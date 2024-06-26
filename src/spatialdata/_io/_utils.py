from __future__ import annotations

import filecmp
import logging
import os.path
import re
import tempfile
import warnings
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from functools import singledispatch
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from anndata import AnnData
from anndata import read_zarr as read_anndata_zarr
from anndata.experimental import read_elem
from dask.array.core import Array as DaskArray
from dask.dataframe.core import DataFrame as DaskDataFrame
from geopandas import GeoDataFrame
from multiscale_spatial_image import MultiscaleSpatialImage
from ome_zarr.format import Format
from ome_zarr.writer import _get_valid_axes
from spatial_image import SpatialImage

from spatialdata._core.spatialdata import SpatialData
from spatialdata._logging import logger
from spatialdata._utils import iterate_pyramid_levels
from spatialdata.models import TableModel
from spatialdata.models._utils import (
    MappingToCoordinateSystem_t,
    SpatialElement,
    ValidAxis_t,
    _validate_mapping_to_coordinate_system_type,
)
from spatialdata.transformations.ngff.ngff_transformations import NgffBaseTransformation
from spatialdata.transformations.transformations import (
    BaseTransformation,
    _get_current_output_axes,
)


# suppress logger debug from ome_zarr with context manager
@contextmanager
def ome_zarr_logger(level: Any) -> Generator[None, None, None]:
    logger = logging.getLogger("ome_zarr")
    current_level = logger.getEffectiveLevel()
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.setLevel(current_level)


def _get_transformations_from_ngff_dict(
    list_of_encoded_ngff_transformations: list[dict[str, Any]]
) -> MappingToCoordinateSystem_t:
    list_of_ngff_transformations = [NgffBaseTransformation.from_dict(d) for d in list_of_encoded_ngff_transformations]
    list_of_transformations = [BaseTransformation.from_ngff(t) for t in list_of_ngff_transformations]
    transformations = {}
    for ngff_t, t in zip(list_of_ngff_transformations, list_of_transformations):
        assert ngff_t.output_coordinate_system is not None
        transformations[ngff_t.output_coordinate_system.name] = t
    return transformations


def overwrite_coordinate_transformations_non_raster(
    group: zarr.Group, axes: tuple[ValidAxis_t, ...], transformations: MappingToCoordinateSystem_t
) -> None:
    _validate_mapping_to_coordinate_system_type(transformations)
    ngff_transformations = []
    for target_coordinate_system, t in transformations.items():
        output_axes = _get_current_output_axes(transformation=t, input_axes=tuple(axes))
        ngff_transformations.append(
            t.to_ngff(
                input_axes=tuple(axes),
                output_axes=tuple(output_axes),
                output_coordinate_system_name=target_coordinate_system,
            ).to_dict()
        )
    group.attrs["coordinateTransformations"] = ngff_transformations


def overwrite_coordinate_transformations_raster(
    group: zarr.Group, axes: tuple[ValidAxis_t, ...], transformations: MappingToCoordinateSystem_t
) -> None:
    _validate_mapping_to_coordinate_system_type(transformations)
    # prepare the transformations in the dict representation
    ngff_transformations = []
    for target_coordinate_system, t in transformations.items():
        output_axes = _get_current_output_axes(transformation=t, input_axes=tuple(axes))
        ngff_transformations.append(
            t.to_ngff(
                input_axes=tuple(axes),
                output_axes=tuple(output_axes),
                output_coordinate_system_name=target_coordinate_system,
            )
        )
    coordinate_transformations = [t.to_dict() for t in ngff_transformations]
    # replace the metadata storage
    multiscales = group.attrs["multiscales"]
    assert len(multiscales) == 1
    multiscale = multiscales[0]
    # the transformation present in multiscale["datasets"] are the ones for the multiscale, so and we leave them intact
    # we update multiscale["coordinateTransformations"] and multiscale["coordinateSystems"]
    # see the first post of https://github.com/scverse/spatialdata/issues/39 for an overview
    # fix the io to follow the NGFF specs, see https://github.com/scverse/spatialdata/issues/114
    multiscale["coordinateTransformations"] = coordinate_transformations
    # multiscale["coordinateSystems"] = [t.output_coordinate_system_name for t in ngff_transformations]
    group.attrs["multiscales"] = multiscales


