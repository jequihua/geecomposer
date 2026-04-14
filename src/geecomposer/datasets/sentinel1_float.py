"""Sentinel-1 linear-unit (float) dataset helpers.

Handles collection loading and filtering for the Sentinel-1 Ground Range
Detected imagery in linear power units (``COPERNICUS/S1_GRD_FLOAT``).

This module provides the same filter interface as the dB-scaled
``sentinel1`` module but uses the float-scaled collection where band values
represent linear backscatter power.  Linear units are required for
physically meaningful ratio and algebraic features such as VH/VV, VH-VV,
and RVI.

No advanced SAR preprocessing (speckle filtering, terrain correction,
coherence) is included.
"""

from __future__ import annotations

import ee

from ..exceptions import GeeComposerError
from ._sentinel1_filters import (
    DEFAULT_INSTRUMENT_MODE,
    SUPPORTED_FILTERS,
    validate_filters,
)

COLLECTION_ID = "COPERNICUS/S1_GRD_FLOAT"


def get_collection_id() -> str:
    """Return the Sentinel-1 float collection identifier."""
    return COLLECTION_ID


def load_collection(
    aoi: ee.Geometry,
    start: str,
    end: str,
    filters: dict | None = None,
) -> ee.ImageCollection:
    """Load a Sentinel-1 GRD Float collection filtered by AOI, date, and radar filters.

    Parameters
    ----------
    aoi:
        An ``ee.Geometry`` for spatial filtering.
    start, end:
        ISO date strings for temporal filtering.
    filters:
        Optional dict of Sentinel-1-specific filters.  Supported keys are
        the same as for the dB-scaled ``sentinel1`` module:

        - ``"instrumentMode"`` (str): e.g. ``"IW"``.  Defaults to ``"IW"``.
        - ``"orbitPass"`` (str): ``"ASCENDING"`` or ``"DESCENDING"``.
        - ``"polarizations"`` (list[str]): e.g. ``["VV", "VH"]``.

    Returns
    -------
    ee.ImageCollection
        The filtered Sentinel-1 float collection.

    Raises
    ------
    GeeComposerError
        If *filters* contains unsupported keys or malformed values.
    """
    filters = filters or {}
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
    """Apply a masking preset to a Sentinel-1 float collection.

    No masking presets are supported for Sentinel-1 in v0.1.

    Raises
    ------
    GeeComposerError
        Always.
    """
    raise GeeComposerError(
        f"No masking presets are supported for Sentinel-1 float in v0.1. "
        f"Received mask='{mask}'."
    )
