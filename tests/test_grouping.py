"""Tests for grouped composition helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

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


class TestComposeYearly:
    @patch("geecomposer.grouping.compose")
    def test_delegates_to_compose_per_year(self, mock_compose: MagicMock) -> None:
        from geecomposer.grouping import compose_yearly

        img_2023 = MagicMock(name="img_2023")
        img_2024 = MagicMock(name="img_2024")
        mock_compose.side_effect = [img_2023, img_2024]

        result = compose_yearly(
            years=[2023, 2024],
            dataset="sentinel2",
            aoi=POLYGON_GEOJSON,
            reducer="max",
        )

        assert mock_compose.call_count == 2
        calls = mock_compose.call_args_list

        assert calls[0] == call(
            start="2023-01-01", end="2024-01-01",
            dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="max",
        )
        assert calls[1] == call(
            start="2024-01-01", end="2025-01-01",
            dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="max",
        )

        assert result == {2023: img_2023, 2024: img_2024}

    @patch("geecomposer.grouping.compose")
    def test_returns_dict_keyed_by_year(self, mock_compose: MagicMock) -> None:
        from geecomposer.grouping import compose_yearly

        mock_compose.return_value = MagicMock()

        result = compose_yearly(
            years=range(2020, 2023),
            dataset="sentinel2",
            aoi=POLYGON_GEOJSON,
            reducer="median",
        )

        assert isinstance(result, dict)
        assert set(result.keys()) == {2020, 2021, 2022}
        assert mock_compose.call_count == 3

    @patch("geecomposer.grouping.compose")
    def test_single_year(self, mock_compose: MagicMock) -> None:
        from geecomposer.grouping import compose_yearly

        img = MagicMock()
        mock_compose.return_value = img

        result = compose_yearly(
            years=[2024],
            dataset="sentinel1",
            aoi=POLYGON_GEOJSON,
            reducer="median",
            filters={"polarizations": ["VV"]},
        )

        assert result == {2024: img}
        mock_compose.assert_called_once_with(
            start="2024-01-01", end="2025-01-01",
            dataset="sentinel1", aoi=POLYGON_GEOJSON, reducer="median",
            filters={"polarizations": ["VV"]},
        )

    @patch("geecomposer.grouping.compose")
    def test_generator_input_works(self, mock_compose: MagicMock) -> None:
        """Generators must not be silently exhausted during validation."""
        from geecomposer.grouping import compose_yearly

        img_2023 = MagicMock(name="img_2023")
        img_2024 = MagicMock(name="img_2024")
        mock_compose.side_effect = [img_2023, img_2024]

        years_gen = (y for y in [2023, 2024])
        result = compose_yearly(
            years=years_gen,
            dataset="sentinel2",
            aoi=POLYGON_GEOJSON,
            reducer="median",
        )

        assert result == {2023: img_2023, 2024: img_2024}
        assert mock_compose.call_count == 2

    @patch("geecomposer.grouping.compose")
    def test_forwards_all_compose_kwargs(self, mock_compose: MagicMock) -> None:
        from geecomposer.grouping import compose_yearly

        mock_compose.return_value = MagicMock()
        from geecomposer.transforms.indices import ndvi

        transform = ndvi()

        compose_yearly(
            years=[2024],
            dataset="sentinel2",
            aoi=POLYGON_GEOJSON,
            reducer="max",
            mask="s2_cloud_score_plus",
            transform=transform,
            select=["B4", "B8"],
        )

        kwargs = mock_compose.call_args[1]
        assert kwargs["mask"] == "s2_cloud_score_plus"
        assert kwargs["transform"] is transform
        assert kwargs["select"] == ["B4", "B8"]


class TestComposeYearlyValidation:
    def test_empty_years_raises(self) -> None:
        from geecomposer.grouping import compose_yearly

        with pytest.raises(GeeComposerError, match="years must not be empty"):
            compose_yearly(years=[], dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="median")

    def test_non_integer_year_raises(self) -> None:
        from geecomposer.grouping import compose_yearly

        with pytest.raises(GeeComposerError, match="years\\[0\\] must be an integer"):
            compose_yearly(
                years=["2024"], dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="median"
            )

    def test_start_in_kwargs_raises(self) -> None:
        from geecomposer.grouping import compose_yearly

        with pytest.raises(GeeComposerError, match="Do not pass 'start' or 'end'"):
            compose_yearly(
                years=[2024], start="2024-01-01",
                dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="median",
            )

    def test_end_in_kwargs_raises(self) -> None:
        from geecomposer.grouping import compose_yearly

        with pytest.raises(GeeComposerError, match="Do not pass 'start' or 'end'"):
            compose_yearly(
                years=[2024], end="2024-12-31",
                dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="median",
            )

    def test_start_none_in_kwargs_still_raises(self) -> None:
        """Even start=None is rejected — strict key-presence check."""
        from geecomposer.grouping import compose_yearly

        with pytest.raises(GeeComposerError, match="Do not pass 'start' or 'end'"):
            compose_yearly(
                years=[2024], start=None,
                dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="median",
            )

    def test_non_iterable_years_raises(self) -> None:
        from geecomposer.grouping import compose_yearly

        with pytest.raises(GeeComposerError, match="years must be an iterable"):
            compose_yearly(
                years=2024, dataset="sentinel2", aoi=POLYGON_GEOJSON, reducer="median"
            )
