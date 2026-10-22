"""Unit reducer masks and homogeneous temporal types (M005-S05 correction247, D178).

Server measurements these tests guard (they do not by themselves establish server
behaviour, and they cover only the measured synthetic operands and path): a constant
image given ``updateMask(1/2)`` read its mask back as 128/255 in float32; ``where``
carried its value's fractional mask to the output and ``reduceResolution`` weighted
every input by its mask, so the fraction was applied twice. Constant-built
observations got value-dependent inferred band types, and collection ``sum``, ``min``
and the sorted-array median all rejected a mixed collection. Production therefore keeps
only presence (mask > 0) as the S/W reducer-input mask, builds W in double and casts
each observation to double once, before any collection is built. Numbers come from
``coarse_oracle``. The final-coverage boundary tests at the end check the separate D179
operational floating-point rule; they do not change the exact oracle.
"""

from fractions import Fraction

import pytest

from geecomposer import coarse
from coarse_oracle import q, spatial, temporal
from ee_graph import Graph

NATIVE = [Fraction(k, 255) for k in (255, 128, 64, 191)]  # 1, 1/2, 1/4, 3/4 as stored (measured)
VALUES = [2, 4, 6, 8]
QUARTERS = ["1/4"] * 4


def call(graph, node):
    inv = graph.resolve(node)["functionInvocationValue"]
    return inv["functionName"], inv["arguments"]


def test_reducer_inputs_keep_presence_and_carry_the_fraction_once(real_ee):
    import ee

    values = ee.Image.constant([1, 2]).rename(["B4", "B8"]).updateMask(0.5)
    graph = Graph(coarse.sum_weight_inputs(values, None, ["S_B4", "S_B8", "W"]))
    name, args = call(graph, graph.root)
    assert name == "Image.where"  # still a mask-1 constant base: frozen stage-input shape
    assert call(graph, args["input"])[0] == "Image.setDefaultProjection"
    reset, reset_args = call(graph, args["value"])
    assert reset == "Image.updateMask"
    stacked = graph.resolve(reset_args["image"])
    presence, presence_args = call(graph, reset_args["mask"])
    assert presence == "Image.gt" and graph.constant(call(graph, presence_args["image2"])[1]["value"]) == 0
    mask_name, mask_args = call(graph, presence_args["image1"])
    assert mask_name == "Image.mask" and graph.resolve(mask_args["image"]) is stacked  # the value's own presence
    renamed, renamed_args = call(graph, reset_args["image"])
    assert renamed == "Image.rename"
    added, added_args = call(graph, renamed_args["input"])
    numerator, numerator_args = call(graph, added_args["dstImg"])
    assert added == "Image.addBands" and numerator == "Image.multiply"
    weight = graph.resolve(added_args["srcImg"])
    assert graph.resolve(numerator_args["image2"]) is weight  # fraction enters S once, and is W itself
    weight_name, weight_args = call(graph, added_args["srcImg"])
    support_name, support_args = call(graph, weight_args["image1"])
    assert weight_name == "Image.multiply" and support_name == "Image.toDouble"  # W in double, not float32
    assert call(graph, call(graph, support_args["value"])[1]["image"])[0] == "Image.mask"
    assert graph.resolve(call(graph, args["test"])[1]["image1"]) is weight
    assert not list(graph.calls("Image.unmask"))


def test_numeric_quality_is_a_value_weight_with_binary_presence(real_ee):
    import ee

    quality = ee.Image.constant(0.5)
    graph = Graph(coarse.sum_weight_inputs(ee.Image.constant([3]).rename(["x"]), quality, ["S_x", "W"]))
    products = list(graph.calls("Image.multiply"))
    assert any(call(graph, p["image2"])[0] == "Image.constant" and graph.constant(
        call(graph, p["image2"])[1]["value"]) == 0.5 for p in products)
    assert len(list(graph.calls("Image.updateMask"))) == 1


