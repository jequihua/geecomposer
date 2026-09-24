"""Regression cases for inherited audit A01-A05."""

from unittest.mock import MagicMock, patch
import importlib
import math

import ee
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from geecomposer import compose_yearly
from geecomposer.aoi import read_vector_file
from geecomposer.datasets._sentinel1_filters import validate_filters
from geecomposer.datasets.sentinel1_preprocessing import _gamma_map_estimate, gamma_map
from geecomposer.exceptions import GeeComposerError, InvalidAOIError
from geecomposer.validation import validate_dates
from rao_raster_double import Raster


@pytest.mark.parametrize("years", [[True], [0], [9999], [2024, 2024], [2024, False]])
def test_invalid_years_never_compose(years):
    with patch("geecomposer.grouping.compose") as call:
        with pytest.raises(GeeComposerError):
            compose_yearly(years)
        call.assert_not_called()


def test_year_boundaries_are_canonical():
    with patch("geecomposer.grouping.compose") as call:
        compose_yearly([1, 9998])
        assert call.call_args_list[0].kwargs == {
            "start": "0001-01-01",
            "end": "0002-01-01",
        }
        assert call.call_args_list[1].kwargs == {
            "start": "9998-01-01",
            "end": "9999-01-01",
        }


@pytest.mark.parametrize(
    "start,end",
    [
        (None, "2024-01-01"),
        ("2024-01-01", "2024-01-01"),
        ("2025-01-01", "2024-01-01"),
        ("20240201", "2024-03-01"),
        ("2023-02-29", "2024-01-01"),
        (1, 2),
    ],
)
def test_invalid_dates(start, end):
    with pytest.raises(GeeComposerError):
        validate_dates(start, end)


@pytest.mark.parametrize(
    "filters",
    [
        [],
        False,
        {"unused": 1},
        {1: 2},
        {"instrumentMode": "XX"},
        {"orbitPass": "ascending"},
        {"polarizations": ["VV", "VV"]},
        {"polarizations": ["XY"]},
    ],
)
def test_invalid_radar_filters(filters):
    with pytest.raises(GeeComposerError):
        validate_filters(filters)


@pytest.mark.parametrize("filters", [[], False, {"ignored": 1}])
def test_other_loaders_reject_filters_before_ee(filters):
    from geecomposer.datasets import sentinel2

    mod = importlib.import_module("geecomposer.compose")
    with patch.object(sentinel2, "ee") as s2, patch.object(mod, "ee") as generic:
        for loader in [sentinel2, mod._GenericLoader("test")]:
            with pytest.raises(GeeComposerError):
                loader.load_collection(None, "2024-01-01", "2025-01-01", filters)
        s2.ImageCollection.assert_not_called()
        generic.ImageCollection.assert_not_called()


def test_missing_local_crs_fails(tmp_path):
    path = tmp_path / "aoi.shp"
    path.touch()
    with patch(
        "geopandas.read_file", return_value=gpd.GeoDataFrame(geometry=[Point(1, 2)])
    ):
        with pytest.raises(InvalidAOIError, match="CRS"):
            read_vector_file(path)


def test_actual_provenance_is_normalized_and_not_user_overridden():
    mod = importlib.import_module("geecomposer.compose")
    loader = MagicMock()
    image = MagicMock()
    with (
        patch.object(mod, "_DATASET_MODULES", {"sentinel1_float": loader}),
        patch.object(mod, "to_ee_geometry"),
        patch.object(mod, "apply_reducer", return_value=image),
    ):
        mod.compose(
            dataset="sentinel1_float",
            aoi={},
            start="2024-01-01",
            end="2025-01-01",
            reducer=" MEAN ",
            preprocess=gamma_map(5),
            select=["VV"],
            metadata={"reducer": "fake"},
        )
    props = image.set.call_args.args[0]
    assert props["geecomposer:reducer"] == "mean"
    assert props["geecomposer:preprocess"] == "gamma_map(kernel_size=5)"
    assert props["geecomposer:filters"] == {"instrumentMode": "IW"}
    assert props["geecomposer:select"] == ["VV"]
    assert props["geecomposer:user:reducer"] == "fake"


