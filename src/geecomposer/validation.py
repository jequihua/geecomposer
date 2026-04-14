"""Input validation helpers and shared constants."""

from __future__ import annotations

from .exceptions import DatasetNotSupportedError, InvalidReducerError

SUPPORTED_DATASETS: tuple[str, ...] = ("sentinel1", "sentinel2")
SUPPORTED_REDUCERS: tuple[str, ...] = ("median", "mean", "min", "max", "mosaic")

SUPPORTED_AOI_VECTOR_EXTENSIONS: tuple[str, ...] = (".geojson", ".json", ".shp", ".gpkg")


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
