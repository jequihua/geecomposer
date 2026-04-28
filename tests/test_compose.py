"""Tests for compose() orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from geecomposer.exceptions import (
    DatasetNotSupportedError,
    GeeComposerError,
    InvalidReducerError,
)


POLYGON_GEOJSON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-3.80, 40.40],
            [-3.80, 40.55],
            [-3.55, 40.55],
            [-3.55, 40.40],
            [-3.80, 40.40],
        ]
    ],
}


def _make_mock_s2_module() -> MagicMock:
    """Build a mock that mimics the sentinel2 dataset module interface."""
    mod = MagicMock()
    mod.get_collection_id.return_value = "COPERNICUS/S2_SR_HARMONIZED"
    return mod


def _make_mock_s1_module() -> MagicMock:
    """Build a mock that mimics the sentinel1 dataset module interface."""
    mod = MagicMock()
    mod.get_collection_id.return_value = "COPERNICUS/S1_GRD"
    return mod


def _make_mock_s1_float_module() -> MagicMock:
    """Build a mock that mimics the sentinel1_float dataset module interface."""
    mod = MagicMock()
    mod.get_collection_id.return_value = "COPERNICUS/S1_GRD_FLOAT"
    return mod


def _all_mock_modules() -> dict:
    """Build a dict of all mock dataset modules."""
    return {
        "sentinel1": _make_mock_s1_module(),
        "sentinel1_float": _make_mock_s1_float_module(),
        "sentinel2": _make_mock_s2_module(),
    }


# ---------------------------------------------------------------------------
# Input validation through compose()
# ---------------------------------------------------------------------------


class TestComposeInputValidation:
    def test_missing_aoi_raises(self) -> None:
        from geecomposer.compose import compose

        mock_s2 = _make_mock_s2_module()
        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            with pytest.raises(GeeComposerError, match="aoi is required"):
                compose(dataset="sentinel2", start="2024-01-01", end="2024-12-31")

    @patch("geecomposer.compose.to_ee_geometry")
    def test_missing_dates_raises(self, mock_to_ee: MagicMock) -> None:
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            with pytest.raises(GeeComposerError, match="start and end dates"):
                compose(dataset="sentinel2", aoi=POLYGON_GEOJSON)

    def test_missing_dataset_and_collection_raises(self) -> None:
        from geecomposer.compose import compose

        with pytest.raises(GeeComposerError, match="Either 'dataset' or 'collection'"):
            compose(aoi=POLYGON_GEOJSON, start="2024-01-01", end="2024-12-31")

    def test_both_dataset_and_collection_raises(self) -> None:
        from geecomposer.compose import compose

        with pytest.raises(GeeComposerError, match="not both"):
            compose(
                dataset="sentinel2",
                collection="COPERNICUS/S2_SR_HARMONIZED",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
            )

    def test_invalid_dataset_raises(self) -> None:
        from geecomposer.compose import compose

        with pytest.raises(DatasetNotSupportedError, match="landsat"):
            compose(
                dataset="landsat",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
            )

    @patch("geecomposer.compose.to_ee_geometry")
    def test_invalid_reducer_raises(self, mock_to_ee: MagicMock) -> None:
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            with pytest.raises(InvalidReducerError, match="percentile"):
                compose(
                    dataset="sentinel2",
                    aoi=POLYGON_GEOJSON,
                    start="2024-01-01",
                    end="2024-12-31",
                    reducer="percentile",
                )


# ---------------------------------------------------------------------------
# Dataset resolution
# ---------------------------------------------------------------------------


class TestDatasetResolution:
    def test_sentinel2_resolves(self) -> None:
        from geecomposer.compose import _resolve_dataset
        from geecomposer.datasets import sentinel2

        name, col_id, mod = _resolve_dataset("sentinel2", None)
        assert name == "sentinel2"
        assert col_id == "COPERNICUS/S2_SR_HARMONIZED"
        assert mod is sentinel2

    def test_sentinel1_resolves(self) -> None:
        from geecomposer.compose import _resolve_dataset
        from geecomposer.datasets import sentinel1

        name, col_id, mod = _resolve_dataset("sentinel1", None)
        assert name == "sentinel1"
        assert col_id == "COPERNICUS/S1_GRD"
        assert mod is sentinel1

    def test_sentinel1_float_resolves(self) -> None:
        from geecomposer.compose import _resolve_dataset
        from geecomposer.datasets import sentinel1_float

        name, col_id, mod = _resolve_dataset("sentinel1_float", None)
        assert name == "sentinel1_float"
        assert col_id == "COPERNICUS/S1_GRD_FLOAT"
        assert mod is sentinel1_float

    def test_raw_collection_returns_generic_loader(self) -> None:
        from geecomposer.compose import _GenericLoader, _resolve_dataset

        name, col_id, mod = _resolve_dataset(None, "LANDSAT/LC08/C02/T1_L2")
        assert name is None
        assert col_id == "LANDSAT/LC08/C02/T1_L2"
        assert isinstance(mod, _GenericLoader)

    def test_generic_loader_mask_raises(self) -> None:
        from geecomposer.compose import _GenericLoader

        loader = _GenericLoader("SOME/COLLECTION")
        with pytest.raises(GeeComposerError, match="Masking is not supported"):
            loader.apply_mask(MagicMock(), "some_mask")


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


class TestComposePipeline:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_minimal_sentinel2_pipeline(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Minimal compose: dataset + aoi + dates + reducer."""
        from geecomposer.compose import compose

        geometry = MagicMock()
        mock_to_ee.return_value = geometry

        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            result = compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                reducer="median",
            )

        mock_to_ee.assert_called_once_with(POLYGON_GEOJSON)
        mock_s2.load_collection.assert_called_once_with(
            geometry, "2024-01-01", "2024-12-31", filters=None
        )
        mock_s2.apply_mask.assert_not_called()
        mock_apply_reducer.assert_called_once_with(mock_col, "median")
        mock_image.set.assert_called_once()
        assert result is mock_image

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel2_with_mask(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Compose with cloud masking."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_masked_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_s2.apply_mask.return_value = mock_masked_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                mask="s2_cloud_score_plus",
                reducer="median",
            )

        mock_s2.apply_mask.assert_called_once_with(mock_col, mask="s2_cloud_score_plus")
        mock_apply_reducer.assert_called_once_with(mock_masked_col, "median")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel2_with_select(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Compose with band selection."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_selected = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_col.select.return_value = mock_selected

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                select="B4",
                reducer="median",
            )

        mock_col.select.assert_called_once_with(["B4"])
        mock_apply_reducer.assert_called_once_with(mock_selected, "median")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel2_with_transform(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Compose with a per-image transform."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_mapped = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_mapped

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        transform_fn = MagicMock()
        transform_fn.__name__ = "ndvi"

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                transform=transform_fn,
                reducer="max",
            )

        mock_col.map.assert_called_once_with(transform_fn)
        mock_apply_reducer.assert_called_once_with(mock_mapped, "max")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel2_with_preprocess(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Compose with a custom preprocessing callable."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_preprocessed = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_preprocessed

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        preprocess_fn = MagicMock()

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                preprocess=preprocess_fn,
                reducer="median",
            )

        mock_col.map.assert_called_once_with(preprocess_fn)

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_pipeline_order_mask_select_preprocess_transform(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Full pipeline: mask -> select -> preprocess -> transform -> reduce."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()

        mock_s2 = _make_mock_s2_module()
        col_raw = MagicMock(name="col_raw")
        col_masked = MagicMock(name="col_masked")
        col_selected = MagicMock(name="col_selected")
        col_preprocessed = MagicMock(name="col_preprocessed")
        col_transformed = MagicMock(name="col_transformed")

        mock_s2.load_collection.return_value = col_raw
        mock_s2.apply_mask.return_value = col_masked
        col_masked.select.return_value = col_selected
        col_selected.map.return_value = col_preprocessed
        col_preprocessed.map.return_value = col_transformed

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        preprocess_fn = MagicMock(name="preprocess_fn")
        transform_fn = MagicMock(name="transform_fn")
        transform_fn.__name__ = "custom"

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                mask="s2_cloud_score_plus",
                select=["B4", "B8"],
                preprocess=preprocess_fn,
                transform=transform_fn,
                reducer="max",
            )

        mock_s2.apply_mask.assert_called_once_with(col_raw, mask="s2_cloud_score_plus")
        col_masked.select.assert_called_once_with(["B4", "B8"])
        col_selected.map.assert_called_once_with(preprocess_fn)
        col_preprocessed.map.assert_called_once_with(transform_fn)
        mock_apply_reducer.assert_called_once_with(col_transformed, "max")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_metadata_attached(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Metadata properties are attached to the output image."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                reducer="median",
                metadata={"project": "test"},
            )

        mock_image.set.assert_called_once()
        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:dataset"] == "sentinel2"
        assert props["geecomposer:collection"] == "COPERNICUS/S2_SR_HARMONIZED"
        assert props["geecomposer:start"] == "2024-01-01"
        assert props["geecomposer:end"] == "2024-12-31"
        assert props["geecomposer:reducer"] == "median"
        assert props["geecomposer:user:project"] == "test"


