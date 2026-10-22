"""Input validation helpers and shared constants."""

from __future__ import annotations

from datetime import date

from .exceptions import DatasetNotSupportedError, InvalidReducerError, GeeComposerError

SUPPORTED_DATASETS: tuple[str, ...] = ("sentinel1", "sentinel1_float", "sentinel2")
SUPPORTED_REDUCERS: tuple[str, ...] = (
    "median",
    "mean",
    "min",
    "max",
    "mosaic",
    "count",
)

SUPPORTED_AOI_VECTOR_EXTENSIONS: tuple[str, ...] = (
    ".geojson",
    ".json",
    ".shp",
    ".gpkg",
)


def validate_reducer(reducer_name: str) -> str:
    """Validate that *reducer_name* is a supported temporal reducer.

    Returns the validated name (lower-cased) on success.
    Raises ``InvalidReducerError`` on non-string input or unsupported names.
    """
    if not isinstance(reducer_name, str):
        raise InvalidReducerError(
            f"Reducer name must be a string, got {type(reducer_name).__name__}."
        )
    normalized = reducer_name.strip().lower()
    if normalized not in SUPPORTED_REDUCERS:
        supported = ", ".join(SUPPORTED_REDUCERS)
        raise InvalidReducerError(
            f"Unsupported reducer '{reducer_name}'. Supported reducers are: {supported}."
        )
    return normalized


def validate_dataset(dataset_name: str) -> str:
    """Validate that *dataset_name* is a supported dataset preset.

    Returns the validated name (lower-cased) on success.
    Raises ``DatasetNotSupportedError`` on non-string input or unsupported names.
    """
    if not isinstance(dataset_name, str):
        raise DatasetNotSupportedError(
            f"Dataset name must be a string, got {type(dataset_name).__name__}."
        )
    normalized = dataset_name.strip().lower()
    if normalized not in SUPPORTED_DATASETS:
        supported = ", ".join(SUPPORTED_DATASETS)
        raise DatasetNotSupportedError(
            f"Unsupported dataset '{dataset_name}'. Supported datasets are: {supported}."
        )
    return normalized


def date_interval(start, end):
    """Return canonical half-open dates; the shared core raises ValueError."""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("start and end dates must be ISO YYYY-MM-DD") from exc
    if first.isoformat() != start or last.isoformat() != end or first >= last:
        raise ValueError("dates must be YYYY-MM-DD with start before exclusive end")
    return first, last


def validate_dates(start, end):
    """Validate dates while preserving the composition API's package error."""
    try:
        date_interval(start, end)
    except ValueError as exc:
        raise GeeComposerError(str(exc)) from exc


def reject_extra_filters(filters):
    """Loaders without filter support must not silently discard input."""
    if filters is not None and (not isinstance(filters, dict) or filters):
        raise GeeComposerError("extra filters are not supported for this collection")
