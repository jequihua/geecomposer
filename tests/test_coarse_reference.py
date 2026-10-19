"""Coarse GN Rao, the benchmark-only fine-first reference and A19 mapping (S01 F07, F09, F13).

Graph checks run offline (``real_ee``); Rao numbers come from the independent
equal-pixel oracle in ``coarse_oracle``. The existing ``rao_q`` formula is reused.
"""

from fractions import Fraction

import pytest

from geecomposer import coarse, grouping
from geecomposer.rao import RaoGrid, RaoNormalization
import coarse_oracle
from coarse_oracle import q, rao, temporal
from ee_graph import Graph
from test_coarse_periods import AOI, BANDS, CAT, S2, UTM, model, snapshot

FINE = RaoGrid("EPSG:32612", (10, 0, 400000, 0, -10, 3100000))
FEATURES = coarse.feature_names(BANDS, "median")
REFERENCE = coarse.reference_feature_names(BANDS, "median")


def call(graph, value):
    inv = graph.resolve(value)["functionInvocationValue"]
    return inv["functionName"], inv["arguments"]


def median_composite():
    return coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snapshot(), temporal=("median",), **S2)


def test_coarse_rao_uses_frozen_model_strict_window_and_full_cell_region(real_ee, runner_offline):
    frozen = model()
    image = coarse.rao(median_composite(), model=frozen, grid=UTM, aoi=AOI)
    text = image.serialize()
    graph = Graph(image)
    for name in ("coarse_rao_gn", "coarse_rao_valid_count", "coarse_rao_coverage", "obs__t_count", *FEATURES):
        assert name in text
    assert frozen.fingerprint in text and '"coarse_rao_gn_3x3"' in text and "Image.reduceRegion" not in text
    assert any(c for c in graph.calls("Element.set") if "coarse:support_m" in str(c))
    clips = list(graph.calls("Image.clip"))
    assert clips and all(list(graph.calls("Geometry.coveringGrid", c["geometry"])) for c in clips)  # full cell union
    kernels = list(graph.calls("Kernel.square"))
    assert kernels and {graph.constant(k["radius"]) for k in kernels} == {1}  # 3x3 window


def test_rao_rejects_diagnostics_or_mismatched_models(real_ee):
    from geecomposer.rao import RaoNormalization

    wrong = RaoNormalization(bands=["obs__t_count"], means=[1.0], scales=[1.0], loadings=[[1.0]], retained_fraction=1.0,
                             n_sigmas=6.0, sample_count=5, decimal_places=None, calibration="x")
    for bad in (wrong, None):
        with pytest.raises(coarse.CoarseError):
            coarse.rao(median_composite(), model=bad, grid=UTM, aoi=AOI)


def test_f07_rao_oracle_cases():
    window = [[0], [0], [0], [0], [1], [0], [0], [0], [0]]
    assert rao(window) == q("16/81")
    one = {"means": ["1/2"], "scales": ["1/2"], "loadings": [[1]], "retained_fraction": 1, "n_sigmas": 6}
    assert rao(window, one) == q("8/243")
    two = [[0, 0]] * 4 + [[3, 4]] + [[0, 0]] * 4
    assert rao(two) == q("80/81")
    model2 = {"means": [0, 0], "scales": [1, 2], "loadings": [[1, 0], [0, 1]], "retained_fraction": 1, "n_sigmas": 6}
    assert abs(rao(two, model2) ** 2 - float(q("104/59049"))) < 1e-15
    assert rao(window[:7] + [None] + window[8:]) is None  # strict: incomplete window missing, never 0
    dates = [[1, 0, 0, 0, 0, 0, 0, 0, 0], [0, 1, 1, 1, 1, 1, 1, 1, 1]]
    composite = [temporal([Fraction(d[i]) for d in dates])["median"] for i in range(9)]
    assert rao([[v] for v in composite]) == 0 != sum(rao([[v] for v in d]) for d in dates) / 2 == q("16/81")