def test_measured_native_support_is_weighted_once_and_mixed_weights_expose_double_use():
    theta = Fraction(1, 2)
    w, s, mean = spatial(VALUES, NATIVE, QUARTERS, theta)
    assert (w, s, mean) == (Fraction(638, 1020), Fraction(2934, 1020), Fraction(2934, 638))
    doubled = sum(v * m * m for v, m in zip(VALUES, NATIVE)) / sum(m * m for m in NATIVE)
    # Server isolation (float32 masks): single use 4.598746267..., double use 4.197284892...
    assert abs(float(mean) - 4.598746267) < 1e-6 and abs(float(doubled) - 4.197284892) < 1e-6
    assert doubled != mean  # the mixed case cannot hide a second application of the masks
    uniform = [Fraction(128, 255)] * 4
    w, s, mean = spatial(VALUES, uniform, QUARTERS, theta)
    assert mean == 5 == sum(v * m * m for v, m in zip(VALUES, uniform)) / sum(m * m for m in uniform)
    assert (w, s) == (Fraction(128, 255), 5 * Fraction(128, 255)) != (q("1/2"), q("5/2"))


def test_float32_weights_would_miss_the_double_contract_tolerance():
    import struct

    def f32(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]

    supports = [f32(float(m)) for m in NATIVE]  # the float32 supports the server reported
    assert supports == [1.0, 0.501960813999176, 0.250980406999588, 0.7490196228027344]
    exact = sum(supports) / 4  # W of those supports in double
    assert f32(exact) == 0.6254901885986328  # the float32 W the server returned before the cast
    assert abs(f32(exact) - exact) > 1e-12 and abs(float(Fraction(638, 1020)) - exact) < 1e-7


def test_exact_numeric_quality_realizes_the_original_half_weight():
    w, s, mean = spatial(VALUES, ["1/2"] * 4, QUARTERS, Fraction(1, 2))
    assert (w, s, mean) == (q("1/2"), q("5/2"), 5)  # W = theta is valid: boundary kept
    w, s, mean = spatial(VALUES, ["1", "1/2", "1/4", "3/4"], QUARTERS, Fraction(1, 2))
    assert (w, s, mean) == (q("5/8"), q("23/8"), q("23/5"))
    assert spatial(VALUES, ["1/2", "1/2", "1/2", "0"], QUARTERS, Fraction(1, 2))[2] is None  # 3/8 < theta


def observations(ee, pairs):
    projection = coarse.grid(1000).projection()
    return [coarse._observation(ee.Image.constant([s, w]).rename(["S_x", "W"]).setDefaultProjection(projection),
                                ["x"], 0.5, projection) for s, w in pairs]


def test_every_temporal_collection_derives_from_one_double_cast_per_observation(real_ee):
    import ee

    obs = observations(ee, [(2, 1), (5, 0.5), (0.75, 0.25)])  # mixed inferred int/float/double types
    graph = Graph(coarse.temporal_statistics(obs, ["x"], list(coarse.TEMPORAL)))
    casts = list(graph.calls("Image.toDouble"))
    assert len(casts) == len(obs)
    cast_inputs = {id(graph.resolve(c["value"])) for c in casts}
    collections = list(graph.calls("ImageCollection.fromImages"))
    assert collections
    for collection in collections:
        images = graph.resolve(collection["images"])["arrayValue"]["values"]
        assert len(images) == len(obs)
        for image in images:  # each element is a cast or derives from exactly one cast observation
            assert list(graph.calls("Image.toDouble", image))
            assert not {id(graph.resolve(c["value"])) for c in graph.calls("Image.toDouble", image)} - cast_inputs


def test_cast_preserves_band_names_order_and_equal_observation_weights(real_ee):
    import ee

    obs = observations(ee, [(2, 1), (5, 0.5)])
    image = coarse.temporal_statistics(obs, ["x"], ["mean", "median", "sd_pop"])
    graph = Graph(image)
    mean = [c for c in graph.calls("Image.rename") if graph.constant(c["names"]) == ["x__s_mean__t_mean"]]
    divide = call(graph, call(graph, mean[0]["input"])[1]["image"])
    assert divide[0] == "Image.divide"  # equal-observation mean: sum of values / valid count
    assert [graph.constant(call(graph, divide[1][k])[1]["bandSelectors"]) for k in ("image1", "image2")] == [["x"], ["valid"]]
    text = image.serialize()
    for band in ("x__s_mean__t_mean", "x__s_mean__t_median", "x__s_mean__t_sd_pop", "obs__t_count", "cov__t_mean"):
        assert band in text
    assert "Image.cast" not in text and "Reducer.median" not in text and "Image.unmask" not in text


