"""Tests for Sentinel-2 dataset loading and masking."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from geecomposer.exceptions import GeeComposerError


class TestSentinel2Constants:
    def test_collection_id(self) -> None:
        from geecomposer.datasets.sentinel2 import COLLECTION_ID

        assert COLLECTION_ID == "COPERNICUS/S2_SR_HARMONIZED"

    def test_get_collection_id(self) -> None:
        from geecomposer.datasets.sentinel2 import get_collection_id

        assert get_collection_id() == "COPERNICUS/S2_SR_HARMONIZED"

    def test_supported_masks(self) -> None:
        from geecomposer.datasets.sentinel2 import SUPPORTED_MASKS

        assert "s2_cloud_score_plus" in SUPPORTED_MASKS


class TestLoadCollection:
    @patch("geecomposer.datasets.sentinel2.ee")
    def test_loads_and_filters(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel2 import load_collection

        aoi = MagicMock()
        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col

        result = load_collection(aoi, "2024-01-01", "2024-12-31")

        mock_ee.ImageCollection.assert_called_once_with("COPERNICUS/S2_SR_HARMONIZED")
        mock_col.filterBounds.assert_called_once_with(aoi)
        mock_col.filterDate.assert_called_once_with("2024-01-01", "2024-12-31")
        assert result is mock_col


class TestApplyMask:
    def test_unsupported_mask_raises(self) -> None:
        from geecomposer.datasets.sentinel2 import apply_mask

        col = MagicMock()
        with pytest.raises(GeeComposerError, match="Unsupported Sentinel-2 mask"):
            apply_mask(col, mask="invalid_mask")

    @patch("geecomposer.datasets.sentinel2.ee")
    def test_cloud_score_plus_joins_and_maps(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel2 import apply_mask

        col = MagicMock()
        col.geometry.return_value = MagicMock()

        cs_plus_col = MagicMock()
        cs_plus_col.filterBounds.return_value = cs_plus_col
        mock_ee.ImageCollection.return_value = cs_plus_col

        linked_col = MagicMock()
        col.linkCollection.return_value = linked_col
        mapped_col = MagicMock()
        linked_col.map.return_value = mapped_col

        result = apply_mask(col, mask="s2_cloud_score_plus")

        # Should load the Cloud Score+ collection
        mock_ee.ImageCollection.assert_called_once_with(
            "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
        )
        # Should link the CS+ band to the S2 collection
        col.linkCollection.assert_called_once_with(cs_plus_col, ["cs_cdf"])
        # Should map a masking function
        linked_col.map.assert_called_once()
        assert result is mapped_col

    @patch("geecomposer.datasets.sentinel2.ee")
    def test_cloud_score_plus_mask_fn_threshold(self, mock_ee: MagicMock) -> None:
        """The mapped mask function selects cs_cdf, applies gte(0.6), and updateMask."""
        from geecomposer.datasets.sentinel2 import apply_mask

        col = MagicMock()
        col.geometry.return_value = MagicMock()

        cs_plus_col = MagicMock()
        cs_plus_col.filterBounds.return_value = cs_plus_col
        mock_ee.ImageCollection.return_value = cs_plus_col

        linked_col = MagicMock()
        col.linkCollection.return_value = linked_col

        apply_mask(col, mask="s2_cloud_score_plus")

        # Extract the mask function passed to .map()
        mask_fn = linked_col.map.call_args[0][0]

        # Build a mock image and run the mask function
        img = MagicMock()
        score_band = MagicMock()
        mask_result = MagicMock()
        img.select.return_value = score_band
        score_band.gte.return_value = mask_result
        masked_img = MagicMock()
        img.updateMask.return_value = masked_img

        result = mask_fn(img)

        img.select.assert_called_once_with("cs_cdf")
        score_band.gte.assert_called_once_with(0.6)
        img.updateMask.assert_called_once_with(mask_result)
        assert result is masked_img
