"""Coarse-first construction and temporal statistics (S01 F03-F06, F09, F10; A6-A12).

Graph tests inspect the production constructors with the installed EE client
offline (``real_ee``; any evaluation fails). Numeric expectations come from the
independent Fraction oracle in ``coarse_oracle`` (not an Earth Engine emulator).
"""

from fractions import Fraction
import math

import pytest

from geecomposer import coarse
from geecomposer.rao import RaoGrid
from coarse_oracle import construct, q, spatial, temporal
from ee_graph import Graph
from test_coarse_periods import AOI, BANDS, S2, UTM, snapshot

WEIGHTS = ["1/16", "1/8", "1/16", "1/8", "1/4", "1/8", "1/16", "1/8", "1/16"]
F03 = {  # case: values, masks, W, S, mean
    "A_fractional_nonfinite": ([4, 8, 0, 2, 6, 10, "nan", 3, 5], [1, 1, 1, 0, 1, "1/2", 1, 1, 0], "11/16", "15/4", "60/11"),
    "B_threshold_equal": ([4, 8, 0, 2, 6, 10, 7, 3, 5], [0, 0, 0, 0, 1, 1, 0, 1, 0], "1/2", "25/8", "25/4"),
    "C_below_threshold": ([4, 8, 0, 2, 6, 10, 7, 3, 5], [0, 0, 0, 0, 1, 0, 0, 1, 0], "3/8", "15/8", None),
    "D_all_masked": ([4, 8, 0, 2, 6, 10, 7, 3, 5], [0] * 9, 0, 0, None),
    "E_valid_zero": ([0] * 9, [1] * 9, 1, 0, 0),
    "F_partial_footprint": ([1, 2, None, 3, 4, None, 5, 6, None], [1, 1, None, 1, 1, None, 1, 1, None], "3/4", "11/4", "11/3"),
    "G_nan_inf_fractional": (["nan", "inf", "-inf", 0, 6, 10, 3, 5, 8], [1, 1, "1/2", 1, 1, 0, 1, 1, 1], "5/8", "45/16", "9/2"),
    "H_all_invalid": (["nan", "inf", "-inf", "nan", 7, "inf", 2, "-inf", None], [1, "1/2", 1, "1/4", 0, 1, 0, 1, None], 0, 0, None),
}


@pytest.mark.parametrize("case", F03)
def test_f03_spatial_mean_oracle(case):
    values, masks, w, s, mean = F03[case]
    pixels = construct(values, masks)
    assert all(math.isfinite(n) and m == 1 for (n, m), _ in pixels) and all(m == 1 for _, (_, m) in pixels)
    assert spatial(values, masks, WEIGHTS, Fraction(1, 2)) == (q(w), q(s), q(mean))


def node(graph, value):
    return graph.resolve(value)


def call(graph, value):
    resolved = graph.resolve(value)
    inv = resolved["functionInvocationValue"]
    return inv["functionName"], inv["arguments"]


def test_sum_weight_construction_order(real_ee):
    """Replacement precedes multiplication; S/W ride on a mask-1 constant base; the
    fractional weight enters the numerator once and is the W band itself."""
    import ee

    values = ee.Image.constant([1, 2]).rename(["B4", "B8"])
    image = coarse.sum_weight_inputs(values, ee.Image.constant(1), ["S_B4", "S_B8", "W"])
    graph = Graph(image)
    name, args = call(graph, graph.root)
    assert name == "Image.where"
    base_name, base = call(graph, args["input"])
    assert base_name == "Image.setDefaultProjection"
    assert call(graph, base["image"])[0] == "Image.rename"
    test_name, test = call(graph, args["test"])
    assert test_name == "Image.gt"
    value_name, value = call(graph, args["value"])
    assert value_name == "Image.rename"
    stacked_name, stacked = call(graph, value["input"])
    assert stacked_name == "Image.addBands"
    numerator_name, numerator = call(graph, stacked["dstImg"])
    assert numerator_name == "Image.multiply"
    replaced_name, replaced = call(graph, numerator["image1"])
    assert replaced_name == "Image.where"  # non-finite values replaced before the multiply
    assert graph.constant(call(graph, replaced["value"])[1]["value"]) == 0
    assert call(graph, replaced["test"])[0] == "Image.not"
    assert node(graph, numerator["image2"]) is node(graph, stacked["srcImg"])  # weight used once, = W
    assert node(graph, test["image1"]) is node(graph, stacked["srcImg"])
    assert not list(graph.calls("Image.unmask"))


