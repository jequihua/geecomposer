"""Sentinel-1 dataset helpers.

Handles collection loading and filtering for Sentinel-1 Ground Range
Detected (GRD) imagery.  No advanced SAR preprocessing (speckle filtering,
terrain correction, coherence) is included in v0.1.
"""

from __future__ import annotations

import ee

from ..exceptions import GeeComposerError

COLLECTION_ID = "COPERNICUS/S1_GRD"

# Filters accepted through the ``filters`` dict parameter.
SUPPORTED_FILTERS: tuple[str, ...] = ("instrumentMode", "orbitPass", "polarizations")

# Default instrument mode applied when no filters are provided.
_DEFAULT_INSTRUMENT_MODE = "IW"


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
    filters = filters or {}
    _validate_filters(filters)

    col = (
        ee.ImageCollection(COLLECTION_ID)
        .filterBounds(aoi)
        .filterDate(start, end)
    )

    # Instrument mode — default to IW when not explicitly provided.
    instrument_mode = filters.get("instrumentMode", _DEFAULT_INSTRUMENT_MODE)
    col = col.filter(ee.Filter.eq("instrumentMode", instrument_mode))

    # Orbit pass direction (optional).
    orbit_pass = filters.get("orbitPass")
    if orbit_pass is not None:
        col = col.filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))

    # Polarization filter (optional).
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


def _validate_filters(filters: dict) -> None:
    """Validate filter keys and values for Sentinel-1.

    Checks:
    - all keys are in ``SUPPORTED_FILTERS``
    - ``instrumentMode`` is a non-empty string (if present)
    - ``orbitPass`` is a non-empty string (if present)
    - ``polarizations`` is a non-empty list or tuple of non-empty strings
      (if present)
    """
    # Key validation
    unsupported = set(filters.keys()) - set(SUPPORTED_FILTERS)
    if unsupported:
        supported = ", ".join(SUPPORTED_FILTERS)
        raise GeeComposerError(
            f"Unsupported Sentinel-1 filter keys: {sorted(unsupported)}. "
            f"Supported filters: {supported}."
        )

    # Value validation
    if "instrumentMode" in filters:
        val = filters["instrumentMode"]
        if not isinstance(val, str) or not val.strip():
            raise GeeComposerError(
                f"instrumentMode must be a non-empty string, got {type(val).__name__}."
            )

    if "orbitPass" in filters:
        val = filters["orbitPass"]
        if not isinstance(val, str) or not val.strip():
            raise GeeComposerError(
                f"orbitPass must be a non-empty string, got {type(val).__name__}."
            )

    if "polarizations" in filters:
        val = filters["polarizations"]
        if not isinstance(val, (list, tuple)) or len(val) == 0:
            raise GeeComposerError(
                "polarizations must be a non-empty list of strings, "
                f"got {type(val).__name__}."
            )
        for i, pol in enumerate(val):
            if not isinstance(pol, str) or not pol.strip():
                raise GeeComposerError(
                    f"polarizations[{i}] must be a non-empty string, "
                    f"got {type(pol).__name__}."
                )
