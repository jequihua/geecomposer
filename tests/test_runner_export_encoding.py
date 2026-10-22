"""Runner final export encoding (M004-S16, corrected in M004-S19): masked cells carry -9999.

Native-client graph assertions (installed EE client, offline algorithm table, the
real unstarted Drive/GCS constructors) fix WHERE and HOW the runner encodes: after
all science and the AOI selection (Image.clip), Image.unmask(-9999, sameFootprint=False)
then Image.toDouble, under the constructor's reproject and its clip to the aligned
export-window rectangle, with matching noData metadata. A tiny NumPy adapter states
the pixel semantics of the two encoding operations, and export_model reads the whole
request (neither is a server emulator). Nothing here proves server output.
"""

import json
import math

import ee
import numpy as np
import pytest

import cli_contract as C
from ee_graph import Graph
from export_model import chain as request_chain, deliver, rectangle
from geecomposer import runner_personal as personal
from geecomposer.export.drive import export_to_drive
from geecomposer.rao import RaoGrid
from geecomposer.recipe import RunnerError, canonical, pixel_bound, validate_recipe
from geecomposer import runner_science as rs
from geecomposer.runner_science import config_fingerprint, prepare
from runner_fakes import Clock, recipe_value
from test_runner_science import BOUNDS, evaluation

pytestmark = pytest.mark.usefixtures("runner_offline")

GRID = dict(crs="EPSG:32632", transform=[20, 0, 571660, 0, -20, 5539120])
GN_ANSWERS = [9, {f"{b}_{k}": v for b in ("B4", "B8", "B11")
                  for k, v in (("count", 9), ("mean", 0.5), ("min", 0), ("max", 1))},
              {f"c_{i}_{j}": 1.0 if i == j else 0.0 for i in range(3) for j in range(3)}]


def build(monkeypatch, *, destination, workflow, bounds=BOUNDS):
    value = recipe_value()
    value.update(grid=GRID, destination=destination)
    answers = [bounds, 1, ["scene_a"]]
    if workflow == "rao_gn":
        value.update(workflow="rao", bands=["B4", "B8", "B11"], rao={"normalization": "gn", "window_size": 3})
        value.pop("transform")
        answers += GN_ANSWERS
    pending = evaluation(monkeypatch, answers)
    monkeypatch.setattr(ee.batch.Task, "start", lambda *a: pytest.fail("task started"))
    recipe = personal.validate(value) if "drive_folder" in destination else validate_recipe(value)
    (task,), provenance = prepare(recipe, "products/run" if "bucket" in destination else "run-x", Clock().budget())
    assert pending == []
    return recipe, task, provenance


def call(node, name):
    assert node.func.getSignature()["name"] == name, node.func.getSignature()["name"]
    return node.args


def chain(config):
    """clip(window) -> reproject -> toDouble -> unmask -> clip(AOI) -> science product."""
    clipped = call(config["expression"], "Image.clip")
    projected = call(clipped["input"], "Image.reproject")
    doubled = call(projected["image"], "Image.toDouble")
    filled = call(doubled["value"], "Image.unmask")
    selected = call(filled["input"], "Image.clip")
    return clipped, projected, filled, selected


DESTINATIONS = {"drive": dict(drive_folder="geecomposer-test"),
                "gcs": dict(bucket="output-bucket", prefix="products/test")}


