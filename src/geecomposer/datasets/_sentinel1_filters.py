"""Shared Sentinel-1 filter constants and validation.

Used by both the dB (``sentinel1``) and linear-unit (``sentinel1_float``)
dataset modules.  Kept separate so neither module imports private symbols
from the other.
"""

from __future__ import annotations

from ..exceptions import GeeComposerError

# Filters accepted through the ``filters`` dict parameter.
SUPPORTED_FILTERS: tuple[str, ...] = ("instrumentMode", "orbitPass", "polarizations")

# Default instrument mode applied when no filters are provided.
DEFAULT_INSTRUMENT_MODE = "IW"


def validate_filters(filters: dict) -> None:
    """Validate filter keys and values for Sentinel-1 collections.

    Checks:
    - all keys are in ``SUPPORTED_FILTERS``
    - ``instrumentMode`` is a non-empty string (if present)
    - ``orbitPass`` is a non-empty string (if present)
    - ``polarizations`` is a non-empty list or tuple of non-empty strings
      (if present)
    """
    if not isinstance(filters, dict):
        raise GeeComposerError("filters must be a dict")
    unsupported = set(filters.keys()) - set(SUPPORTED_FILTERS)
    if unsupported:
        supported = ", ".join(SUPPORTED_FILTERS)
        raise GeeComposerError(
            f"Unsupported Sentinel-1 filter keys: {sorted(map(str, unsupported))}. "
            f"Supported filters: {supported}."
        )

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

    if "instrumentMode" in filters and filters["instrumentMode"] not in ("IW", "EW", "SM"):
        raise GeeComposerError("instrumentMode must be IW, EW or SM")
    if "orbitPass" in filters and filters["orbitPass"] not in ("ASCENDING", "DESCENDING"):
        raise GeeComposerError("orbitPass must be ASCENDING or DESCENDING")
    if "polarizations" in filters:
        values = filters["polarizations"]
        if any(p not in ("VV", "VH", "HH", "HV") for p in values) or len(set(values)) != len(values):
            raise GeeComposerError("polarizations must be unique VV, VH, HH or HV values")