def constant_of(graph, value):
    name, args = call(graph, value)
    return graph.constant(args["value"]) if name == "Image.constant" else None


def composite(theta=0.37, **kw):
    return coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snapshot(),
                            coverage_min=theta, **{**S2, **kw})


def test_threshold_applies_only_after_the_last_stage(real_ee):
    image = composite(temporal=("mean",))
    graph = Graph(image)
    stages = list(graph.calls("Image.reduceResolution"))
    assert stages
    for stage in stages:  # no coverage threshold feeds any spatial stage
        assert not [g for g in graph.calls("Image.gte", stage["image"]) if constant_of(graph, g["image2"]) == 0.37]
    thresholds = [g for g in graph.calls("Image.gte") if constant_of(graph, g["image2"]) == 0.37]
    assert thresholds and all(list(graph.calls("Image.reduceResolution", g["image1"])) for g in thresholds)


def test_every_spatial_input_is_a_mask_one_rebase(real_ee):
    graph = Graph(composite())
    for stage in graph.calls("Image.reduceResolution"):
        name, args = call(graph, stage["image"])
        assert name == "Image.where"
        assert call(graph, args["input"])[0] == "Image.setDefaultProjection"


def test_temporal_statistics_are_exact_and_equal_weight(real_ee):
    graph = Graph(composite(temporal=coarse.TEMPORAL))
    text = composite(temporal=coarse.TEMPORAL).serialize()
    for forbidden in ("Reducer.median", "Reducer.histogram", "Reducer.percentile", "Reducer.stdDev",
                      "Reducer.sampleStdDev", "Image.unmask"):
        assert forbidden not in text
    for required in ("Image.arraySort", "Image.arrayGet", "Image.sqrt", "Image.pow"):
        assert list(graph.calls(required))
    mean = [c for c in graph.calls("Image.rename") if graph.constant(c["names"]) == ["B4__s_mean__t_mean"]]
    (mean,) = mean
    masked_name, masked = call(graph, mean["input"])
    assert masked_name == "Image.updateMask"
    divide_name, divide = call(graph, masked["image"])
    assert divide_name == "Image.divide"  # sum of observation values / number of valid observations
    for operand, band in ((divide["image1"], ["B4"]), (divide["image2"], ["valid"])):
        select_name, select = call(graph, operand)
        assert select_name == "Image.select" and graph.constant(select["bandSelectors"]) == band


def test_granule_ties_keep_the_smaller_identifier(real_ee):
    graph = Graph(composite())
    (choice,) = [w for w in graph.calls("Image.where") if call(graph, w["test"])[0] == "Image.gt"
                 and call(graph, call(graph, w["test"])[1]["image2"])[0] == "Image.select"]

    def loads(value):
        return {graph.constant(c["id"]) for c in graph.calls("Image.load", value)}

    assert any(i.endswith("SYNTH_C1") for i in loads(choice["input"]))
    assert any(i.endswith("SYNTH_C2") for i in loads(choice["value"]))
    assert not any(i.endswith("SYNTH_C2") for i in loads(choice["input"]))


def test_units_scale_and_indices(real_ee):
    s2 = composite(temporal=("median", "mean"), index="ndvi").serialize()
    assert "0.0001" in s2 and "ndvi__of__s_mean__t_median" in s2 and "ndvi__of__s_mean__t_mean" in s2
    spec = coarse._spec("sentinel1_float", ["VV", "VH"], None, {"orbitPass": "DESCENDING", "polarizations": ["VV", "VH"]},
                        ("mean", "median", "sd_pop"), 0.5, None, True)
    assert coarse.output_names(spec) == [
        "VV__s_mean__t_mean", "VV__s_mean__t_median", "VV__s_mean__t_sd_pop",
        "VH__s_mean__t_mean", "VH__s_mean__t_median", "VH__s_mean__t_sd_pop",
        "VV__s_mean__t_mean__db", "VV__s_mean__t_median__db", "VH__s_mean__t_mean__db",
        "VH__s_mean__t_median__db", "obs__t_count", "cov__t_mean",
    ]


