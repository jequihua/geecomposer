"""Tests for validation helpers and shared constants."""

from __future__ import annotations

import pytest

from geecomposer.exceptions import DatasetNotSupportedError, InvalidReducerError
from geecomposer.validation import (
    SUPPORTED_AOI_VECTOR_EXTENSIONS,
    SUPPORTED_DATASETS,
    SUPPORTED_REDUCERS,
    validate_dataset,
    validate_reducer,
)


# --- validate_reducer ---


class TestValidateReducer:
    @pytest.mark.parametrize("name", list(SUPPORTED_REDUCERS))
    def test_valid_reducers_accepted(self, name: str) -> None:
        assert validate_reducer(name) == name

    def test_case_insensitive(self) -> None:
        assert validate_reducer("Median") == "median"
        assert validate_reducer("MAX") == "max"

    def test_strips_whitespace(self) -> None:
        assert validate_reducer("  mean  ") == "mean"

    def test_invalid_reducer_raises(self) -> None:
        with pytest.raises(InvalidReducerError, match="Unsupported reducer 'sum'"):
            validate_reducer("sum")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(InvalidReducerError):
            validate_reducer("")

    def test_none_raises_package_error(self) -> None:
        with pytest.raises(InvalidReducerError, match="must be a string"):
            validate_reducer(None)

    def test_int_raises_package_error(self) -> None:
        with pytest.raises(InvalidReducerError, match="must be a string"):
            validate_reducer(42)


# --- validate_dataset ---


class TestValidateDataset:
    @pytest.mark.parametrize("name", list(SUPPORTED_DATASETS))
    def test_valid_datasets_accepted(self, name: str) -> None:
        assert validate_dataset(name) == name

    def test_case_insensitive(self) -> None:
        assert validate_dataset("Sentinel2") == "sentinel2"

    def test_invalid_dataset_raises(self) -> None:
        with pytest.raises(DatasetNotSupportedError, match="Unsupported dataset 'landsat'"):
            validate_dataset("landsat")

    def test_none_raises_package_error(self) -> None:
        with pytest.raises(DatasetNotSupportedError, match="must be a string"):
            validate_dataset(None)

    def test_int_raises_package_error(self) -> None:
        with pytest.raises(DatasetNotSupportedError, match="must be a string"):
            validate_dataset(123)


# --- constants ---


class TestConstants:
    def test_supported_reducers_is_tuple(self) -> None:
        assert isinstance(SUPPORTED_REDUCERS, tuple)
        assert len(SUPPORTED_REDUCERS) == 5

    def test_supported_datasets_is_tuple(self) -> None:
        assert isinstance(SUPPORTED_DATASETS, tuple)
        assert len(SUPPORTED_DATASETS) == 2

    def test_supported_vector_extensions(self) -> None:
        assert ".geojson" in SUPPORTED_AOI_VECTOR_EXTENSIONS
        assert ".shp" in SUPPORTED_AOI_VECTOR_EXTENSIONS
        assert ".gpkg" in SUPPORTED_AOI_VECTOR_EXTENSIONS