def _write_metadata(
    group: zarr.Group,
    group_type: str,
    fmt: Format,
    axes: str | list[str] | list[dict[str, str]] | None = None,
    attrs: Mapping[str, Any] | None = None,
) -> None:
    """Write metdata to a group."""
    axes = _get_valid_axes(axes=axes, fmt=fmt)

    group.attrs["encoding-type"] = group_type
    group.attrs["axes"] = axes
    # we write empty coordinateTransformations and then overwrite
    # them with overwrite_coordinate_transformations_non_raster()
    group.attrs["coordinateTransformations"] = []
    # group.attrs["coordinateTransformations"] = coordinate_transformations
    group.attrs["spatialdata_attrs"] = attrs


def _iter_multiscale(
    data: MultiscaleSpatialImage,
    attr: str | None,
) -> list[Any]:
    # TODO: put this check also in the validator for raster multiscales
    for i in data:
        variables = set(data[i].variables.keys())
        names: set[str] = variables.difference({"c", "z", "y", "x"})
        if len(names) != 1:
            raise ValueError(f"Invalid variable name: `{names}`.")
    name: str = next(iter(names))
    if attr is not None:
        return [getattr(data[i][name], attr) for i in data]
    return [data[i][name] for i in data]


class dircmp(filecmp.dircmp):  # type: ignore[type-arg]
    """
    Compare the content of dir1 and dir2.

    In contrast with filecmp.dircmp, this
    subclass compares the content of files with the same path.
    """

    # from https://stackoverflow.com/a/24860799/3343783
    def phase3(self) -> None:
        """
        Differences between common files.

        Ensure we are using content comparison with shallow=False.
        """
        fcomp = filecmp.cmpfiles(self.left, self.right, self.common_files, shallow=False)
        self.same_files, self.diff_files, self.funny_files = fcomp


def _are_directories_identical(
    dir1: Any,
    dir2: Any,
    exclude_regexp: str | None = None,
    _root_dir1: str | None = None,
    _root_dir2: str | None = None,
) -> bool:
    """
    Compare two directory trees content.

    Return False if they differ, True is they are the same.
    """
    if _root_dir1 is None:
        _root_dir1 = dir1
    if _root_dir2 is None:
        _root_dir2 = dir2
    if exclude_regexp is not None and (
        re.match(rf"{re.escape(str(_root_dir1))}/" + exclude_regexp, str(dir1))
        or re.match(rf"{re.escape(str(_root_dir2))}/" + exclude_regexp, str(dir2))
    ):
        return True

    compared = dircmp(dir1, dir2)
    if compared.left_only or compared.right_only or compared.diff_files or compared.funny_files:
        return False
    for subdir in compared.common_dirs:
        if not _are_directories_identical(
            os.path.join(dir1, subdir),
            os.path.join(dir2, subdir),
            exclude_regexp=exclude_regexp,
            _root_dir1=_root_dir1,
            _root_dir2=_root_dir2,
        ):
            return False
    return True


def _compare_sdata_on_disk(a: SpatialData, b: SpatialData) -> bool:
    if not isinstance(a, SpatialData) or not isinstance(b, SpatialData):
        return False
    # TODO: if the sdata object is backed on disk, don't create a new zarr file
    with tempfile.TemporaryDirectory() as tmpdir:
        a.write(os.path.join(tmpdir, "a.zarr"))
        b.write(os.path.join(tmpdir, "b.zarr"))
        return _are_directories_identical(os.path.join(tmpdir, "a.zarr"), os.path.join(tmpdir, "b.zarr"))


@singledispatch
def get_dask_backing_files(element: SpatialData | SpatialElement | AnnData) -> list[str]:
    """
    Get the backing files that appear in the Dask computational graph of an element/any element of a SpatialData object.

    Parameters
    ----------
    element
        The element to get the backing files from.

    Returns
    -------
    List of backing files.

    Notes
    -----
    It is possible for lazy objects to be constructed from multiple files.
    """
    raise TypeError(f"Unsupported type: {type(element)}")


@get_dask_backing_files.register(SpatialData)
def _(element: SpatialData) -> list[str]:
    files: set[str] = set()
    for e in element._gen_spatial_element_values():
        if isinstance(e, (SpatialImage, MultiscaleSpatialImage, DaskDataFrame)):
            files = files.union(get_dask_backing_files(e))
    return list(files)


@get_dask_backing_files.register(SpatialImage)
def _(element: SpatialImage) -> list[str]:
    return _get_backing_files(element.data)


@get_dask_backing_files.register(MultiscaleSpatialImage)
def _(element: MultiscaleSpatialImage) -> list[str]:
    xdata0 = next(iter(iterate_pyramid_levels(element)))
    return _get_backing_files(xdata0.data)


@get_dask_backing_files.register(DaskDataFrame)
def _(element: DaskDataFrame) -> list[str]:
    return _get_backing_files(element)