@pytest.mark.parametrize("workflow", ["composite", "rao_gn"])
@pytest.mark.parametrize("kind", sorted(DESTINATIONS))
def test_final_boundary_fills_after_science_with_declared_double_nodata(real_ee, monkeypatch, kind, workflow):
    recipe, task, provenance = build(monkeypatch, destination=DESTINATIONS[kind], workflow=workflow)
    config = task["config"]
    clipped, projected, filled, selected = chain(config)
    # the encoding: explicit value and explicit footprint semantics
    assert filled["value"].serialize() == ee.Image.constant(-9999).serialize() and filled["sameFootprint"] is False
    # unchanged native grid metadata from the constructor
    assert projected["crs"].serialize() == ee.Projection(GRID["crs"]).serialize()
    assert json.loads(projected["crsTransform"].serialize()) == json.loads(ee.List(GRID["transform"]).serialize())
    # the AOI selection precedes the fill; the constructor clips to the recorded aligned window
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    assert selected["geometry"].serialize() == region.serialize()
    w = provenance["export_window"]
    assert w == dict(col_off=0, row_off=0, width=10, height=10)  # the BOUNDS pixel response
    assert rectangle(clipped["geometry"], RaoGrid(**GRID)) == [
        w["col_off"], w["row_off"], w["col_off"] + w["width"], w["row_off"] + w["height"]]
    options = config["fileExportOptions"]
    assert options["geoTiffOptions"]["noData"] == {"floatValue": -9999}
    assert provenance["nodata"] == -9999 and provenance["dtype"] == "float64"
    assert config["workloadTag"] == "" and task["fingerprint"] == config_fingerprint(config)
    # all science sits below the AOI selection; nothing but selection/encoding/constructor above it
    science = Graph(selected["input"])
    whole = Graph(config["expression"])
    for name in ("reduce.median", "ImageCollection.fromImages") + (
            ("Image.neighborhoodToArray",) if workflow == "rao_gn" else ()):
        assert len(list(science.calls(name))) == len(list(whole.calls(name))) > 0, name
    fills = [a for a in whole.calls("Image.unmask") if sentinel(whole, a["value"])]
    assert len(fills) == 1  # exactly one sentinel fill, at the boundary


def sentinel(graph, node):
    node = graph.resolve(node)
    call_ = node.get("functionInvocationValue")
    return bool(call_ and call_["functionName"] == "Image.constant"
                and graph.constant(call_["arguments"]["value"]) == -9999)


def test_drive_and_gcs_requests_share_the_same_encoded_expression(real_ee, monkeypatch):
    _, drive, _ = build(monkeypatch, destination=DESTINATIONS["drive"], workflow="rao_gn")
    _, gcs, _ = build(monkeypatch, destination=DESTINATIONS["gcs"], workflow="rao_gn")
    assert drive["config"]["expression"].serialize() == gcs["config"]["expression"].serialize()


def test_shared_helper_is_exactly_fill_then_double(real_ee):
    image = ee.Image.constant([1, 2]).rename(["a", "b"]).updateMask(ee.Image.constant(0.5))
    assert rs.EXPORT_NODATA == -9999
    doubled = call(rs.export_encoded(image), "Image.toDouble")
    filled = call(doubled["value"], "Image.unmask")
    assert filled["input"].serialize() == image.serialize()
    assert filled["value"].serialize() == ee.Image.constant(-9999).serialize() and filled["sameFootprint"] is False


def test_public_drive_helper_stays_metadata_only(real_ee):
    image = ee.Image.constant(1).toDouble()
    task = export_to_drive(image, "masked", "folder", dict(type="Polygon", coordinates=BOUNDS), max_pixels=2500,
                           no_data=-9999, **{
        "crs": GRID["crs"], "crs_transform": GRID["transform"]})
    assert "Image.unmask" not in task.config["expression"].serialize()


# ------------------------------------------------------------------ pixel semantics (test-only adapter)


def adapter(values, mask):
    """unmask(-9999, sameFootprint=False) then toDouble, per band: only mask==0 pixels change."""
    values = np.asarray(values, dtype=float)
    return np.where(np.asarray(mask, dtype=float) == 0, -9999.0, values).astype(np.float64)


def audit_valid(encoded):
    """What a nodata-aware reader/audit derives: finite and not the declared sentinel."""
    return np.isfinite(encoded) & (encoded != -9999.0)


