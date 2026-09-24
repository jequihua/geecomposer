"""Scientific ordering, exact selection and real preprocessing graphs."""

from datetime import datetime, timezone
import math

import ee
import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from geecomposer import RaoGrid, export_to_drive, nirv
from geecomposer.exceptions import GeeComposerError
from geecomposer.seasonal import (
    NoRaoDataError,
    exact_quantile,
    acquisition_groups,
    select_season,
    prepare_sentinel2_nirv,
    nirv_seasonal_rao,
    _interval,
)
from test_rao import oracle
from ee_graph import Graph


@pytest.mark.parametrize("q", [0, 0.25, 0.5, 0.89, 0.9, 1])
def test_exact_quantile(q):
    values = [1.0, 8.0, 2.0, 2.0, 5.0, 4.0, 3.0]
    assert exact_quantile(values, q) == pytest.approx(
        np.quantile(values, q, method="linear")
    )


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf]])
def test_no_quantile(values):
    with pytest.raises(NoRaoDataError):
        exact_quantile(values)


def test_quality_threshold_and_strict_ties():
    t = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        dict(
            time=t,
            median_ndvi=x,
            median_rao=x / 2,
            input_coverage=0.5,
            valid_windows=1,
            id=str(i),
        )
        for i, x in enumerate([0.1, 0.2, 0.2, 0.9])
    ]
    # A high-NDVI invalid observation must not shift the season threshold.
    rows.append(
        dict(
            time=t,
            median_ndvi=1.0,
            median_rao=1.0,
            input_coverage=0.49,
            valid_windows=1,
            id="bad",
        )
    )
    interval = _interval("2020-01-01", "2021-01-01")
    selected, threshold = select_season(rows, interval, interval, quantile=0.5)
    assert threshold == 0.2
    assert [row["id"] for row in selected] == ["3"]
    assert (
        len(select_season(rows, interval, interval, quantile=0.5, comparison="ge")[0])
        == 3
    )
    with pytest.raises(NoRaoDataError):
        select_season(rows, interval, interval, quantile=1)


def test_empty_quality_and_zero_windows():
    interval = _interval("2020-01-01", "2021-01-01")
    with pytest.raises(NoRaoDataError):
        select_season([], interval, interval)
    with pytest.raises(NoRaoDataError):
        select_season(
            [
                dict(
                    time=0,
                    median_ndvi=0.9,
                    median_rao=0.1,
                    input_coverage=1.0,
                    valid_windows=0,
                )
            ],
            interval,
            interval,
        )


def test_grouping_and_deterministic_priority():
    rows = [dict(id=x, time=100, spacecraft="S2A", orbit=5) for x in ["tileB", "tileA"]]
    rows.append(dict(id="other", time=100, spacecraft="S2B", orbit=5))
    groups = acquisition_groups(rows)
    assert [[r["id"] for r in group] for group in groups] == [
        ["tileA", "tileB"],
        ["other"],
    ]
    with pytest.raises(ValueError, match="identity"):
        acquisition_groups([dict(id="a", time=0)])


def test_rao_before_temporal_mean_counterexample():
    first, second = np.array([0.0, 1.0]), np.array([1.0, 0.0])
    assert (oracle(first) + oracle(second)) / 2 == 0.5
    assert oracle((first + second) / 2) == 0


def test_prepare_real_graph(real_ee):
    image = ee.Image.constant([2000, 6000, 4]).rename(["B4", "B8", "SCL"])
    result = prepare_sentinel2_nirv(
        image,
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        region=ee.Geometry.Rectangle([0, 0, 1, 1]),
    )
    graph = result.serialize()
    assert isinstance(result, ee.Image)
    assert "Image.reduceResolution" in graph
    assert "Image.remap" in graph
    assert "Image.normalizedDifference" not in graph
    assert "all_contributors" in graph
    assert "ESA/WorldCover/v200" in graph
    dependencies = Graph(result)
    nirv_node = next(
        a
        for a in dependencies.calls("Image.rename")
        if dependencies.constant(a["names"]) == ["nirv"]
    )
    ratio = next(dependencies.calls("Image.divide", nirv_node["input"]))
    difference = next(dependencies.calls("Image.subtract", ratio["image1"]))
    # Inspect the actual red/NIR operands, not the support-mask side branch.
    for operand, band in [("image1", "B8"), ("image2", "B4")]:
        selected = next(dependencies.calls("Image.select", difference[operand]))
        assert dependencies.constant(selected["bandSelectors"]) == [band]
        aggregated = list(
            dependencies.calls("Image.reduceResolution", selected["input"])
        )
        assert aggregated
        reflectance = [
            a
            for a in aggregated
            if list(dependencies.calls("Image.multiply", a["image"]))
        ]
        assert len(reflectance) == 1
        assert not list(dependencies.calls("Image.divide", reflectance[0]["image"]))