@get_dask_backing_files.register(AnnData)
@get_dask_backing_files.register(GeoDataFrame)
def _(element: AnnData | GeoDataFrame) -> list[str]:
    return []


def _get_backing_files(element: DaskArray | DaskDataFrame) -> list[str]:
    files = []
    for k, v in element.dask.layers.items():
        if k.startswith("original-from-zarr-"):
            mapping = v.mapping[k]
            path = mapping.store.path
            files.append(os.path.realpath(path))
        if k.startswith("read-parquet-"):
            t = v.creation_info["args"]
            assert isinstance(t, tuple)
            assert len(t) == 1
            parquet_file = t[0]
            files.append(os.path.realpath(parquet_file))
    return files


def _backed_elements_contained_in_path(path: Path, object: SpatialData | SpatialElement | AnnData) -> list[bool]:
    """
    Return the list of boolean values indicating if backing files for an object are child directory of a path.

    Parameters
    ----------
    path
        The path to check if the backing files are contained in.
    object
        The object to check the backing files of.

    Returns
    -------
    List of boolean values for each of the backing files.

    Notes
    -----
    If an object does not have a Dask computational graph, it will return an empty list.
    It is possible for a single SpatialElement to contain multiple files in their Dask computational graph.
    """
    if not isinstance(path, Path):
        raise TypeError(f"Expected a Path object, got {type(path)}")
    return [_is_subfolder(parent=path, child=Path(fp)) for fp in get_dask_backing_files(object)]


def _is_subfolder(parent: Path, child: Path) -> bool:
    """
    Check if a path is a subfolder of another path.

    Parameters
    ----------
    parent
        The parent folder.
    child
        The child folder.

    Returns
    -------
    True if the child is a subfolder of the parent.
    """
    if isinstance(child, str):
        child = Path(child)
    if isinstance(parent, str):
        parent = Path(parent)
    if not isinstance(parent, Path) or not isinstance(child, Path):
        raise TypeError(f"Expected a Path object, got {type(parent)} and {type(child)}")
    return child.resolve().is_relative_to(parent.resolve())


def _is_element_self_contained(
    element: SpatialImage | MultiscaleSpatialImage | DaskDataFrame | GeoDataFrame | AnnData, element_path: Path
) -> bool:
    return all(_backed_elements_contained_in_path(path=element_path, object=element))


def save_transformations(sdata: SpatialData) -> None:
    """
    Save all the transformations of a SpatialData object to disk.

    sdata
        The SpatialData object
    """
    warnings.warn(
        "This function is deprecated and should be replaced by `SpatialData.write_transformations()` or "
        "`SpatialData.write_metadata()`, which gives more control over which metadata to write. This function will call"
        " `SpatialData.write_transformations()`; please call this function directly.",
        DeprecationWarning,
        stacklevel=2,
    )
    sdata.write_transformations()


def read_table_and_validate(
    zarr_store_path: str, group: zarr.Group, subgroup: zarr.Group, tables: dict[str, AnnData]
) -> dict[str, AnnData]:
    """
    Read in tables in the tables Zarr.group of a SpatialData Zarr store.

    Parameters
    ----------
    zarr_store_path
        The path to the Zarr store.
    group
        The parent group containing the subgroup.
    subgroup
        The subgroup containing the tables.
    tables
        A dictionary of tables.

    Returns
    -------
    The modified dictionary with the tables.
    """
    count = 0
    for table_name in subgroup:
        f_elem = subgroup[table_name]
        f_elem_store = os.path.join(zarr_store_path, f_elem.path)
        if isinstance(group.store, zarr.storage.ConsolidatedMetadataStore):
            tables[table_name] = read_elem(f_elem)
            # we can replace read_elem with read_anndata_zarr after this PR gets into a release (>= 0.6.5)
            # https://github.com/scverse/anndata/pull/1057#pullrequestreview-1530623183
            # table = read_anndata_zarr(f_elem)
        else:
            tables[table_name] = read_anndata_zarr(f_elem_store)
        if TableModel.ATTRS_KEY in tables[table_name].uns:
            # fill out eventual missing attributes that has been omitted because their value was None
            attrs = tables[table_name].uns[TableModel.ATTRS_KEY]
            if "region" not in attrs:
                attrs["region"] = None
            if "region_key" not in attrs:
                attrs["region_key"] = None
            if "instance_key" not in attrs:
                attrs["instance_key"] = None
            # fix type for region
            if "region" in attrs and isinstance(attrs["region"], np.ndarray):
                attrs["region"] = attrs["region"].tolist()

        count += 1

    logger.debug(f"Found {count} elements in {subgroup}")
    return tables