def test_sentinel1_linear_power_has_no_scale_or_speckle_filter(real_ee):
    rows = []
    for image_id, when in [("SYNTH_S1A", 1735787000000), ("SYNTH_S1B", 1735787005000)]:
        rows.append({"id": image_id, "time_ms": when, "platform_number": "A", "missionDataTakeID": 7,
                     "projections": {b: {"crs": "EPSG:32612", "transform": [10, 0, 500000, 0, -10, 3100000]}
                                     for b in ("VV", "VH")}})
    filters = {"orbitPass": "DESCENDING", "polarizations": ["VV", "VH"]}
    snap = coarse.build_snapshot(dataset="sentinel1_float", aoi=AOI, grid=UTM, start="2025-01-01", end="2025-02-01",
                                 select=["VV", "VH"], filters=filters, candidate_rows=rows, completion_rows=rows)
    assert snap.groups[0][2] == ("SYNTH_S1A", "SYNTH_S1B")  # two slices, one acquisition
    image = coarse.composite(dataset="sentinel1_float", aoi=AOI, grid=UTM, start="2025-01-01", end="2025-02-01",
                             select=["VV", "VH"], filters=filters, snapshot=snap, temporal=("mean",), db=True)
    text = image.serialize()
    assert "COPERNICUS/S1_GRD_FLOAT/SYNTH_S1A" in text and "0.0001" not in text
    assert "VV__s_mean__t_mean__db" in text and "Image.log10" in text
    assert "gamma" not in text.lower() and "focal" not in text.lower()


@pytest.mark.parametrize(
    "kwargs, code",
    [
        (dict(mask=None), "mask"),
        (dict(select=["B11"]), "bands"),
        (dict(select=["B4", "B4"]), "bands"),
        (dict(filters={"orbitPass": "ASCENDING"}), "filters"),
        (dict(db=True), "db"),
        (dict(index="nirv"), "index"),
        (dict(index="ndvi", select=["B2", "B4"]), "index"),
        (dict(index="ndvi", temporal=("sd_pop",)), "index"),
        (dict(temporal=("histogram",)), "temporal"),
        (dict(temporal=()), "temporal"),
        (dict(temporal=("mean", "mean")), "temporal"),
        (dict(coverage_min=0), "coverage_min"),
        (dict(coverage_min=1.5), "coverage_min"),
        (dict(coverage_min=float("nan")), "coverage_min"),
        (dict(coverage_min=True), "coverage_min"),
        (dict(dataset="sentinel1"), "dataset"),
    ],
)
def test_sentinel2_request_rejections(kwargs, code):
    with pytest.raises(coarse.CoarseError) as err:
        coarse._spec(**{**dict(dataset="sentinel2", select=BANDS, mask="s2_cloud_score_plus", filters=None,
                               temporal=("median",), coverage_min=0.5, index=None, db=False), **kwargs})
    assert err.value.code == code


@pytest.mark.parametrize(
    "filters, extra, code",
    [
        ({"polarizations": ["VV", "VH"]}, {}, "filters"),
        ({"orbitPass": "DESCENDING", "polarizations": ["VV"]}, {}, "filters"),
        ({"orbitPass": "DESCENDING", "polarizations": ["VV", "VH"], "instrumentMode": "EW"}, {}, "filters"),
        ({"orbitPass": "DESCENDING", "polarizations": ["VV", "VH"]}, {"mask": "s2_cloud_score_plus"}, "mask"),
        (None, {}, "filters"),
    ],
)
def test_sentinel1_request_rejections(filters, extra, code):
    with pytest.raises(coarse.CoarseError) as err:
        coarse._spec(**{**dict(dataset="sentinel1_float", select=["VV", "VH"], mask=None, filters=filters,
                               temporal=("mean",), coverage_min=0.5, index=None, db=False), **extra})
    assert err.value.code == code


def test_f04_order_does_not_commute():
    weights = ["1/2", "1/2"]
    for values, masks, coarse_expected, fine_expected in (
        ([[0, 10], [4, 4], [9, 0]], [[1, 1], [1, 1], [1, 1]], ("9/2", "9/2"), (4, "9/2")),
        ([[0, 10], [4, 4], [9, 0]], [[1, 1], [1, 1], [1, 0]], (5, 6), ("11/2", "17/3")),
    ):
        observed = [spatial(v, m, weights, Fraction(1, 2))[2] for v, m in zip(values, masks)]
        stats = temporal([v for v in observed if v is not None])
        pixels = [temporal([q(v[i]) for v, m in zip(values, masks) if m[i]]) for i in range(2)]
        fine = [sum(q(a) * p[s] for a, p in zip(weights, pixels)) for s in ("median", "mean")]
        assert (stats["median"], stats["mean"]) == tuple(q(x) for x in coarse_expected)
        assert tuple(fine) == tuple(q(x) for x in fine_expected)


