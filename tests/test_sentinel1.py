"""Tests for Sentinel-1 dataset loading and filtering."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from geecomposer.exceptions import GeeComposerError


class TestSentinel1Constants:
    def test_collection_id(self) -> None:
        from geecomposer.datasets.sentinel1 import COLLECTION_ID

        assert COLLECTION_ID == "COPERNICUS/S1_GRD"

    def test_get_collection_id(self) -> None:
        from geecomposer.datasets.sentinel1 import get_collection_id

        assert get_collection_id() == "COPERNICUS/S1_GRD"

    def test_supported_filters(self) -> None:
        from geecomposer.datasets.sentinel1 import SUPPORTED_FILTERS

        assert "instrumentMode" in SUPPORTED_FILTERS
        assert "orbitPass" in SUPPORTED_FILTERS
        assert "polarizations" in SUPPORTED_FILTERS


class TestLoadCollection:
    @patch("geecomposer.datasets.sentinel1.ee")
    def test_loads_and_filters_with_defaults(self, mock_ee: MagicMock) -> None:
        """Default behavior: AOI + date + instrumentMode=IW."""
        from geecomposer.datasets.sentinel1 import load_collection

        aoi = MagicMock()
        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        result = load_collection(aoi, "2024-01-01", "2024-12-31")

        mock_ee.ImageCollection.assert_called_once_with("COPERNICUS/S1_GRD")
        mock_col.filterBounds.assert_called_once_with(aoi)
        mock_col.filterDate.assert_called_once_with("2024-01-01", "2024-12-31")
        # Default IW instrument mode filter
        mock_ee.Filter.eq.assert_called_once_with("instrumentMode", "IW")
        assert result is mock_col

    @patch("geecomposer.datasets.sentinel1.ee")
    def test_orbit_pass_filter(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1 import load_collection

        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        load_collection(
            MagicMock(), "2024-01-01", "2024-12-31",
            filters={"orbitPass": "ASCENDING"},
        )

        # Should have instrumentMode=IW + orbitPass=ASCENDING
        eq_calls = mock_ee.Filter.eq.call_args_list
        assert any(
            c == call("orbitProperties_pass", "ASCENDING") for c in eq_calls
        )

    @patch("geecomposer.datasets.sentinel1.ee")
    def test_polarization_filter(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1 import load_collection

        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        load_collection(
            MagicMock(), "2024-01-01", "2024-12-31",
            filters={"polarizations": ["VV", "VH"]},
        )

        lc_calls = mock_ee.Filter.listContains.call_args_list
        assert call("transmitterReceiverPolarisation", "VV") in lc_calls
        assert call("transmitterReceiverPolarisation", "VH") in lc_calls

    @patch("geecomposer.datasets.sentinel1.ee")
    def test_custom_instrument_mode(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1 import load_collection

        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        load_collection(
            MagicMock(), "2024-01-01", "2024-12-31",
            filters={"instrumentMode": "EW"},
        )

        mock_ee.Filter.eq.assert_any_call("instrumentMode", "EW")

    @patch("geecomposer.datasets.sentinel1.ee")
    def test_all_filters_combined(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1 import load_collection

        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        load_collection(
            MagicMock(), "2024-01-01", "2024-12-31",
            filters={
                "instrumentMode": "IW",
                "orbitPass": "DESCENDING",
                "polarizations": ["VV"],
            },
        )

        eq_calls = mock_ee.Filter.eq.call_args_list
        assert call("instrumentMode", "IW") in eq_calls
        assert call("orbitProperties_pass", "DESCENDING") in eq_calls
        mock_ee.Filter.listContains.assert_called_once_with(
            "transmitterReceiverPolarisation", "VV"
        )


class TestFilterValidation:
    def test_unsupported_filter_key_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="Unsupported Sentinel-1 filter"):
            _validate_filters({"resolution_meters": 10})

    def test_mixed_valid_and_invalid_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="Unsupported"):
            _validate_filters({"instrumentMode": "IW", "badKey": "value"})

    def test_valid_filters_pass(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        _validate_filters({"instrumentMode": "IW", "orbitPass": "ASCENDING"})

    def test_empty_filters_pass(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        _validate_filters({})

    def test_non_string_instrument_mode_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="instrumentMode must be a non-empty string"):
            _validate_filters({"instrumentMode": 123})

    def test_non_string_orbit_pass_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="orbitPass must be a non-empty string"):
            _validate_filters({"orbitPass": 123})

    def test_empty_polarizations_list_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="polarizations must be a non-empty list"):
            _validate_filters({"polarizations": []})

    def test_string_polarizations_raises(self) -> None:
        """A bare string like 'VV' must be rejected — it would iterate as characters."""
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="polarizations must be a non-empty list"):
            _validate_filters({"polarizations": "VV"})

    def test_non_string_element_in_polarizations_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="polarizations\\[1\\] must be a non-empty string"):
            _validate_filters({"polarizations": ["VV", 42]})

    def test_empty_string_instrument_mode_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="instrumentMode must be a non-empty string"):
            _validate_filters({"instrumentMode": ""})


class TestApplyMask:
    def test_mask_raises(self) -> None:
        from geecomposer.datasets.sentinel1 import apply_mask

        col = MagicMock()
        with pytest.raises(GeeComposerError, match="No masking presets"):
            apply_mask(col, "some_mask")
