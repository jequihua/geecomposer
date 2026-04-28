"""Tests for Sentinel-1 linear-unit (float) dataset loading."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from geecomposer.exceptions import GeeComposerError


class TestSentinel1FloatConstants:
    def test_collection_id(self) -> None:
        from geecomposer.datasets.sentinel1_float import COLLECTION_ID

        assert COLLECTION_ID == "COPERNICUS/S1_GRD_FLOAT"

    def test_get_collection_id(self) -> None:
        from geecomposer.datasets.sentinel1_float import get_collection_id

        assert get_collection_id() == "COPERNICUS/S1_GRD_FLOAT"


class TestLoadCollection:
    @patch("geecomposer.datasets.sentinel1_float.ee")
    def test_loads_float_collection_with_defaults(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1_float import load_collection

        aoi = MagicMock()
        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        result = load_collection(aoi, "2024-01-01", "2024-12-31")

        mock_ee.ImageCollection.assert_called_once_with("COPERNICUS/S1_GRD_FLOAT")
        mock_col.filterBounds.assert_called_once_with(aoi)
        mock_col.filterDate.assert_called_once_with("2024-01-01", "2024-12-31")
        mock_ee.Filter.eq.assert_called_once_with("instrumentMode", "IW")
        assert result is mock_col

    @patch("geecomposer.datasets.sentinel1_float.ee")
    def test_polarization_filter(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1_float import load_collection

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

    @patch("geecomposer.datasets.sentinel1_float.ee")
    def test_orbit_pass_filter(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1_float import load_collection

        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        load_collection(
            MagicMock(), "2024-01-01", "2024-12-31",
            filters={"orbitPass": "DESCENDING"},
        )

        eq_calls = mock_ee.Filter.eq.call_args_list
        assert any(
            c == call("orbitProperties_pass", "DESCENDING") for c in eq_calls
        )


class TestFilterValidation:
    def test_unsupported_filter_key_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="Unsupported Sentinel-1 filter"):
            _validate_filters({"badKey": "value"})

    def test_string_polarizations_raises(self) -> None:
        from geecomposer.datasets._sentinel1_filters import validate_filters as _validate_filters

        with pytest.raises(GeeComposerError, match="polarizations must be a non-empty list"):
            _validate_filters({"polarizations": "VV"})


class TestApplyMask:
    def test_mask_raises(self) -> None:
        from geecomposer.datasets.sentinel1_float import apply_mask

        with pytest.raises(GeeComposerError, match="No masking presets"):
            apply_mask(MagicMock(), "some_mask")


class TestSentinel1FloatPreservesDbPreset:
    """Verify that the dB sentinel1 preset is unchanged."""

    def test_db_collection_id_unchanged(self) -> None:
        from geecomposer.datasets.sentinel1 import COLLECTION_ID

        assert COLLECTION_ID == "COPERNICUS/S1_GRD"

    @patch("geecomposer.datasets.sentinel1.ee")
    def test_db_loads_grd_not_float(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1 import load_collection

        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col
        mock_col.filter.return_value = mock_col

        load_collection(MagicMock(), "2024-01-01", "2024-12-31")
        mock_ee.ImageCollection.assert_called_once_with("COPERNICUS/S1_GRD")
