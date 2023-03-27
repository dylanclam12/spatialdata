"""This file contains functions to compute the bounding box describing the extent of a spatial element,
or of a specific region in the SpatialElement object."""
import numpy as np
from geopandas import GeoDataFrame
from shapely import Point

from spatialdata._types import ArrayLike
from spatialdata.models import get_axis_names


def _get_bounding_box_of_circle_elements(shapes: GeoDataFrame) -> tuple[ArrayLike, ArrayLike, tuple[str, ...]]:
    """Get the coordinates for the corners of the bounding box of that encompasses a circle element, for all the circles.

    Returns
    -------
    min_coordinate
        The minimum coordinate of the bounding box.
    max_coordinate
        The maximum coordinate of the bounding box.
    """
    circle_element = shapes
    if not isinstance(circle_element.geometry.iloc[0], Point):
        raise NotImplementedError("Only circles (shapely Point) are currently supported (not Polygon or MultiPolygon).")
    circle_dims = get_axis_names(circle_element)

    centroids = []
    for dim_name in circle_dims:
        centroids.append(getattr(circle_element["geometry"], dim_name).to_numpy())
    centroids_array = np.column_stack(centroids)
    radius = np.expand_dims(circle_element["radius"].to_numpy(), axis=1)

    min_coordinates = (centroids_array - radius).astype(int)
    max_coordinates = (centroids_array + radius).astype(int)

    return min_coordinates, max_coordinates, circle_dims