CASES = {
    # name: (values per band, masks per band, expected encoded, expected audit consistency)
    "per_band_masks_differ": ([[0.2, 0.3], [5.0, 6.0]], [[1, 0], [1, 1]], [[0.2, -9999], [5.0, 6.0]], True),
    "valid_zero_kept": ([[0.0, 0.0]], [[1, 0]], [[0.0, -9999]], True),
    "finite_values_unchanged": ([[1e-12, 123456.75]], [[1, 1]], [[1e-12, 123456.75]], True),
    "fractional_mask_not_binarized": ([[0.4, 0.7]], [[0.5, 0.0]], [[0.4, -9999]], True),
    "masked_nan_becomes_sentinel": ([[math.nan, 1.0]], [[0, 1]], [[-9999, 1.0]], True),
    "nan_at_valid_pixel_stays_rejectable": ([[math.nan, 1.0]], [[1, 1]], [[math.nan, 1.0]], False),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_encoding_pixel_semantics(name):
    values, masks, expected, consistent = CASES[name]
    for v, m, e in zip(values, masks, expected):
        out = adapter(v, m)
        assert out.dtype == np.float64
        np.testing.assert_array_equal(out, np.asarray(e, dtype=float))
        # mask derived from the declared nodata must agree with finite validity -- unless a
        # valid science pixel is non-finite, which must stay visible (never a sentinel)
        tag_mask = out != -9999.0
        assert bool(np.array_equal(tag_mask, audit_valid(out))) is consistent


def test_original_nan_with_declared_tag_is_inconsistent():
    """The execution162 symptom: masked cells delivered as NaN under a -9999 tag."""
    delivered = np.array([math.nan, 0.25, math.nan])
    tag_mask = delivered != -9999.0
    assert not np.array_equal(tag_mask, audit_valid(delivered))


# ------------------------------------------------------------------ M004-S19: the complete native request
# The S16 helper filled correctly, but the native constructor's AOI clip came AFTER the fill, so
# cells outside the AOI inside the export window were masked again (the leading explanation of
# execution181's border NaNs). These tests read the real request through export_model's
# explicitly limited model; they are not server evidence.

# AOI bounds response (grid-pixel coordinates) cutting through pixels; floor/ceil gives this window.
# On GRID it is the projected box x 571625..571715, y 5539065..5539135.
WINDOW_BOUNDS = [[[-1.75, -0.75], [2.75, -0.75], [2.75, 2.75], [-1.75, 2.75], [-1.75, -0.75]]]
WINDOW = dict(col_off=-2, row_off=-1, width=5, height=4)
NAN = math.nan
# AOI coverage per window cell: border cells outside (0), partial fractions, inside (1).
COVERAGE = [[0, 0.5, 1, 1, 0], [0.25, 1, 1, 1, 1], [1, 1, 1, 1, 1], [0, 1, 1, 1, 0.5]]
SCIENCE = [  # per band (values, mask); the masks differ by band; (2, 2) is missing in every band
    ([[7, 0.0, 0.4, 2, 3], [NAN, 0.0, 1e-12, 123456.75, 5], [NAN, 1, 1, 1, 1], [1, 1, 1, 1, 1]],
     [[1, 1, 1, 1, 1], [0, 1, 1, 1, 0.5], [1, 1, 0, 1, 1], [1, 1, 1, 1, 1]]),
    ([[1] * 5] * 4, [[1, 1, 1, 1, 1], [1, 0, 1, 1, 1], [1, 1, 0, 1, 1], [1, 1, 1, 0, 1]]),
    ([[0.5] * 5] * 4, [[1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [1, 1, 0, 1, 1], [1, 1, 1, 1, 1]]),
]


def expected_delivery(bands, coverage):
    """The contract: -9999 wherever a band's science mask or the AOI selection leaves a window cell
    missing; every other value unchanged (zero, fractional masks, a NaN at a valid pixel)."""
    cov = np.asarray(coverage, float)
    return np.array([np.where(np.minimum(np.asarray(m, float), cov) > 0, np.asarray(v, float), -9999.0)
                     for v, m in bands])


@pytest.mark.parametrize("workflow", ["composite", "rao_gn"])
@pytest.mark.parametrize("kind", sorted(DESTINATIONS))
def test_complete_request_encodes_every_missing_window_cell(real_ee, monkeypatch, kind, workflow):
    recipe, task, provenance = build(monkeypatch, destination=DESTINATIONS[kind], workflow=workflow,
                                     bounds=WINDOW_BOUNDS)
    config = task["config"]
    bands = SCIENCE if workflow == "rao_gn" else SCIENCE[:1]
    grid = RaoGrid(**recipe["grid"])
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    delivered, product, calls = deliver(config["expression"], grid=grid, window=WINDOW, aoi=region,
                                        coverage=COVERAGE, bands=bands)
    # S16 order: the outside-AOI border cells come back NaN here instead of -9999
    np.testing.assert_array_equal(delivered, expected_delivery(bands, COVERAGE))
    a = delivered[0]
    assert a[0, 0] == a[0, 4] == a[3, 0] == -9999.0  # border cells outside the AOI
    assert a[1, 0] == -9999.0 and a[2, 2] == -9999.0  # masked NaN; missing in every band
    assert a[0, 1] == a[1, 1] == 0.0 and a[1, 2] == 1e-12 and a[1, 4] == 5.0 and a[3, 4] == 1.0
    assert np.isnan(delivered).sum() == 1 and np.isnan(a[2, 0])  # a valid-pixel NaN is never replaced
    if workflow == "rao_gn":  # band-specific missingness stays band-specific
        assert delivered[1, 1, 1] == delivered[1, 3, 3] == -9999.0 and delivered[2, 1, 1] == 0.5
    # exactly the corrected request: one window-bounded fill after the AOI selection, science below
    assert calls == ["Image.clip", "Image.reproject", "Image.toDouble", "Image.unmask", "Image.clip"]
    selection = config["expression"].args["input"].args["image"].args["value"].args["input"]
    assert selection.args["geometry"].serialize() == region.serialize()
    assert rectangle(config["expression"].args["geometry"], grid) == [-2, -1, 3, 3]
    assert WINDOW["width"] * WINDOW["height"] <= int(config["maxPixels"]["value"])
    assert provenance["export_window"] == WINDOW
    science, whole = Graph(product), Graph(config["expression"])
    for name in ("reduce.median", "ImageCollection.fromImages") + (
            ("Image.neighborhoodToArray",) if workflow == "rao_gn" else ()):
        assert len(list(science.calls(name))) == len(list(whole.calls(name))) > 0, name
    assert len([a for a in whole.calls("Image.unmask") if sentinel(whole, a["value"])]) == 1
    assert sum(sentinel(science, a["value"]) for a in science.calls("Image.unmask")) == 0
    assert config["fileExportOptions"]["geoTiffOptions"]["noData"] == {"floatValue": -9999}
    assert task["fingerprint"] == config_fingerprint(config)


def test_real_personal_cli_request_is_the_window_bounded_encoding(tmp_path, monkeypatch, capsys):
    """The real CLI, existing offline service fakes and native serialization of the sent request."""
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(tmp_path / "home"))
    service = C.Service(monkeypatch)
    code, out, data = C.run_cli(tmp_path, capsys, canonical(C.rao_recipe()))
    assert code == 0, out
    ((_, config),) = service.exports
    state = json.loads(data)["state"]
    assert config_fingerprint(config) == state["tasks"][0]["fingerprint"]  # the recorded digest binds it
    sent = ee.deserializer.fromCloudApiJSON(ee.serializer.toJSON(config["expression"]))
    assert ee.serializer.toJSON(sent) == ee.serializer.toJSON(config["expression"])
    recipe = state["recipe"]
    grid = RaoGrid(**recipe["grid"])
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    window = dict(col_off=0, row_off=0, width=36, height=56)  # the fixture's pixel-aligned bounds
    coverage = np.ones((56, 36))
    coverage[0, 0] = coverage[-1, -1] = coverage[0, -1] = 0  # border cells outside the AOI
    bands = [(np.full((56, 36), v), np.ones((56, 36))) for v in (0.25, 9.0, 1.0)]
    delivered, _, calls = deliver(sent, grid=grid, window=window, aoi=region, coverage=coverage, bands=bands)
    np.testing.assert_array_equal(delivered, expected_delivery(bands, coverage))
    assert not np.isnan(delivered).any() and (delivered == -9999.0).sum() == 9
    assert calls == ["Image.clip", "Image.reproject", "Image.toDouble", "Image.unmask", "Image.clip"]
    assert state["provenance"]["export_window"] == window


@pytest.mark.parametrize("crs_bounds,transform,expected", [
    ((571625, 5539065, 571715, 5539135), GRID["transform"], dict(col_off=-2, row_off=-1, width=5, height=4)),
    ((571660, 5538000, 572380, 5539120), GRID["transform"], dict(col_off=0, row_off=0, width=36, height=56)),
    ((0, 0, 0.001, 0.001), [0.0001, 0, 0, 0, -0.0001, 0.001], dict(col_off=0, row_off=0, width=10, height=10)),
])
def test_axis_aligned_window_and_admission_bound_are_unchanged(crs_bounds, transform, expected):
    """On axis-aligned grids the pixel box of a CRS box is exact, so the window and the admission
    bound equal what the CRS-box calculation (recipe.pixel_bound) gives."""
    x0, y0, x1, y1 = crs_bounds
    (ring,) = exact_bounds([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], transform)
    window = rs.export_window((*ring[0], *ring[2]))
    assert window == expected
    assert (window["width"] + 4) * (window["height"] + 4) == pixel_bound(crs_bounds, transform)


@pytest.mark.parametrize("response", [
    [[[0, 0], [math.nan, 0], [10, 10], [0, 10], [0, 0]]],  # NaN that min/max alone would skip
    [[[0, 0], [math.inf, 0], [10, 10], [0, 10], [0, 0]]],
    [[[3, 0], [3, 0], [3, 10], [3, 10], [3, 0]]],  # zero-width window
    [[[0, 0], ["10", 0], [10, 10], [0, 10], [0, 0]]],
    [[[0], [10], [10], [0]]],
])
def test_malformed_bounds_response_fails_closed(real_ee, monkeypatch, response):
    evaluation(monkeypatch, [response])
    with pytest.raises(RunnerError) as exc:
        prepare(validate_recipe(recipe_value()), "products/run", Clock().budget())
    assert exc.value.status == "uncertain" and exc.value.phase == "bounds"


# ------------------------------------------------------------------ M004-S19 r2: affine-grid export extent
# The window must bound the AOI itself in the grid's full affine pixel coordinates (shear and
# rotation included), not the inverted corners of its CRS-aligned box (review r1 F1).

AFFINE_CASES = {  # name: (EPSG:4326 transform, closed AOI ring, independently expected window)
    "rotated_review_f1": ([0.001, 0.001, 0, 0.001, -0.001, 0],
                          [(0, 0), (0.01, 0.01), (0.02, 0), (0.01, -0.01), (0, 0)],
                          dict(col_off=0, row_off=0, width=10, height=10)),
    "axis_aligned_control": ([0.001, 0, 0, 0, -0.001, 0.01],
                             [(0.0005, 0.0005), (0.0095, 0.0005), (0.0095, 0.0085), (0.0005, 0.0085), (0.0005, 0.0005)],
                             dict(col_off=0, row_off=1, width=10, height=9)),
    "shear": ([0.001, 0.0005, 0, 0, -0.001, 0],
              [(0.00315, -0.00125), (0.00715, -0.00125), (0.008875, -0.00475), (0.004875, -0.00475), (0.00315, -0.00125)],
              dict(col_off=2, row_off=1, width=5, height=4)),
}


def exact_bounds(ring, transform=None):
    """Independent exact arithmetic: the AOI's bounding box in CRS coordinates, or (with a transform)
    in that affine grid's pixel coordinates. Straight lon/lat edges stay straight under an affine map,
    so the vertices bound the polygon."""
    from fractions import Fraction as Q

    points = [(Q(str(x)), Q(str(y))) for x, y in ring]
    if transform is not None:
        a, b, c, d, e, f = (Q(str(v)) for v in transform)
        det = a * e - b * d
        points = [((e * (x - c) - b * (y - f)) / det, (a * (y - f) - d * (x - c)) / det) for x, y in points]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    lo_x, lo_y, hi_x, hi_y = (float(v) for v in (min(xs), min(ys), max(xs), max(ys)))
    return [[[lo_x, lo_y], [hi_x, lo_y], [hi_x, hi_y], [lo_x, hi_y], [lo_x, lo_y]]]


def projection_args(proj):
    """(crs, transform or None) of a Projection(...) expression."""
    graph = Graph(proj)
    call = graph.root["functionInvocationValue"]
    assert call["functionName"] == "Projection", call["functionName"]
    args = call["arguments"]
    return graph.constant(args["crs"]), graph.constant(args["transform"]) if "transform" in args else None


def bounds_query(obj):
    """The Geometry.bounds node of a coordinates() query, else None."""
    if not list(Graph(obj).calls("Geometry.bounds")):
        return None
    assert obj.func.getSignature()["name"] == "Geometry.coordinates"
    query = obj.args["geometry"]
    assert query.func.getSignature()["name"] == "Geometry.bounds"
    return query


def truthful_service(monkeypatch, ring, answers):
    """computeValue that answers the bounds query in exactly the projection the query asks for
    (CRS-aligned box or full affine pixel box); every other query takes the next scripted answer."""
    queries = []

    def compute(obj, *args, **kwargs):
        queries.append(obj)
        query = bounds_query(obj)
        if query is not None:
            crs, transform = projection_args(query.args["proj"])
            assert crs == "EPSG:4326"
            return exact_bounds(ring, transform)
        assert answers, "unexpected remote evaluation"
        return answers.pop(0)

    monkeypatch.setattr(ee.data, "computeValue", compute)
    return queries


@pytest.mark.parametrize("case", sorted(AFFINE_CASES))
def test_affine_grid_window_bounds_the_aoi_in_full_pixel_coordinates(real_ee, monkeypatch, case):
    transform, ring, expected = AFFINE_CASES[case]
    requests = {}
    for kind in sorted(DESTINATIONS):
        value = recipe_value()
        value.update(grid=dict(crs="EPSG:4326", transform=transform), destination=DESTINATIONS[kind],
                     aoi=dict(type="Polygon", coordinates=[[list(p) for p in ring]]))
        answers = [1, ["scene_a"]]
        queries = truthful_service(monkeypatch, ring, answers)
        monkeypatch.setattr(ee.batch.Task, "start", lambda *a: pytest.fail("task started"))
        recipe = personal.validate(value) if kind == "drive" else validate_recipe(value)
        (task,), provenance = prepare(recipe, "products/run" if kind == "gcs" else "run-x", Clock().budget())
        assert answers == []
        config = task["config"]
        # the window and every place it is bound: provenance, the request rectangle, the admission bound
        assert provenance["export_window"] == expected, provenance["export_window"]
        grid = RaoGrid(**recipe["grid"])
        assert rectangle(config["expression"].args["geometry"], grid) == [
            expected["col_off"], expected["row_off"], expected["col_off"] + expected["width"],
            expected["row_off"] + expected["height"]]
        assert provenance["pixels_upper_bound"] == (expected["width"] + 4) * (expected["height"] + 4)
        assert expected["width"] * expected["height"] <= int(config["maxPixels"]["value"])
        # the ONE bounds evaluation asks for this AOI in the full affine projection
        bounds = [q for q in queries if bounds_query(q) is not None]
        assert len(bounds) == 1 and queries[0] is bounds[0] and len(queries) == 3
        query = bounds_query(bounds[0])
        assert projection_args(query.args["proj"]) == ("EPSG:4326", [float(v) for v in transform])
        region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
        assert query.args["geometry"].serialize() == region.serialize()
        # original grid metadata and the corrected nodata order are unchanged
        assert [name for name, _ in request_chain(config["expression"])[0]] == [
            "Image.clip", "Image.reproject", "Image.toDouble", "Image.unmask", "Image.clip"]
        projected = config["expression"].args["input"].args
        assert json.loads(projected["crsTransform"].serialize()) == json.loads(ee.List(transform).serialize())
        assert task["fingerprint"] == config_fingerprint(config)
        requests[kind] = config["expression"].serialize()
    assert requests["drive"] == requests["gcs"]