# ---------------------------------------------------------------------------
# Sentinel-1 compose path
# ---------------------------------------------------------------------------


class TestComposeSentinel1Pipeline:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_minimal_sentinel1_pipeline(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Minimal S1 compose: dataset + aoi + dates + reducer."""
        from geecomposer.compose import compose

        geometry = MagicMock()
        mock_to_ee.return_value = geometry

        mock_s1 = _make_mock_s1_module()
        mock_col = MagicMock()
        mock_s1.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {
            "sentinel1": mock_s1, "sentinel2": _make_mock_s2_module()
        }):
            result = compose(
                dataset="sentinel1",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                reducer="median",
            )

        mock_to_ee.assert_called_once_with(POLYGON_GEOJSON)
        mock_s1.load_collection.assert_called_once_with(
            geometry, "2024-01-01", "2024-12-31", filters=None
        )
        mock_apply_reducer.assert_called_once_with(mock_col, "median")
        assert result is mock_image

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_with_filters_and_transform(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """S1 compose with polarization filters and a custom transform."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s1 = _make_mock_s1_module()
        mock_col = MagicMock()
        mock_mapped = MagicMock()
        mock_s1.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_mapped

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        transform_fn = MagicMock()
        transform_fn.__name__ = "vv_vh_ratio"
        s1_filters = {"instrumentMode": "IW", "polarizations": ["VV", "VH"]}

        with patch("geecomposer.compose._DATASET_MODULES", {
            "sentinel1": mock_s1, "sentinel2": _make_mock_s2_module()
        }):
            compose(
                dataset="sentinel1",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                filters=s1_filters,
                transform=transform_fn,
                reducer="median",
            )

        mock_s1.load_collection.assert_called_once()
        call_filters = mock_s1.load_collection.call_args[1]["filters"]
        assert call_filters == s1_filters
        mock_col.map.assert_called_once_with(transform_fn)
        mock_apply_reducer.assert_called_once_with(mock_mapped, "median")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_mask_raises(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """S1 compose with mask= should raise since S1 has no mask presets."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s1 = _make_mock_s1_module()
        mock_col = MagicMock()
        mock_s1.load_collection.return_value = mock_col
        mock_s1.apply_mask.side_effect = GeeComposerError("No masking presets")

        with patch("geecomposer.compose._DATASET_MODULES", {
            "sentinel1": mock_s1, "sentinel2": _make_mock_s2_module()
        }):
            with pytest.raises(GeeComposerError, match="No masking presets"):
                compose(
                    dataset="sentinel1",
                    aoi=POLYGON_GEOJSON,
                    start="2024-01-01",
                    end="2024-12-31",
                    mask="some_mask",
                    reducer="median",
                )

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_metadata_has_correct_dataset(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """S1 metadata records sentinel1 dataset and S1_GRD collection."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_s1 = _make_mock_s1_module()
        mock_col = MagicMock()
        mock_s1.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {
            "sentinel1": mock_s1, "sentinel2": _make_mock_s2_module()
        }):
            compose(
                dataset="sentinel1",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                reducer="median",
            )

        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:dataset"] == "sentinel1"
        assert props["geecomposer:collection"] == "COPERNICUS/S1_GRD"


# ---------------------------------------------------------------------------
# Sentinel-1 float compose path
# ---------------------------------------------------------------------------


class TestComposeSentinel1FloatPipeline:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_float_pipeline(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Minimal S1 float compose: dataset + aoi + dates + reducer."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1f = mods["sentinel1_float"]
        mock_col = MagicMock()
        mock_s1f.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            result = compose(
                dataset="sentinel1_float",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                reducer="median",
            )

        mock_s1f.load_collection.assert_called_once()
        mock_apply_reducer.assert_called_once_with(mock_col, "median")
        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:dataset"] == "sentinel1_float"
        assert props["geecomposer:collection"] == "COPERNICUS/S1_GRD_FLOAT"
        assert result is mock_image

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_float_with_expression_transform(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Linear-unit VH/VV ratio via expression_transform on float data."""
        from geecomposer.compose import compose
        from geecomposer.transforms.expressions import expression_transform

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1f = mods["sentinel1_float"]
        mock_col = MagicMock()
        mock_mapped = MagicMock()
        mock_s1f.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_mapped

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        vh_vv_ratio = expression_transform(
            expression="vh / vv",
            band_map={"vh": "VH", "vv": "VV"},
            name="vh_vv_ratio",
        )

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            compose(
                dataset="sentinel1_float",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                filters={"polarizations": ["VV", "VH"]},
                transform=vh_vv_ratio,
                reducer="median",
            )

        mock_col.map.assert_called_once_with(vh_vv_ratio)
        mock_apply_reducer.assert_called_once_with(mock_mapped, "median")
        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:transform"] == "expression_transform('vh_vv_ratio')"

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_db_unchanged_after_float_added(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Adding float preset does not change dB sentinel1 behavior."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1_db = mods["sentinel1"]
        mock_col = MagicMock()
        mock_s1_db.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            compose(
                dataset="sentinel1",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                reducer="median",
            )

        mock_s1_db.load_collection.assert_called_once()
        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:dataset"] == "sentinel1"
        assert props["geecomposer:collection"] == "COPERNICUS/S1_GRD"


# ---------------------------------------------------------------------------
# Raw collection path
# ---------------------------------------------------------------------------


class TestComposeRawCollection:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.ee")
    @patch("geecomposer.compose.apply_reducer")
    def test_raw_collection_loads_and_reduces(
        self,
        mock_apply_reducer: MagicMock,
        mock_ee: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        result = compose(
            collection="LANDSAT/LC08/C02/T1_L2",
            aoi=POLYGON_GEOJSON,
            start="2024-01-01",
            end="2024-12-31",
            reducer="median",
        )

        mock_ee.ImageCollection.assert_called_once_with("LANDSAT/LC08/C02/T1_L2")
        mock_apply_reducer.assert_called_once()
        assert result is mock_image

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.ee")
    def test_raw_collection_with_mask_raises(
        self,
        mock_ee: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mock_col = MagicMock()
        mock_ee.ImageCollection.return_value = mock_col
        mock_col.filterBounds.return_value = mock_col
        mock_col.filterDate.return_value = mock_col

        with pytest.raises(GeeComposerError, match="Masking is not supported"):
            compose(
                collection="LANDSAT/LC08/C02/T1_L2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                mask="some_mask",
                reducer="median",
            )


# ---------------------------------------------------------------------------
# Transform metadata with real built-in factories
# ---------------------------------------------------------------------------


class TestTransformMetadata:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_ndvi_transform_metadata_name(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """compose() with ndvi() records 'ndvi' in metadata, not '_fn'."""
        from geecomposer.compose import compose
        from geecomposer.transforms.indices import ndvi

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                transform=ndvi(),
                reducer="max",
            )

        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:transform"] == "ndvi"

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_select_band_transform_metadata_name(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """compose() with select_band() records a descriptive name."""
        from geecomposer.compose import compose
        from geecomposer.transforms.basic import select_band

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                transform=select_band("B4"),
                reducer="median",
            )

        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:transform"] == "select_band('B4')"

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_expression_transform_metadata_name(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """compose() with expression_transform() records a descriptive name."""
        from geecomposer.compose import compose
        from geecomposer.transforms.expressions import expression_transform

        mock_to_ee.return_value = MagicMock()
        mock_s2 = _make_mock_s2_module()
        mock_col = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_col.map.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", {"sentinel2": mock_s2}):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                transform=expression_transform(
                    expression="vv / vh",
                    band_map={"vv": "VV", "vh": "VH"},
                    name="ratio",
                ),
                reducer="median",
            )

        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:transform"] == "expression_transform('ratio')"


# ---------------------------------------------------------------------------
# Count reducer compose paths
# ---------------------------------------------------------------------------


class TestComposeCountReducer:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel2_count_with_mask(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """S2 count with masking: counts clear observations after mask."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s2 = mods["sentinel2"]
        mock_col = MagicMock()
        mock_masked = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_s2.apply_mask.return_value = mock_masked

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                mask="s2_cloud_score_plus",
                reducer="count",
            )

        mock_s2.apply_mask.assert_called_once()
        mock_apply_reducer.assert_called_once_with(mock_masked, "count")
        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:reducer"] == "count"

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_float_count(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """S1 float count: counts contributing acquisitions."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1f = mods["sentinel1_float"]
        mock_col = MagicMock()
        mock_s1f.load_collection.return_value = mock_col

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            compose(
                dataset="sentinel1_float",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                filters={"polarizations": ["VV", "VH"]},
                reducer="count",
            )

        mock_apply_reducer.assert_called_once_with(mock_col, "count")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel2_ndvi_count_with_mask_and_transform(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """S2 NDVI count with masking: counts valid NDVI observations."""
        from geecomposer.compose import compose
        from geecomposer.transforms.indices import ndvi

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s2 = mods["sentinel2"]
        mock_col = MagicMock()
        mock_masked = MagicMock()
        mock_transformed = MagicMock()
        mock_s2.load_collection.return_value = mock_col
        mock_s2.apply_mask.return_value = mock_masked
        mock_masked.map.return_value = mock_transformed

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        transform = ndvi()

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            compose(
                dataset="sentinel2",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                mask="s2_cloud_score_plus",
                transform=transform,
                reducer="count",
            )

        mock_s2.apply_mask.assert_called_once()
        mock_masked.map.assert_called_once_with(transform)
        mock_apply_reducer.assert_called_once_with(mock_transformed, "count")