@pytest.mark.parametrize("values, expected", [
    ([], dict(count=0, mean=None, median=None, var_pop=None)),
    (["7/2"], dict(count=1, mean="7/2", median="7/2", var_pop=0)),
    ([2, 10], dict(count=2, mean=6, median=6, var_pop=16)),
    ([8, 2, 2], dict(count=3, mean=4, median=2, var_pop=8)),
    ([1, 3, 7, 9], dict(count=4, mean=5, median=5, var_pop=10)),
])
def test_equal_observation_statistics_after_a_type_only_cast(values, expected):
    got = temporal([q(v) for v in values])
    assert {k: got[k] for k in expected} == {k: v if k == "count" or v is None else q(v) for k, v in expected.items()}
    if values:  # unequal coverage never weights the temporal mean (pooled S/W differs)
        weights = [q(w) for w in ("1", "1/2", "3/4", "1/4")[:len(values)]]
        pooled = sum(q(v) * w for v, w in zip(values, weights)) / sum(weights)
        assert got["mean"] == sum(q(v) for v in values) / len(values)
        assert len(values) == 1 or pooled != got["mean"]


# ------------------------------------------------------------------ D179 final coverage boundary (operational)

SAVED_JUST_BELOW = 0.49999999999999983  # correction247 C-V3 F-ABSENT: exact 1/2 after the staged double means


def evaluate(graph, node):
    """Tiny IEEE-double evaluator for the constant-image operations of the admission graph."""
    node = graph.resolve(node)
    if "constantValue" in node:
        return node["constantValue"]
    if "arrayValue" in node:
        return [evaluate(graph, v) for v in node["arrayValue"]["values"]]
    name, args = call(graph, node)
    if name == "Image.constant":
        value = evaluate(graph, args["value"])
        return dict(zip([f"constant_{i}" for i in range(len(value))], value)) if isinstance(value, list) else {"constant": value}
    if name in ("Image.setDefaultProjection", "Image.toDouble"):
        return evaluate(graph, args["image"] if "image" in args else args["value"])
    if name == "Image.rename":
        return dict(zip(evaluate(graph, args["names"]), evaluate(graph, args["input"]).values()))
    if name == "Image.select":
        bands = evaluate(graph, args["input"])
        return {b: bands[b] for b in evaluate(graph, args["bandSelectors"])}
    if name == "Image.abs":
        (key, value), = evaluate(graph, args["value"]).items()
        return {key: abs(value)}
    ops = {"Image.gte": lambda a, b: int(a >= b), "Image.gt": lambda a, b: int(a > b), "Image.lte": lambda a, b: int(a <= b),
           "Image.subtract": lambda a, b: a - b, "Image.divide": lambda a, b: a / b,
           "Image.and": lambda a, b: int(bool(a) and bool(b)), "Image.or": lambda a, b: int(bool(a) or bool(b))}
    (key, left), = evaluate(graph, args["image1"]).items()
    (_, right), = evaluate(graph, args["image2"]).items()
    return {key: ops[name](left, right)}


def admitted(ee, weight, theta):
    image = coarse._coverage_admitted(ee.Image.constant(weight), theta)
    graph = Graph(image)
    (value,) = evaluate(graph, graph.root).values()
    return value


def neighbours(theta):
    """The representable doubles just inside and just outside |W - theta| <= 1e-12, below theta."""
    import math

    w = theta - coarse.COVERAGE_TOLERANCE
    while abs(w - theta) > coarse.COVERAGE_TOLERANCE:
        w = math.nextafter(w, theta)
    while abs(math.nextafter(w, -math.inf) - theta) <= coarse.COVERAGE_TOLERANCE:
        w = math.nextafter(w, -math.inf)
    return w, math.nextafter(w, -math.inf)


def test_tolerance_is_a_fixed_absolute_coverage_fraction():
    assert coarse.COVERAGE_TOLERANCE == 1e-12
    assert "tolerance" not in coarse.composite.__code__.co_varnames  # not configurable per request


@pytest.mark.parametrize("theta", [0.5, 1.0, 1e-13, 0.37])
def test_final_admission_boundary_on_representable_neighbours(real_ee, theta):
    import ee
    import math

    inside, outside = neighbours(theta)
    assert abs(inside - theta) <= 1e-12 < abs(outside - theta) and outside < inside < theta
    assert admitted(ee, theta, theta) == 1  # exact equality
    assert admitted(ee, inside, theta) == (1 if inside > 0 else 0) and admitted(ee, outside, theta) == 0
    assert admitted(ee, math.nextafter(theta, 2), theta) == 1
    assert admitted(ee, 0.0, theta) == 0  # zero support is never admitted, even for tiny theta
    assert admitted(ee, -1e-15, theta) == 0


