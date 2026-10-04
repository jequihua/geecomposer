"""Runner final export encoding (M004-S16): masked cells carry the declared -9999.

Native-client graph assertions (installed EE client, offline algorithm table, the
real unstarted Drive/GCS constructors) fix WHERE and HOW the runner encodes: after
all science, Image.unmask(-9999, sameFootprint=False) then Image.toDouble, under the
constructor's unchanged reproject/clip, with matching noData metadata. A tiny
NumPy adapter states the pixel semantics of exactly those two operations (it is
not a server emulator). Nothing here proves server output.
"""

import json
import math

import ee
import numpy as np
import pytest

from ee_graph import Graph
from geecomposer import runner_personal as personal
from geecomposer.export.drive import export_to_drive
from geecomposer.recipe import validate_recipe
from geecomposer import runner_science as rs
from geecomposer.runner_science import config_fingerprint, prepare
from runner_fakes import Clock, recipe_value
from test_runner_science import BOUNDS, evaluation

pytestmark = pytest.mark.usefixtures("runner_offline")

GRID = dict(crs="EPSG:32632", transform=[20, 0, 571660, 0, -20, 5539120])
GN_ANSWERS = [9, {f"{b}_{k}": v for b in ("B4", "B8", "B11")
                  for k, v in (("count", 9), ("mean", 0.5), ("min", 0), ("max", 1))},
              {f"c_{i}_{j}": 1.0 if i == j else 0.0 for i in range(3) for j in range(3)}]


def build(monkeypatch, *, destination, workflow):
    value = recipe_value()
    value.update(grid=GRID, destination=destination)
    answers = [BOUNDS, 1, ["scene_a"]]
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
    """clip -> reproject -> toDouble -> unmask -> science product (returned)."""
    clipped = call(config["expression"], "Image.clip")
    projected = call(clipped["input"], "Image.reproject")
    doubled = call(projected["image"], "Image.toDouble")
    filled = call(doubled["value"], "Image.unmask")
    return clipped, projected, filled


DESTINATIONS = {"drive": dict(drive_folder="geecomposer-test"),
                "gcs": dict(bucket="output-bucket", prefix="products/test")}


@pytest.mark.parametrize("workflow", ["composite", "rao_gn"])
@pytest.mark.parametrize("kind", sorted(DESTINATIONS))
def test_final_boundary_fills_after_science_with_declared_double_nodata(real_ee, monkeypatch, kind, workflow):
    recipe, task, provenance = build(monkeypatch, destination=DESTINATIONS[kind], workflow=workflow)
    config = task["config"]
    clipped, projected, filled = chain(config)
    # the encoding: explicit value and explicit footprint semantics
    assert filled["value"].serialize() == ee.Image.constant(-9999).serialize() and filled["sameFootprint"] is False
    # unchanged native grid/region metadata from the constructor
    assert projected["crs"].serialize() == ee.Projection(GRID["crs"]).serialize()
    assert json.loads(projected["crsTransform"].serialize()) == json.loads(ee.List(GRID["transform"]).serialize())
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    assert clipped["geometry"].serialize() == region.serialize()
    options = config["fileExportOptions"]
    assert options["geoTiffOptions"]["noData"] == {"floatValue": -9999}
    assert provenance["nodata"] == -9999 and provenance["dtype"] == "float64"
    assert config["workloadTag"] == "" and task["fingerprint"] == config_fingerprint(config)
    # all science sits below the fill; nothing but the encoding/constructor above it
    science = Graph(filled["input"])
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