def test_nirv_explicit_arithmetic(real_ee):
    graph = nirv(reflectance_scale=0.0001)(
        ee.Image.constant([2000, 6000]).rename(["B4", "B8"])
    ).serialize()
    assert "Image.normalizedDifference" not in graph
    assert "Image.neq" in graph


def test_empty_and_excess_scene_limits(real_ee, monkeypatch):
    args = dict(
        region=ee.Geometry.Rectangle([0, 0, 1, 1]),
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        start="2020-01-01",
        end="2021-01-01",
    )
    monkeypatch.setattr(ee.data, "computeValue", lambda *_: 0)
    with pytest.raises(NoRaoDataError):
        nirv_seasonal_rao(**args)
    monkeypatch.setattr(ee.data, "computeValue", lambda *_: 1001)
    with pytest.raises(ValueError, match="limit"):
        nirv_seasonal_rao(**args)


@pytest.mark.parametrize(
    "quantile,comparison,threshold,estimate,selected_count",
    [(0.9, "gt", 0.74, 0.09, 1), (0, "ge", 0.2, 0.06, 2)],
)
@pytest.mark.parametrize("collection_kind", ["default", "string", "custom"])
def test_full_orchestration_real_graphs(
    real_ee,
    monkeypatch,
    quantile,
    comparison,
    threshold,
    estimate,
    selected_count,
    collection_kind,
):
    """Bounded server responses exercise orchestration, not evaluated pixels."""
    times = [1577836800000, 1577923200000]
    responses = iter(
        [
            2,
            [
                dict(id="a", time=times[0], spacecraft="S2A", orbit=5),
                dict(id="b", time=times[1], spacecraft="S2A", orbit=5),
            ],
            {"total": 100},
            {"ndvi_count": 50, "ndvi_median": 0.2},
            {"nirv_count": 50, "nirv_mean": 0.1, "nirv_min": 0, "nirv_max": 0.2},
            {"c_0_0": 0.5},
            {"rao_gn_count": 20, "rao_gn_median": 0.03},
            {"ndvi_count": 100, "ndvi_median": 0.8},
            {"nirv_count": 100, "nirv_mean": 0.3, "nirv_min": 0, "nirv_max": 0.6},
            {"c_0_0": 1.0},
            {"rao_gn_count": 80, "rao_gn_median": 0.09},
        ]
    )
    monkeypatch.setattr(ee.data, "computeValue", lambda *_: next(responses))
    collection = {
        "default": None,
        "string": "test/collection",
        "custom": ee.ImageCollection.fromImages([ee.Image.constant(1)]),
    }[collection_kind]
    result = nirv_seasonal_rao(
        region=ee.Geometry.Rectangle([0, 0, 1, 1]),
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        start="2020-01-01",
        end="2021-01-01",
        quantile=quantile,
        comparison=comparison,
        collection=collection,
    )
    assert result.threshold == pytest.approx(threshold)
    assert result.site_estimate == pytest.approx(estimate)
    assert result.provenance["world_cover_dataset"] == "ESA/WorldCover/v200"
    assert result.provenance["reflectance_scale"] == 0.0001
    assert result.provenance["fine_support"] == "all_contributors"
    assert (
        result.provenance["collection"]
        == {
            "default": "COPERNICUS/S2_SR_HARMONIZED",
            "string": "test/collection",
            "custom": "custom",
        }[collection_kind]
    )
    assert result.provenance["max_pixels"] == 1_000_000
    assert result.provenance["max_scenes"] == 1000
    assert result.scene_summaries[0]["input_coverage"] == 0.5
    assert result.scene_summaries[1]["window_coverage"] == 0.8
    assert result.scene_summaries[1]["selected"] is True
    assert len(result.scene_images) == selected_count
    assert "rao_gn_mean" in result.mean_image.serialize()
    graph = Graph(result.mean_image)
    output = next(
        a
        for a in graph.calls("Image.rename")
        if graph.constant(a["names"]) == ["rao_gn_mean"]
    )
    reduction = graph.resolve(output["input"])["functionInvocationValue"]
    assert reduction["functionName"] == "reduce.mean"
    temporal_mean = reduction["arguments"]
    members = next(
        graph.calls("ImageCollection.fromImages", temporal_mean["collection"])
    )
    images = graph.resolve(members["images"])["arrayValue"]["values"]
    assert len(images) == selected_count
    observed_sources = []
    for member in images:
        # Every temporal member is Rao of its own acquisition mosaic.
        assert list(graph.calls("Image.neighborhoodToArray", member))
        assert not list(graph.calls("reduce.mean", member))
        assert list(graph.calls("ImageCollection.mosaic", member))
        filters = list(graph.calls("Filter.listContains", member))
        observed_sources.append(graph.constant(filters[0]["leftValue"]))
    assert sorted(observed_sources) == (
        [["b"]] if selected_count == 1 else [["a"], ["b"]]
    )
    assert "rao_observation_count" in result.observation_count.serialize()
    assert isinstance(result.summary_table(), ee.FeatureCollection)
    assert "median_rao" in result.summary_table().serialize()
    with pytest.raises(StopIteration):
        next(responses)