def test_tile_halo_reproduces_untiled_values_and_crop_happens_after_science():
    grid = [[1, 0, 0, 1, 0, 0], [0, 0, 0, 1, 1, 0], [0, 0, 0, 0, 0, 1]]

    def window(col, low, high):
        return [[grid[r][c]] if low <= c <= high else None for r in range(3) for c in range(col - 1, col + 2)]

    full = [rao(window(c, 0, 5)) for c in range(6)]
    assert full == [None, q("16/81"), q("28/81"), q("4/9"), q("40/81"), None]
    halo = [rao(window(c, max(0, lo - 1), min(5, hi + 1))) for lo, hi in ((0, 2), (3, 5)) for c in range(lo, hi + 1)]
    no_halo = [rao(window(c, lo, hi)) for lo, hi in ((0, 2), (3, 5)) for c in range(lo, hi + 1)]
    assert halo == full and no_halo == [None, q("16/81"), None, None, q("40/81"), None]


@pytest.mark.parametrize(
    "model_bands, reference_bands, code",
    [
        (FEATURES, REFERENCE[:3], "missing"),
        (FEATURES, REFERENCE + [REFERENCE[2]], "duplicate"),
        (FEATURES, ["B2__t_mean__s_mean"] + REFERENCE[1:], "missing"),
        (FEATURES, FEATURES, "missing"),
        (coarse.feature_names(BANDS, "mean"), REFERENCE, "model_bands"),
    ],
)
def test_reference_mapping_rejections(model_bands, reference_bands, code):
    with pytest.raises(coarse.CoarseError) as err:
        coarse.reference_feature_map(model_bands, reference_bands)
    assert err.value.code == code


def test_reference_mapping_is_by_name_in_model_order():
    shuffled = [REFERENCE[3], REFERENCE[0], "obs__t_count", REFERENCE[2], REFERENCE[1]]
    assert coarse.reference_feature_map(FEATURES, shuffled) == tuple(zip(REFERENCE, FEATURES))


def test_fine_first_reference_declares_its_confounds(real_ee, runner_offline):
    image = coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(),
                                        fine_grid=FINE, temporal=("median",), **S2)
    text = image.serialize()
    for value in (*REFERENCE, "cov__s_mean", '"nearest"', "lowest_id_valid_per_fine_pixel", "reference:confounds"):
        assert value in text
    graph = Graph(image)
    stages = list(graph.calls("Image.reduceResolution"))
    assert sorted(graph.constant(c["maxPixels"]) for c in stages) == [144, 144]
    # the first stage reads the declared 10 m fine grid, never a composite's default projection
    first = [s for s in stages if graph.constant(s["maxPixels"]) == 144]
    bases = [call(graph, call(graph, s["image"])[1]["input"]) for s in first]
    transforms = [graph.constant(call(graph, b[1]["crs"])[1]["transform"]) for b in bases]
    assert list(FINE.transform) in transforms


@pytest.mark.parametrize(
    "fine, code",
    [
        (RaoGrid("EPSG:32612", (20, 0, 400000, 0, -20, 3100000)), "fine_grid"),
        (RaoGrid("EPSG:32612", (10, 0, 400005, 0, -10, 3100000)), "fine_grid"),
        (RaoGrid("EPSG:32613", (10, 0, 400000, 0, -10, 3100000)), coarse.NO_PROOF),
    ],
)
def test_fine_grid_rules(real_ee, fine, code):
    with pytest.raises(coarse.CoarseError) as err:
        coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(), fine_grid=fine, **S2)
    assert err.value.code == code


def test_fine_first_admits_member_sources_like_the_coarse_product(real_ee):
    zone13 = {"crs": "EPSG:32613", "transform": [10, 0, 300000, 0, -10, 3100000]}
    foreign = {k: dict(v, projections={b: dict(zone13) for b in v["projections"]}) for k, v in CAT.items()}
    with pytest.raises(coarse.CoarseError) as err:  # never silently resampled across zones
        coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(catalogue=foreign),
                                    fine_grid=FINE, **S2)
    assert err.value.code == coarse.NO_PROOF


def test_reference_rao_maps_names_into_the_same_frozen_model(real_ee, runner_offline):
    frozen = model()
    before = frozen.to_json()
    reference = coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(),
                                            fine_grid=FINE, temporal=("median",), **S2)
    image = coarse.reference_rao(reference, model=frozen, grid=UTM, aoi=AOI, reference_bands=REFERENCE)
    text = image.serialize()
    assert "reference_rao_gn" in text and frozen.fingerprint in text and frozen.to_json() == before
    graph = Graph(image)
    renames = [c for c in graph.calls("Image.select") if graph.constant(c["bandSelectors"]) == REFERENCE]
    assert renames and graph.constant(renames[0]["newNames"]) == FEATURES
    with pytest.raises(coarse.CoarseError) as err:
        coarse.reference_rao(reference, model=frozen, grid=UTM, aoi=AOI, reference_bands=FEATURES)
    assert err.value.code == "missing"


