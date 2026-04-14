"""Tests for reducer mapping behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from geecomposer.exceptions import InvalidReducerError
from geecomposer.reducers.temporal import _REDUCER_MAP, apply_reducer
from geecomposer.validation import SUPPORTED_REDUCERS


class TestReducerMap:
    def test_all_supported_reducers_have_entries(self) -> None:
        for name in SUPPORTED_REDUCERS:
            assert name in _REDUCER_MAP, f"Missing reducer map entry for '{name}'"

    def test_no_extra_entries(self) -> None:
        for name in _REDUCER_MAP:
            assert name in SUPPORTED_REDUCERS, f"Unexpected reducer map entry '{name}'"


class TestApplyReducer:
    @pytest.mark.parametrize("name", list(SUPPORTED_REDUCERS))
    def test_dispatches_to_correct_method(self, name: str) -> None:
        collection = MagicMock()
        sentinel = MagicMock(name=f"result_{name}")
        getattr(collection, name).return_value = sentinel

        result = apply_reducer(collection, name)
        getattr(collection, name).assert_called_once()
        assert result is sentinel

    def test_invalid_reducer_raises(self) -> None:
        collection = MagicMock()
        with pytest.raises(InvalidReducerError, match="percentile"):
            apply_reducer(collection, "percentile")

    def test_case_insensitive(self) -> None:
        collection = MagicMock()
        sentinel = MagicMock()
        collection.median.return_value = sentinel

        result = apply_reducer(collection, "Median")
        collection.median.assert_called_once()
        assert result is sentinel
