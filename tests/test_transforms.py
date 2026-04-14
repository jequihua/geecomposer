"""Tests for transform factory behavior."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from geecomposer.transforms.basic import normalized_difference, select_band
from geecomposer.transforms.expressions import expression_transform
from geecomposer.transforms.indices import ndvi


# ---------------------------------------------------------------------------
# select_band
# ---------------------------------------------------------------------------


class TestSelectBand:
    def test_returns_callable(self) -> None:
        fn = select_band("B4")
        assert callable(fn)

    def test_calls_select_on_image(self) -> None:
        fn = select_band("B4")
        img = MagicMock()
        selected = MagicMock()
        img.select.return_value = selected

        result = fn(img)
        img.select.assert_called_once_with("B4")
        assert result is selected

    def test_renames_when_name_given(self) -> None:
        fn = select_band("B4", name="red")
        img = MagicMock()
        selected = MagicMock()
        renamed = MagicMock()
        img.select.return_value = selected
        selected.rename.return_value = renamed

        result = fn(img)
        img.select.assert_called_once_with("B4")
        selected.rename.assert_called_once_with("red")
        assert result is renamed

    def test_empty_band_raises(self) -> None:
        with pytest.raises(ValueError, match="band must be a non-empty string"):
            select_band("")

    def test_non_string_band_raises(self) -> None:
        with pytest.raises(ValueError):
            select_band(123)


# ---------------------------------------------------------------------------
# normalized_difference
# ---------------------------------------------------------------------------


class TestNormalizedDifference:
    def test_returns_callable(self) -> None:
        fn = normalized_difference("B8", "B4", "ndvi")
        assert callable(fn)

    def test_calls_normalized_difference_on_image(self) -> None:
        fn = normalized_difference("B8", "B4", "ndvi")
        img = MagicMock()
        nd_result = MagicMock()
        renamed = MagicMock()
        img.normalizedDifference.return_value = nd_result
        nd_result.rename.return_value = renamed

        result = fn(img)
        img.normalizedDifference.assert_called_once_with(["B8", "B4"])
        nd_result.rename.assert_called_once_with("ndvi")
        assert result is renamed

    def test_empty_band1_raises(self) -> None:
        with pytest.raises(ValueError, match="band1"):
            normalized_difference("", "B4", "ndvi")

    def test_empty_band2_raises(self) -> None:
        with pytest.raises(ValueError, match="band2"):
            normalized_difference("B8", "", "ndvi")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            normalized_difference("B8", "B4", "")


# ---------------------------------------------------------------------------
# ndvi
# ---------------------------------------------------------------------------


class TestNDVI:
    def test_returns_callable(self) -> None:
        fn = ndvi()
        assert callable(fn)

    def test_default_bands(self) -> None:
        fn = ndvi()
        img = MagicMock()
        nd_result = MagicMock()
        renamed = MagicMock()
        img.normalizedDifference.return_value = nd_result
        nd_result.rename.return_value = renamed

        fn(img)
        img.normalizedDifference.assert_called_once_with(["B8", "B4"])
        nd_result.rename.assert_called_once_with("ndvi")

    def test_custom_bands(self) -> None:
        fn = ndvi(nir="B5", red="B3", name="custom_ndvi")
        img = MagicMock()
        nd_result = MagicMock()
        renamed = MagicMock()
        img.normalizedDifference.return_value = nd_result
        nd_result.rename.return_value = renamed

        fn(img)
        img.normalizedDifference.assert_called_once_with(["B5", "B3"])
        nd_result.rename.assert_called_once_with("custom_ndvi")


# ---------------------------------------------------------------------------
# expression_transform
# ---------------------------------------------------------------------------


class TestExpressionTransform:
    def test_returns_callable(self) -> None:
        fn = expression_transform(
            expression="(nir - red) / (nir + red)",
            band_map={"nir": "B8", "red": "B4"},
            name="ndvi",
        )
        assert callable(fn)

    def test_calls_expression_on_image(self) -> None:
        fn = expression_transform(
            expression="vv / vh",
            band_map={"vv": "VV", "vh": "VH"},
            name="ratio",
        )
        img = MagicMock()
        vv_band = MagicMock()
        vh_band = MagicMock()
        img.select.side_effect = lambda b: {"VV": vv_band, "VH": vh_band}[b]

        expr_result = MagicMock()
        renamed = MagicMock()
        img.expression.return_value = expr_result
        expr_result.rename.return_value = renamed

        result = fn(img)
        img.expression.assert_called_once()
        call_args = img.expression.call_args
        assert call_args[0][0] == "vv / vh"
        band_args = call_args[0][1]
        assert band_args["vv"] is vv_band
        assert band_args["vh"] is vh_band
        expr_result.rename.assert_called_once_with("ratio")
        assert result is renamed

    def test_extra_vars_merged(self) -> None:
        fn = expression_transform(
            expression="b * scale",
            band_map={"b": "B4"},
            name="scaled",
            extra_vars={"scale": 0.0001},
        )
        img = MagicMock()
        b4_band = MagicMock()
        img.select.return_value = b4_band
        expr_result = MagicMock()
        img.expression.return_value = expr_result
        expr_result.rename.return_value = MagicMock()

        fn(img)
        call_args = img.expression.call_args[0][1]
        assert call_args["b"] is b4_band
        assert call_args["scale"] == 0.0001

    def test_empty_expression_raises(self) -> None:
        with pytest.raises(ValueError, match="expression"):
            expression_transform(expression="", band_map={"a": "B1"}, name="out")

    def test_empty_band_map_raises(self) -> None:
        with pytest.raises(ValueError, match="band_map"):
            expression_transform(expression="a + b", band_map={}, name="out")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            expression_transform(expression="a", band_map={"a": "B1"}, name="")