def test_f09_selection_alone_changes_a_single_acquisition():
    weights = [Fraction(1, 4)] * 4
    granules = {"SYNTH_A": [1, 1, None, None], "SYNTH_B": [3, 3, 3, 3]}
    coverage = {g: sum(w for w, v in zip(weights, vals) if v is not None) for g, vals in granules.items()}
    chosen = max(sorted(coverage), key=coverage.get)
    coarse_value = sum(w * v for w, v in zip(weights, granules[chosen]) if v is not None) / coverage[chosen]
    pixels = [next(granules[g][i] for g in sorted(granules) if granules[g][i] is not None) for i in range(4)]
    fine_value = sum(w * v for w, v in zip(weights, pixels))
    assert (chosen, coarse_value, fine_value) == ("SYNTH_B", 3, 2)


# ------------------------------------------------------------------ round 2: F5-F6 (prompt 203)


def normalization(bands, calibration="test-frozen"):
    count = len(bands)
    return RaoNormalization(bands=list(bands), means=[0.05 * (i + 1) for i in range(count)],
                            scales=[0.02 * (i + 1) for i in range(count)],
                            loadings=[[1 if i == j else 0 for j in range(count)] for i in range(count)],
                            retained_fraction=1.0, n_sigmas=6.0, sample_count=10, decimal_places=None,
                            calibration=calibration)


ONE_FEATURE = ["B2__s_mean__t_median"]  # the review's counterexample


@pytest.mark.parametrize(
    "bands",
    [
        ONE_FEATURE,
        FEATURES[:3],
        [FEATURES[0], FEATURES[1], FEATURES[3]],
        FEATURES + ["B5__s_mean__t_median"],
        FEATURES[:3] + ["B8__s_mean__t_mean"],
    ],
)
def test_f5_coarse_rao_requires_exactly_the_four_features(real_ee, bands):
    reduced = normalization(bands)
    selected = [b.split("__")[0] for b in bands]
    with pytest.raises(coarse.CoarseError) as err:
        coarse.rao(median_composite(), model=reduced, grid=UTM, aoi=AOI)
    assert err.value.code == "model_bands"
    with pytest.raises(coarse.CoarseError) as err:
        coarse.composite_periods(grouping.monthly_periods(2025), model=reduced, snapshot=snapshot(),
                                 temporal=("median",), **{**S2, "select": selected})
    assert err.value.code in ("model_bands", "bands")
    reference = coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(),
                                            fine_grid=FINE, temporal=("median",), **S2)
    with pytest.raises(coarse.CoarseError) as err:
        coarse.reference_rao(reference, model=reduced, grid=UTM, aoi=AOI, reference_bands=REFERENCE)
    assert err.value.code == "model_bands"
    with pytest.raises(coarse.CoarseError) as err:
        coarse.reference_feature_map(bands, REFERENCE)
    assert err.value.code == "model_bands"


def test_f5_a_reduced_feature_metric_is_a_different_metric():
    window = [[0, 0, 0, 0]] * 4 + [[0, 0, 0, 1]] + [[0, 0, 0, 0]] * 4  # differs only in B8
    assert rao([[cell[0]] for cell in window]) == 0 != rao(window) == q("16/81")


