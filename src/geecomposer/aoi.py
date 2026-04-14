"""AOI normalization helpers.

Converts supported AOI inputs into Earth Engine geometry objects.
Supported forms:
- ``ee.Geometry``, ``ee.Feature``, ``ee.FeatureCollection`` (passthrough)
- GeoJSON-like Python dictionaries
- Local vector file paths (``.geojson``, ``.json``, ``.shp``, ``.gpkg``)

Multi-feature inputs are dissolved into a single geometry by default,
matching the spec recommendation (section 8.4). This applies to both
GeoJSON dict FeatureCollections and local vector files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import ee

from .exceptions import InvalidAOIError
from .validation import SUPPORTED_AOI_VECTOR_EXTENSIONS


# ---------------------------------------------------------------------------
# Local vector file reading
# ---------------------------------------------------------------------------

def read_vector_file(path: str | os.PathLike) -> dict:
    """Read a local vector file and return a GeoJSON-like geometry dict.

    Uses ``geopandas`` to read the file, dissolves all features into a
    single geometry, reprojects to EPSG:4326, and returns the
    ``__geo_interface__`` geometry mapping.

    Raises ``InvalidAOIError`` on unsupported formats or read failures.
    """
    filepath = Path(path)

    if not filepath.exists():
        raise InvalidAOIError(f"AOI file not found: {filepath}")

    suffix = filepath.suffix.lower()
    if suffix not in SUPPORTED_AOI_VECTOR_EXTENSIONS:
        supported = ", ".join(SUPPORTED_AOI_VECTOR_EXTENSIONS)
        raise InvalidAOIError(
            f"Unsupported vector file extension '{suffix}'. "
            f"Supported formats: {supported}."
        )

    try:
        import geopandas as gpd
    except ImportError as exc:
        raise InvalidAOIError(
            "geopandas is required for local vector file AOI support. "
            "Install it with: pip install geopandas"
        ) from exc

    try:
        gdf = gpd.read_file(filepath)
    except Exception as exc:
        raise InvalidAOIError(f"Failed to read vector file '{filepath}': {exc}") from exc

    if gdf.empty:
        raise InvalidAOIError(f"Vector file contains no features: {filepath}")

    # Reproject to WGS84 if CRS is set and differs
    if gdf.crs is not None and not gdf.crs.equals("EPSG:4326"):
        gdf = gdf.to_crs("EPSG:4326")

    # Dissolve all features into a single geometry
    dissolved = gdf.geometry.union_all()

    if dissolved is None or dissolved.is_empty:
        raise InvalidAOIError(f"Dissolved geometry is empty for: {filepath}")

    return dissolved.__geo_interface__


# ---------------------------------------------------------------------------
# GeoJSON dict to EE geometry
# ---------------------------------------------------------------------------

def _dissolve_feature_collection(features: list[dict]) -> dict:
    """Dissolve all geometries in a GeoJSON feature list into one geometry dict.

    Uses ``shapely`` to union the geometries, matching the dissolve behavior
    of ``read_vector_file`` for local vector inputs.

    Every feature must be a dict with a valid ``geometry`` entry.  Features
    without a ``geometry`` key are skipped, but features whose ``geometry``
    dict is malformed (missing ``coordinates``, unknown geometry type, etc.)
    cause an immediate ``InvalidAOIError`` rather than being silently
    discarded.
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    geoms = []
    for i, feat in enumerate(features):
        geom_dict = feat.get("geometry") if isinstance(feat, dict) else None
        if geom_dict is None:
            continue
        try:
            geoms.append(shape(geom_dict))
        except Exception as exc:
            raise InvalidAOIError(
                f"Malformed geometry in FeatureCollection feature {i}: {exc}"
            ) from exc

    if not geoms:
        raise InvalidAOIError(
            "FeatureCollection contains no valid geometries."
        )

    dissolved = unary_union(geoms)
    if dissolved.is_empty:
        raise InvalidAOIError("Dissolved FeatureCollection geometry is empty.")

    return dissolved.__geo_interface__


def geojson_to_ee_geometry(obj: dict) -> ee.Geometry:
    """Convert a GeoJSON-like mapping to an ``ee.Geometry``.

    Accepts raw geometry dicts (with ``type`` and ``coordinates``),
    Feature wrappers (extracts the geometry), and FeatureCollection
    wrappers (dissolves all feature geometries into one).

    Multi-feature FeatureCollections are dissolved into a single geometry
    to match the behavior of ``read_vector_file`` for local vector inputs.

    Raises ``InvalidAOIError`` if the dict is not a recognized GeoJSON form.
    """
    if not isinstance(obj, dict):
        raise InvalidAOIError(
            f"Expected a GeoJSON-like dict, got {type(obj).__name__}."
        )

    geojson_type = obj.get("type")

    if geojson_type == "FeatureCollection":
        features = obj.get("features")
        if not features:
            raise InvalidAOIError("FeatureCollection has no features.")
        dissolved = _dissolve_feature_collection(features)
        return ee.Geometry(dissolved)

    if geojson_type == "Feature":
        geom = obj.get("geometry")
        if geom is None:
            raise InvalidAOIError("Feature has no geometry.")
        return ee.Geometry(geom)

    if geojson_type in (
        "Point", "MultiPoint",
        "LineString", "MultiLineString",
        "Polygon", "MultiPolygon",
        "GeometryCollection",
    ):
        return ee.Geometry(obj)

    raise InvalidAOIError(
        f"Unrecognized GeoJSON type '{geojson_type}'. "
        "Expected a GeoJSON geometry, Feature, or FeatureCollection dict."
    )


# ---------------------------------------------------------------------------
# Top-level normalizer
# ---------------------------------------------------------------------------

def to_ee_geometry(aoi: Any) -> ee.Geometry:
    """Normalize a supported AOI input to an ``ee.Geometry``.

    Supported inputs:
    - ``ee.Geometry`` -- returned as-is
    - ``ee.Feature`` -- ``.geometry()`` is extracted
    - ``ee.FeatureCollection`` -- ``.geometry()`` is extracted
    - GeoJSON-like ``dict`` -- converted via ``geojson_to_ee_geometry``
    - ``str`` or ``pathlib.Path`` -- treated as a local vector file path

    Raises ``InvalidAOIError`` for unrecognized or invalid inputs.
    """
    if aoi is None:
        raise InvalidAOIError("AOI must not be None.")

    # Earth Engine native objects
    if isinstance(aoi, ee.Geometry):
        return aoi
    if isinstance(aoi, ee.Feature):
        return aoi.geometry()
    if isinstance(aoi, ee.FeatureCollection):
        return aoi.geometry()

    # GeoJSON-like dict
    if isinstance(aoi, dict):
        return geojson_to_ee_geometry(aoi)

    # Local vector file path
    if isinstance(aoi, (str, Path)):
        geojson = read_vector_file(aoi)
        return ee.Geometry(geojson)

    raise InvalidAOIError(
        f"Unsupported AOI type: {type(aoi).__name__}. "
        "Expected ee.Geometry, ee.Feature, ee.FeatureCollection, "
        "a GeoJSON dict, or a path to a local vector file."
    )
