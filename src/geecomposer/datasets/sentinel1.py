"""Sentinel-1 dB-scaled dataset helpers.

Handles collection loading and filtering for Sentinel-1 Ground Range
Detected imagery in decibel units (``COPERNICUS/S1_GRD``).

Band values are in dB scale.  For physically meaningful ratio features
such as VH/VV or RVI, use the ``sentinel1_float`` preset instead, which
operates in linear power units.

No advanced SAR preprocessing (speckle filtering, terrain correction,
coherence) is included in v0.1.
"""

from __future__ import annotations

import ee

from ..validation import validate_dates

from ..exceptions import GeeComposerError
from ._sentinel1_filters import (
    DEFAULT_INSTRUMENT_MODE,
    SUPPORTED_FILTERS as SUPPORTED_FILTERS,
    validate_filters,
)

COLLECTION_ID = "COPERNICUS/S1_GRD"


def get_collection_id() -> str:
    """Return the default Sentinel-1 collection identifier."""
    return COLLECTION_ID


def load_collection(
    aoi: ee.Geometry,
    start: str,
    end: str,
    filters: dict | None = None,
) -> ee.ImageCollection:
    """Load a Sentinel-1 GRD collection filtered by AOI, date, and radar filters.

    Parameters
    ----------
    aoi:
        An ``ee.Geometry`` for spatial filtering.
    start, end:
        ISO date strings for temporal filtering.
    filters:
        Optional dict of Sentinel-1-specific filters.  Supported keys:

        - ``"instrumentMode"`` (str): e.g. ``"IW"``.  Defaults to ``"IW"``
          if not provided.
        - ``"orbitPass"`` (str): ``"ASCENDING"`` or ``"DESCENDING"``.
        - ``"polarizations"`` (list[str]): e.g. ``["VV", "VH"]``.  Each
          entry is applied as a ``listContains`` filter on
          ``transmitterReceiverPolarisation``.

    Returns
    -------
    ee.ImageCollection
        The filtered Sentinel-1 collection.

    Raises
    ------
    GeeComposerError
        If *filters* contains unsupported keys or malformed values.
    """
    validate_dates(start, end)
    filters = {} if filters is None else filters
    validate_filters(filters)

    col = (
        ee.ImageCollection(COLLECTION_ID)
        .filterBounds(aoi)
        .filterDate(start, end)
    )

    instrument_mode = filters.get("instrumentMode", DEFAULT_INSTRUMENT_MODE)
    col = col.filter(ee.Filter.eq("instrumentMode", instrument_mode))

    orbit_pass = filters.get("orbitPass")
    if orbit_pass is not None:
        col = col.filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))

    polarizations = filters.get("polarizations")
    if polarizations is not None:
        for pol in polarizations:
            col = col.filter(
                ee.Filter.listContains("transmitterReceiverPolarisation", pol)
            )

    return col


def apply_mask(
    collection: ee.ImageCollection,
    mask: str,
) -> ee.ImageCollection:
    """Apply a masking preset to a Sentinel-1 collection.

    Sentinel-1 GRD imagery does not have a standard cloud masking workflow.
    No masking presets are supported in v0.1.

    Raises
    ------
    GeeComposerError
        Always — no masking presets are available for Sentinel-1 in v0.1.
    """
    raise GeeComposerError(
        f"No masking presets are supported for Sentinel-1 in v0.1. "
        f"Received mask='{mask}'."
    )