@pytest.mark.parametrize(
    "x,mean,cv", [(2, 2, 0.6), (0.01, 0.03, 0.5), (3, 2, 0.1), (3, 2, 0.9), (2, 2, 0)]
)
def test_gamma_numeric_map_root_and_scale(x, mean, cv):
    cu = 1 / math.sqrt(5)
    if cv <= cu:
        expected = mean
    elif cv >= math.sqrt(2) * cu:
        expected = x
    else:
        # Independent positive root of the MAP stationarity polynomial.
        alpha = (1 + 1 / 5) / (cv**2 - 1 / 5)
        expected = max(np.roots([alpha, -mean * (alpha - 6), -5 * mean * x]))
    for factor in (1e-3, 1, 10, 1000):
        result = _gamma_map_estimate(
            Raster(x * factor), Raster(mean * factor), Raster(cv * mean * factor)
        )
        assert result.data.item() == pytest.approx(expected * factor, rel=1e-12)
        assert result.valid.item()


def test_gamma_zero_and_mask_preservation():
    result = _gamma_map_estimate(
        Raster([0, 2], mask=[[[True, False]]]), Raster([0, 2]), Raster([0, 0])
    )
    assert result.data[0, 0, 0] == 0
    assert np.isfinite(result.data).all()
    assert result.valid.tolist() == [[[True, False]]]


def test_gamma_real_graph(real_ee):
    from ee_graph import Graph

    result = gamma_map(7)(ee.Image.constant([1, 30]).rename(["VV", "angle"]))
    graph = result.serialize()
    assert "Image.where" in graph
    assert "Image.updateMask" in graph
    dependencies = Graph(result)
    (kernel,) = dependencies.calls("Kernel.square")
    assert dependencies.constant(kernel["radius"]) == 3
    assert dependencies.constant(kernel["units"]) == "pixels"
    assert "ImageCollection.reduce" not in graph


@pytest.mark.parametrize(
    "dataset,collection,filters",
    [
        ("sentinel1", None, {"orbitPass": "bad"}),
        ("sentinel1_float", None, False),
        ("sentinel2", None, {"unused": 1}),
        (None, "custom", []),
    ],
)
def test_filter_validation_precedes_aoi_reads(dataset, collection, filters):
    mod = importlib.import_module("geecomposer.compose")
    with patch.object(
        mod, "to_ee_geometry", side_effect=AssertionError("AOI read")
    ) as read:
        with pytest.raises(GeeComposerError):
            mod.compose(
                dataset=dataset,
                collection=collection,
                aoi="missing.geojson",
                start="2020-01-01",
                end="2021-01-01",
                filters=filters,
            )
        read.assert_not_called()


@pytest.mark.parametrize(
    "start,end",
    [
        (None, "2020-01-01"),
        ("20200101", "2021-01-01"),
        ("2020-02-30", "2021-01-01"),
        ("2021-01-01", "2020-01-01"),
    ],
)
def test_date_public_exception_contracts(start, end):
    from geecomposer.seasonal import _interval

    with pytest.raises(GeeComposerError):
        validate_dates(start, end)
    with pytest.raises(ValueError):
        _interval(start, end)


def test_metadata_tracks_loader_constants(monkeypatch):
    from geecomposer.datasets import sentinel2, _sentinel1_filters
    from geecomposer.utils.metadata import build_metadata_payload

    monkeypatch.setattr(sentinel2, "_CS_PLUS_BAND", "test_score")
    monkeypatch.setattr(sentinel2, "_CS_PLUS_THRESHOLD", 0.75)
    props = build_metadata_payload(
        None, None, None, None, "mean", None, mask="s2_cloud_score_plus"
    )
    assert props["geecomposer:mask_band"] == "test_score"
    assert props["geecomposer:mask_threshold"] == 0.75
    monkeypatch.setattr(_sentinel1_filters, "DEFAULT_INSTRUMENT_MODE", "EW")
    mod = importlib.import_module("geecomposer.compose")
    with (
        patch.object(mod, "_DATASET_MODULES", {"sentinel1": MagicMock()}),
        patch.object(mod, "to_ee_geometry"),
        patch.object(mod, "apply_reducer") as reduce,
    ):
        mod.compose(dataset="sentinel1", aoi={}, start="2020-01-01", end="2021-01-01")
    assert reduce.return_value.set.call_args.args[0]["geecomposer:filters"] == {
        "instrumentMode": "EW"
    }
