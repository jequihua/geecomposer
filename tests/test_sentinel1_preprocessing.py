"""Tests for Sentinel-1 mono-temporal Gamma MAP speckle filtering."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from geecomposer.exceptions import GeeComposerError


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
    mod = MagicMock()
    mod.get_collection_id.return_value = "COPERNICUS/S2_SR_HARMONIZED"
    return mod


def _make_mock_s1_module() -> MagicMock:
    mod = MagicMock()
    mod.get_collection_id.return_value = "COPERNICUS/S1_GRD"
    return mod


def _make_mock_s1_float_module() -> MagicMock:
    mod = MagicMock()
    mod.get_collection_id.return_value = "COPERNICUS/S1_GRD_FLOAT"
    return mod


def _all_mock_modules() -> dict:
    return {
        "sentinel1": _make_mock_s1_module(),
        "sentinel1_float": _make_mock_s1_float_module(),
        "sentinel2": _make_mock_s2_module(),
    }


# ---------------------------------------------------------------------------
# Factory validation
# ---------------------------------------------------------------------------


class TestGammaMapFactoryValidation:
    def test_default_kernel_size_is_seven(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        fn = gamma_map()
        assert fn.__name__ == "gamma_map(kernel_size=7)"

    def test_custom_odd_kernel_size_accepted(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        fn = gamma_map(kernel_size=5)
        assert callable(fn)
        assert fn.__name__ == "gamma_map(kernel_size=5)"

    def test_kernel_size_one_accepted(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        fn = gamma_map(kernel_size=1)
        assert callable(fn)

    def test_zero_kernel_size_raises(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        with pytest.raises(GeeComposerError, match="positive odd integer"):
            gamma_map(kernel_size=0)

    def test_negative_kernel_size_raises(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        with pytest.raises(GeeComposerError, match="positive odd integer"):
            gamma_map(kernel_size=-3)

    def test_even_kernel_size_raises(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        with pytest.raises(GeeComposerError, match="even"):
            gamma_map(kernel_size=6)

    def test_float_kernel_size_raises(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        with pytest.raises(GeeComposerError, match="positive odd integer"):
            gamma_map(kernel_size=7.0)  # type: ignore[arg-type]

    def test_string_kernel_size_raises(self) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        with pytest.raises(GeeComposerError, match="positive odd integer"):
            gamma_map(kernel_size="7")  # type: ignore[arg-type]

    def test_bool_kernel_size_raises(self) -> None:
        """Booleans must be rejected even though bool is a subclass of int."""
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        with pytest.raises(GeeComposerError, match="positive odd integer"):
            gamma_map(kernel_size=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helper behavior on a mocked EE image
# ---------------------------------------------------------------------------


class TestGammaMapBehavior:
    @patch("geecomposer.datasets.sentinel1_preprocessing.ee")
    def test_returns_callable_with_one_arg(self, mock_ee: MagicMock) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        fn = gamma_map()
        # The returned callable signature is `ee.Image -> ee.Image`.
        assert callable(fn)
        assert fn.__code__.co_argcount == 1

    @patch("geecomposer.datasets.sentinel1_preprocessing.ee")
    def test_preserves_angle_band_via_bandnames_remove(
        self, mock_ee: MagicMock
    ) -> None:
        """The filter must remove ``angle`` from the band-name list before
        applying any neighbourhood operation, leaving angle untouched in
        the output stack via overwrite=True semantics on addBands."""
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        image = MagicMock(name="image")
        band_names = MagicMock(name="band_names")
        image.bandNames.return_value = band_names

        fn = gamma_map()
        fn(image)

        image.bandNames.assert_called_once_with()
        band_names.remove.assert_called_once_with("angle")

    @patch("geecomposer.datasets.sentinel1_preprocessing.ee")
    def test_uses_square_kernel_with_half_kernel_size_radius(
        self, mock_ee: MagicMock
    ) -> None:
        """Kernel radius must be ``kernel_size / 2`` in pixel units, matching
        the legacy SAR ARD reference behavior."""
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        image = MagicMock(name="image")
        fn = gamma_map(kernel_size=7)
        fn(image)

        mock_ee.Kernel.square.assert_called_with(3.5, "pixels")

    @patch("geecomposer.datasets.sentinel1_preprocessing.ee")
    def test_custom_kernel_size_propagates_to_kernel(
        self, mock_ee: MagicMock
    ) -> None:
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        image = MagicMock(name="image")
        fn = gamma_map(kernel_size=5)
        fn(image)

        mock_ee.Kernel.square.assert_called_with(2.5, "pixels")

    @patch("geecomposer.datasets.sentinel1_preprocessing.ee")
    def test_returns_addbands_result_with_overwrite(
        self, mock_ee: MagicMock
    ) -> None:
        """The filter must return ``image.addBands(filtered, ..., True)``
        so the original backscatter bands are overwritten while the
        ``angle`` band is preserved."""
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        image = MagicMock(name="image")
        out_image = MagicMock(name="out_image")
        image.addBands.return_value = out_image

        fn = gamma_map()
        result = fn(image)

        assert result is out_image
        image.addBands.assert_called_once()
        kwargs = image.addBands.call_args.kwargs
        assert kwargs.get("overwrite") is True

    @patch("geecomposer.datasets.sentinel1_preprocessing.ee")
    def test_uses_mean_stddev_combined_reducer(
        self, mock_ee: MagicMock
    ) -> None:
        """Gamma MAP needs both local mean and local std-dev; verify the
        combined reducer is constructed."""
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        image = MagicMock(name="image")
        fn = gamma_map()
        fn(image)

        mock_ee.Reducer.mean.assert_called()
        mock_ee.Reducer.stdDev.assert_called()
        # combine(...) is called on the mean reducer with the stddev reducer.
        mean_reducer = mock_ee.Reducer.mean.return_value
        mean_reducer.combine.assert_called_once()
        combine_kwargs = mean_reducer.combine.call_args.kwargs
        assert combine_kwargs.get("sharedInputs") is True


# ---------------------------------------------------------------------------
# compose() integration through the existing preprocess slot
# ---------------------------------------------------------------------------


class TestComposeWithGammaMap:
    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_sentinel1_float_with_gamma_map_preprocess(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """compose(..., dataset='sentinel1_float', preprocess=gamma_map())
        must route the helper through the existing preprocess slot —
        i.e. col.map(gamma_map_fn)."""
        from geecomposer.compose import compose
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1f = mods["sentinel1_float"]

        col_raw = MagicMock(name="col_raw")
        col_preprocessed = MagicMock(name="col_preprocessed")
        mock_s1f.load_collection.return_value = col_raw
        col_raw.map.return_value = col_preprocessed

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        gm = gamma_map()

        with patch("geecomposer.compose._DATASET_MODULES", mods):
            compose(
                dataset="sentinel1_float",
                aoi=POLYGON_GEOJSON,
                start="2024-01-01",
                end="2024-12-31",
                preprocess=gm,
                reducer="median",
                filters={"polarizations": ["VV", "VH"]},
            )

        col_raw.map.assert_called_once_with(gm)
        mock_apply_reducer.assert_called_once_with(col_preprocessed, "median")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_gamma_map_then_transform_then_reducer(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """Pipeline order on the float path: load -> preprocess (gamma_map)
        -> transform -> reducer."""
        from geecomposer.compose import compose
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map
        from geecomposer.transforms.expressions import expression_transform

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1f = mods["sentinel1_float"]

        col_raw = MagicMock(name="col_raw")
        col_preprocessed = MagicMock(name="col_preprocessed")
        col_transformed = MagicMock(name="col_transformed")
        mock_s1f.load_collection.return_value = col_raw
        col_raw.map.return_value = col_preprocessed
        col_preprocessed.map.return_value = col_transformed

        mock_image = MagicMock()
        mock_apply_reducer.return_value = mock_image
        mock_image.set.return_value = mock_image

        gm = gamma_map()
        vh_vv = expression_transform(
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
                preprocess=gm,
                transform=vh_vv,
                reducer="median",
            )

        col_raw.map.assert_called_once_with(gm)
        col_preprocessed.map.assert_called_once_with(vh_vv)
        mock_apply_reducer.assert_called_once_with(col_transformed, "median")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_gamma_map_metadata_records_preprocess_unset_but_transform_set(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """The preprocess slot does not feed the metadata transform name —
        verify gamma_map does not silently leak into transform metadata."""
        from geecomposer.compose import compose
        from geecomposer.datasets.sentinel1_preprocessing import gamma_map

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1f = mods["sentinel1_float"]
        mock_col = MagicMock()
        mock_col.map.return_value = mock_col
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
                preprocess=gamma_map(),
                reducer="median",
            )

        props = mock_image.set.call_args[0][0]
        assert props["geecomposer:dataset"] == "sentinel1_float"
        assert props["geecomposer:transform"] == ""


# ---------------------------------------------------------------------------
# Sentinel-1 dB preset must remain unchanged
# ---------------------------------------------------------------------------


class TestSentinel1DbPathUnchanged:
    def test_db_collection_id_unchanged(self) -> None:
        from geecomposer.datasets.sentinel1 import COLLECTION_ID

        assert COLLECTION_ID == "COPERNICUS/S1_GRD"

    def test_db_module_does_not_export_gamma_map(self) -> None:
        """gamma_map lives in sentinel1_preprocessing, not in sentinel1.py.
        The dB path must not auto-apply or expose speckle filtering."""
        from geecomposer.datasets import sentinel1

        assert not hasattr(sentinel1, "gamma_map")

    def test_float_module_does_not_auto_apply_gamma_map(self) -> None:
        """gamma_map must be an explicit, opt-in helper. Importing the
        float dataset module does not bind a default speckle filter."""
        from geecomposer.datasets import sentinel1_float

        assert not hasattr(sentinel1_float, "gamma_map")

    @patch("geecomposer.compose.to_ee_geometry")
    @patch("geecomposer.compose.apply_reducer")
    def test_db_compose_path_does_not_call_map(
        self,
        mock_apply_reducer: MagicMock,
        mock_to_ee: MagicMock,
    ) -> None:
        """A sentinel1 (dB) compose without preprocess/transform must not
        invoke col.map() — proving no speckle filter sneaks in."""
        from geecomposer.compose import compose

        mock_to_ee.return_value = MagicMock()
        mods = _all_mock_modules()
        mock_s1 = mods["sentinel1"]
        mock_col = MagicMock()
        mock_s1.load_collection.return_value = mock_col

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

        mock_col.map.assert_not_called()