def test_drive_preserves_grid_and_does_not_start():
    with (
        patch("geecomposer.export.drive.ee") as mock,
        patch("geecomposer.export.drive.to_ee_geometry", return_value="geometry"),
    ):
        task = export_to_drive(
            MagicMock(),
            "rao",
            "validation",
            "aoi",
            crs="EPSG:32618",
            crs_transform=(20, 0, 0, 0, -20, 0),
            max_pixels=50000,
        )
        arguments = mock.batch.Export.image.toDrive.call_args.kwargs
        assert arguments["crsTransform"] == [20, 0, 0, 0, -20, 0]
        assert arguments["crs"] == "EPSG:32618"
        assert "scale" not in arguments
        task.start.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(scale=0),
        dict(scale=math.nan),
        dict(scale=20, crs="EPSG:32618", crs_transform=(20, 0, 0, 0, -20, 0)),
        dict(crs_transform=(20, 0, 0, 0, -20, 0)),
        dict(crs="EPSG:32618", crs_transform=(0, 0, 0, 0, 0, 0)),
        dict(crs="EPSG:32618", crs_transform=(20, 0)),
        dict(crs="EPSG:32618", crs_transform=(20, 0, "bad", 0, -20, 0)),
    ],
)
def test_drive_rejects_invalid_grid(kwargs):
    with pytest.raises(GeeComposerError):
        export_to_drive(None, "rao", "validation", None, **kwargs)


@pytest.mark.parametrize("bands,decimals", [(["wrong"], 2), (["nirv"], None)])
def test_invalid_frozen_model_precedes_any_evaluation(
    real_ee, monkeypatch, bands, decimals
):
    from geecomposer.rao import RaoNormalization

    model = RaoNormalization.from_statistics(
        bands, [0], [[1]], 2, decimal_places=decimals, calibration="fixture"
    )

    def forbidden(*_):
        pytest.fail("invalid model attempted a remote evaluation")

    monkeypatch.setattr(ee.ComputedObject, "getInfo", forbidden)
    with pytest.raises(ValueError, match="bands/quantization"):
        nirv_seasonal_rao(
            region=None, grid=None, start="2020-01-01", end="2021-01-01", model=model
        )


def test_reference_only_row_changes_threshold_but_cannot_be_selected():
    def row(day, ndvi):
        return dict(
            time=int(datetime(2020, 1, day, tzinfo=timezone.utc).timestamp() * 1000),
            id=str(day),
            median_ndvi=ndvi,
            median_rao=0.1,
            input_coverage=1,
            valid_windows=9,
        )

    rows = [row(1, 0.9), row(2, 0.1), row(3, 0.6), row(4, 0.8)]
    reference = _interval("2020-01-01", "2020-01-03")
    output = _interval("2020-01-03", "2020-01-05")
    selected, threshold = select_season(rows, reference, output, quantile=0.5)
    assert threshold == 0.5
    assert [r["id"] for r in selected] == ["3", "4"]
    _, without = select_season(rows[1:], reference, output, quantile=0.5)
    assert without == 0.1


def test_disabled_worldcover_provenance(real_ee):
    graph = Graph(
        prepare_sentinel2_nirv(
            ee.Image.constant([1, 2, 4]).rename(["B4", "B8", "SCL"]),
            grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
            region=ee.Geometry.Rectangle([0, 0, 1, 1]),
            world_cover_classes=None,
        )
    )
    properties = {
        graph.constant(a["key"]): graph.constant(a["value"])
        for a in graph.calls("Element.set")
    }
    assert properties["rao:worldcover"] == "disabled"
    assert properties["rao:worldcover_classes"] == "disabled"
    assert not list(graph.calls("ImageCollection.load"))


@pytest.mark.parametrize(
    "total,exclusion", [(1, "insufficient_calibration_pixels"), (10, "input_coverage")]
)
def test_insufficient_pixels_exclusion(real_ee, monkeypatch, total, exclusion):
    import geecomposer.seasonal as seasonal

    responses = iter(
        [
            1,
            [dict(id="a", time=1577836800000, spacecraft="S2A", orbit=5)],
            {"total": total},
            {"ndvi_count": 1, "ndvi_median": 0.8},
        ]
    )
    monkeypatch.setattr(ee.data, "computeValue", lambda *_: next(responses))

    def check(rows, *_args, **_kwargs):
        assert rows[0]["exclusion"] == exclusion
        raise NoRaoDataError("checked exclusion")

    monkeypatch.setattr(seasonal, "select_season", check)
    with pytest.raises(NoRaoDataError, match="checked exclusion"):
        nirv_seasonal_rao(
            region=ee.Geometry.Rectangle([0, 0, 1, 1]),
            grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
            start="2020-01-01",
            end="2021-01-01",
        )