def test_f05_staged_sums_and_invalid_shortcuts():
    subcells = [([2, 2, 2, 2], [1] * 4), ([10, 7, 7, 7], [1, 0, 0, 0]), ([6, 6, 1, 1], [1, 1, 0, 0]), ([4, 4, 9, 9], [1, 1, 0, 0])]
    parts = [spatial(v, m, ["1/4"] * 4, Fraction(0))[:2] for v, m in subcells]
    staged_w, staged_s = sum(p[0] for p in parts) / 4, sum(p[1] for p in parts) / 4
    direct = spatial(sum((v for v, _ in subcells), []), sum((m for _, m in subcells), []), ["1/16"] * 16, Fraction(1, 2))
    assert (staged_w, staged_s, staged_s / staged_w) == direct == (q("9/16"), q("19/8"), q("38/9"))
    assert sum(s / w for w, s in parts) / 4 == q("11/2")  # mean of means is wrong
    kept = [p for p in parts if p[0] >= Fraction(1, 2)]
    assert sum(p[1] for p in kept) / sum(p[0] for p in kept) == q("7/2")  # intermediate threshold is wrong


def test_f06_temporal_statistics_oracle():
    cases = {
        "even": ([2, 4, 4, 4, 5, 5, 7, 9], dict(count=8, mean=5, median="9/2", var_pop=4, min=2, max=9, range=7)),
        "odd": ([3, 1, 2], dict(count=3, mean=2, median=2, var_pop="2/3", min=1, max=3, range=2)),
        "single": (["7/2"], dict(count=1, mean="7/2", median="7/2", var_pop=0, min="7/2", max="7/2", range=0)),
        "empty": ([], dict(count=0, mean=None, median=None, var_pop=None, min=None, max=None, range=None)),
    }
    for values, expected in cases.values():
        got = temporal([q(v) for v in values])
        assert {k: got[k] for k in expected} == {k: q(v) if not isinstance(v, int) or k != "count" else v for k, v in expected.items()}
    observations = [(2, 1), (10, "1/2")]  # equal-observation 6, not pooled 14/3
    assert sum(q(v) for v, _ in observations) / 2 == 6
    assert sum(q(v) * q(w) for v, w in observations) / sum(q(w) for _, w in observations) == q("14/3")


def test_f09_f10_selection_and_index_timing():
    granules = [("SYNTH_ACQ1_T1", 1, 4), ("SYNTH_ACQ1_T2", "1/2", 1)]
    best = None
    for gid, w, s in sorted(granules):
        if best is None or q(w) > q(best[1]):
            best = (gid, w, s)
    assert best[0] == "SYNTH_ACQ1_T1" and q(best[2]) / q(best[1]) == 4  # not double-counted 10/3
    tie = sorted([("SYNTH_ACQ2_T2", "1/2", 1), ("SYNTH_ACQ2_T1", "1/2", 3)])
    assert tie[0][0] == "SYNTH_ACQ2_T1" and q(tie[0][2]) / q(tie[0][1]) == 6
    refl = [[q(dn) * q("1/10000") for dn in pair] for pair in [[1000, 5000], [500, 2000], [2000, 3000]]]
    b4 = temporal([r[0] for r in refl])["median"]
    b8 = temporal([r[1] for r in refl])["median"]
    assert (b8 - b4) / (b8 + b4) == q("1/2")  # NDVI of the median composite
    assert temporal([(n - r) / (n + r) for r, n in refl])["median"] == q("3/5")  # median of NDVI differs


def test_utm_grid_for_another_zone_is_refused_after_metadata(real_ee):
    other = RaoGrid("EPSG:32613", (1000, 0, 400000, 0, -1000, 3100000))
    with pytest.raises(coarse.CoarseError) as err:  # scope matches; zone-12 metadata against a zone-13 grid
        coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snapshot(grid=other), **{**S2, "grid": other})
    assert err.value.code == coarse.NO_PROOF