def test_saved_just_below_half_is_admitted_and_clear_shortfalls_are_not(real_ee):
    import ee

    assert admitted(ee, SAVED_JUST_BELOW, 0.5) == 1
    assert admitted(ee, 0.5 - 2e-12, 0.5) == 0
    assert admitted(ee, 0.3, 0.5) == 0 and admitted(ee, 0.4999, 0.5) == 0
    assert admitted(ee, 1 - 1e-13, 1.0) == 1 and admitted(ee, 0.999, 1.0) == 0
    assert admitted(ee, 5e-14, 1e-13) == 1  # positive support inside the fixed tolerance of a tiny theta


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_coverage_is_never_admitted(weight):
    import math

    # ee.Image.constant cannot carry a nonfinite literal; evaluate the same predicate
    # expression shape directly (the server construction is checked by the controls).
    finite = abs(weight) <= coarse.MAX_DOUBLE
    near = abs(weight - 0.5) <= coarse.COVERAGE_TOLERANCE
    assert not (finite and weight > 0 and (weight >= 0.5 or near))
    assert not math.isfinite(weight)


def observation_test(ee, s, w, theta=0.5):
    projection = coarse.grid(1000).projection()
    best = ee.Image.constant([s, w]).rename(["S_x", "W"]).setDefaultProjection(projection)
    graph = Graph(coarse._observation(best, ["x"], theta, projection))
    name, args = call(graph, graph.root)
    assert name == "Image.where"
    return graph, args


@pytest.mark.parametrize("s, w, valid", [(3.9999999999999987, SAVED_JUST_BELOW, 1), (4.0, 0.5, 1), (0.0, 0.0, 0),
                                          (1.5, 0.3, 0), (4.0, 0.5 - 2e-12, 0)])
def test_coarse_first_observation_admits_the_saved_boundary_and_keeps_raw_s_and_w(real_ee, s, w, valid):
    import ee

    graph, args = observation_test(ee, s, w)
    (flag,) = evaluate(graph, args["test"]).values()
    assert flag == valid
    stacked = graph.resolve(call(graph, args["value"])[1]["input"])  # rename(addBands(addBands(S/W, W), 1))
    outer = stacked["functionInvocationValue"]["arguments"]
    inner = graph.resolve(outer["dstImg"])["functionInvocationValue"]["arguments"]
    assert evaluate(graph, inner["srcImg"]) == {"W": w}  # reported coverage is the raw double W
    if w:  # the zero-W branch is never selected (test is 0); S/W itself stays unrounded and unclamped
        (ratio,) = evaluate(graph, inner["dstImg"]).values()
        assert ratio == s / w


def test_both_final_admission_sites_share_the_one_predicate(real_ee, runner_offline, monkeypatch):
    from test_coarse_periods import S2, snapshot
    from test_coarse_reference import FINE

    seen = []
    original = coarse._coverage_admitted
    monkeypatch.setattr(coarse, "_coverage_admitted", lambda weight, theta: seen.append(theta) or original(weight, theta))
    coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snapshot(), coverage_min=0.37, **S2)
    coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(), fine_grid=FINE,
                                coverage_min=0.41, temporal=("median",), **S2)
    assert 0.37 in seen and 0.41 in seen and set(seen) == {0.37, 0.41}


def test_reference_coverage_band_is_the_raw_final_weight(real_ee, runner_offline):
    from test_coarse_periods import S2, snapshot
    from test_coarse_reference import FINE

    image = coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(), fine_grid=FINE,
                                        temporal=("median",), **S2)
    graph = Graph(image)
    (cov,) = [c for c in graph.calls("Image.rename") if graph.constant(c["names"]) == ["cov__s_mean"]]
    select = call(graph, cov["input"])
    assert select[0] == "Image.select" and graph.constant(select[1]["bandSelectors"]) == ["W"]
    assert call(graph, select[1]["input"])[0] == "Image.where"  # the staged rebase itself, never thresholded
    lte = [c for c in graph.calls("Image.lte") if call(graph, c["image2"])[0] == "Image.constant"
           and graph.constant(call(graph, c["image2"])[1]["value"]) == 1e-12]
    assert lte  # the fine-first final admission uses the shared fixed tolerance