def test_f5_complete_set_in_model_order_keeps_identity_and_eight_bands(real_ee, runner_offline):
    order = [FEATURES[3], FEATURES[1], FEATURES[0], FEATURES[2]]  # B8, B3, B2, B4
    frozen = normalization(order, calibration="non-default-order")
    before = (frozen.fingerprint, frozen.to_json())
    selected = [b.split("__")[0] for b in order]
    snap = snapshot(select=selected)  # the snapshot scope binds the requested band order
    composite = coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snap, temporal=("median",),
                                 **{**S2, "select": selected})
    image = coarse.rao(composite, model=frozen, grid=UTM, aoi=AOI)
    graph = Graph(image)
    selections = [graph.constant(c["bandSelectors"]) for c in graph.calls("Image.select")]
    assert ["obs__t_count"] + order in selections  # features in the model's declared order
    assert ["rao_gn", "rao_valid_count", "rao_coverage"] in selections
    assert len(["obs__t_count"] + order) + 3 == 8
    months = coarse.composite_periods(grouping.monthly_periods(2025), model=frozen, snapshot=snap,
                                      temporal=("median",), **{**S2, "select": selected})
    assert len(months) == 12 and all(frozen.fingerprint in m.serialize() for m in months.values())
    reference = coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(),
                                            fine_grid=FINE, temporal=("median",), **S2)
    mapped = Graph(coarse.reference_rao(reference, model=frozen, grid=UTM, aoi=AOI, reference_bands=REFERENCE))
    renames = [c for c in mapped.calls("Image.select") if "newNames" in c and mapped.constant(c["newNames"]) == order]
    assert renames and mapped.constant(renames[0]["bandSelectors"]) == [b.replace("__s_mean__t_median", "__t_median__s_mean")
                                                                         for b in order]
    assert (frozen.fingerprint, frozen.to_json()) == before


def band_order(graph, node):
    """Output band names of an image graph, following its addBands chain (test helper)."""
    call = graph.resolve(node)["functionInvocationValue"]
    name, args = call["functionName"], call["arguments"]
    if name == "Image.addBands":
        return band_order(graph, args["dstImg"]) + band_order(graph, args["srcImg"])
    if name == "Image.rename":
        names = graph.constant(args["names"])
        return names if isinstance(names, list) else [names]
    for key in ("image", "input", "object"):
        if key in args:
            return band_order(graph, args[key])
    raise AssertionError(name)


def empty_reference(snap, start, end, temporal):
    return coarse.fine_first_reference(start=start, end=end, snapshot=snap, fine_grid=FINE, temporal=temporal, **S2)


@pytest.mark.parametrize("temporal", [("median",), ("mean", "sd_pop", "min", "range")])
@pytest.mark.parametrize("case", ["empty_snapshot", "empty_period"])
def test_f6_empty_fine_first_reference_keeps_its_schema(real_ee, runner_offline, case, temporal):
    snap = snapshot(candidates=[], completion=[]) if case == "empty_snapshot" else snapshot()
    image = empty_reference(snap, "2025-02-01", "2025-03-01", temporal)  # February has no acquisition
    graph = Graph(image)
    text = image.serialize()
    expected = [f"{b}__t_{s}__s_mean" for b in BANDS for s in temporal] + ["cov__s_mean"]
    for forbidden in ("ImageCollection.fromImages", "Image.arraySort", "Image.reduceResolution", '"valid"'):
        assert forbidden not in text  # no empty collection reaches a band or array operation
    assert band_order(graph, graph.root) == expected  # exact reference names and order
    constants = {graph.constant(c["value"]) for c in graph.calls("Image.constant")}
    assert 0 in constants
    (default,) = list(graph.calls("Image.setDefaultProjection"))
    projection = graph.resolve(default["crs"])["functionInvocationValue"]["arguments"]
    assert graph.constant(projection["transform"]) == list(UTM.transform)  # the target grid, not the fine grid
    clips = list(graph.calls("Image.clip"))
    assert clips and all(list(graph.calls("Geometry.coveringGrid", c["geometry"])) for c in clips)
    for value in ("reference:benchmark_only", "lowest_id_valid_per_fine_pixel", "reference:confounds"):
        assert value in text
    assert coarse_oracle.temporal([])["count"] == 0 and coarse_oracle.temporal([])["median"] is None  # statistics missing
    assert coarse_oracle.spatial([1] * 9, [0] * 9, [Fraction(1, 9)] * 9, Fraction(1, 2))[0] == 0  # coverage zero, not missing


def test_f6_empty_reference_feeds_reference_rao_without_new_bands(real_ee):
    image = empty_reference(snapshot(), "2025-02-01", "2025-03-01", ("median",))
    result = coarse.reference_rao(image, model=model(), grid=UTM, aoi=AOI, reference_bands=REFERENCE)
    assert "reference_rao_gn" in result.serialize()


def test_f6_nonempty_and_single_observation_references_are_unchanged(real_ee):
    june = empty_reference(snapshot(), "2025-06-01", "2025-07-01", ("median",))  # one acquisition
    text = june.serialize()
    assert "Image.arraySort" in text and "SYNTH_D1" in text and "Image.reduceResolution" in text
