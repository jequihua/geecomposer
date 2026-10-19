"""S03: native metadata capture (C1/C2/Cloud Score+) and durable snapshot replay.

Responses are realistic, finite fakes of the documented ``ImageCollection.getInfo``
schema, returned by a replaced ``ee.data.computeValue`` behind the installed client's
real graph construction (``real_ee``); the production decoder and the accepted
``coarse.build_snapshot`` validation run unchanged. SYNTHETIC: no catalogue claim.
"""

import copy
import json
from datetime import datetime, timezone

import ee
import pytest

from geecomposer import coarse, runner_coarse
from geecomposer.recipe import RunnerError, validate_policy, validate_recipe
from runner_fakes import policy_value
from test_coarse_national import AOI, TILES

pytestmark = pytest.mark.usefixtures("runner_offline")

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
CS_COLLECTION = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
S1_COLLECTION = "COPERNICUS/S1_GRD_FLOAT"
BANDS = ["B2", "B3", "B4", "B8"]
TILE = 10980
BOUNDS = [[[900, -2400], [4000, -2400], [4000, -300], [900, -300], [900, -2400]]]


def ms(text):
    return round(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def band(name, tile, dims=(TILE, TILE), **extra):
    zone, c, f = TILES[tile] if isinstance(tile, str) else tile
    value = {"id": name, "data_type": {"type": "PixelType", "precision": "int", "min": 0, "max": 65535},
             "dimensions": list(dims), "crs": f"EPSG:326{zone}", "crs_transform": [10, 0, c, 0, -10, f]}
    value.update(extra)
    return value


def s2_feature(image_id, when, datatake, tile, spacecraft="Sentinel-2A", **band_extra):
    return {"type": "Image", "id": f"{S2_COLLECTION}/{image_id}", "version": 1749600000000000,
            "bands": [band(b, tile, **band_extra) for b in BANDS],
            "properties": {"system:index": image_id, "system:time_start": ms(when), "SPACECRAFT_NAME": spacecraft,
                           "DATATAKE_IDENTIFIER": f"GS2A_20250101T000000_{datatake:06d}_N05.11",
                           "CLOUDY_PIXEL_PERCENTAGE": 3.25, "MGRS_TILE": "12RXP"}}


def cs_feature(image_id, tile, **band_extra):
    return {"type": "Image", "id": f"{CS_COLLECTION}/{image_id}", "version": 1749600000000001,
            "bands": [band("cs", tile, **band_extra), band("cs_cdf", tile, **band_extra)],
            "properties": {"system:index": image_id}}


def s1_feature(image_id, when, datatake, tile, platform="A"):
    return {"type": "Image", "id": f"{S1_COLLECTION}/{image_id}", "version": 1,
            "bands": [band(b, tile) for b in ("VV", "VH", "angle")],
            "properties": {"system:index": image_id, "system:time_start": ms(when), "platform_number": platform,
                           "missionDataTakeID": datatake, "instrumentMode": "IW"}}


def collection(features):
    return {"type": "ImageCollection", "bands": [], "features": features, "properties": {}}


# Synthetic national-span acquisitions: datatake 1 crosses zones 11/12/13 (12/13 seam)
# with a member after the run end; datatake 2 covers zones 12 and 16; datatake 9
# began before the run (excluded); datatake 3 is a July acquisition.
C1 = [
    s2_feature("SYNTH_N1_Z11", "2025-06-10T18:20:00Z", 1, "Z11"),
    s2_feature("SYNTH_N1_Z12", "2025-06-10T18:20:30Z", 1, "Z12"),
    s2_feature("SYNTH_N1_Z13", "2025-06-10T18:21:00Z", 1, "Z13"),
    s2_feature("SYNTH_N2_Z12", "2025-06-15T17:50:00Z", 2, "Z12", "Sentinel-2B"),
    s2_feature("SYNTH_N2_Z16", "2025-06-15T16:10:00Z", 2, "Z16", "Sentinel-2B"),
    s2_feature("SYNTH_N3_Z16", "2025-07-02T16:10:00Z", 3, "Z16"),
    s2_feature("SYNTH_N9_Z12", "2025-01-01T00:00:10Z", 9, "Z12"),
    s2_feature("SYNTH_N4_Z16", "2025-12-31T23:59:50Z", 4, "Z16"),
]
C2 = C1 + [
    s2_feature("SYNTH_N9_Z11", "2024-12-31T23:59:50Z", 9, "Z11"),
    s2_feature("SYNTH_N4_Z13", "2026-01-01T00:00:05Z", 4, "Z13"),
]
TILE_OF = {f["properties"]["system:index"]: f["properties"]["system:index"].rsplit("_", 1)[1] for f in C2}
CS = [cs_feature(i, tile) for i, tile in sorted(TILE_OF.items())]


class Answers:
    """Replaced EE evaluation boundary: queued responses, counted calls."""

    def __init__(self, monkeypatch, responses):
        self.pending, self.calls = list(responses), 0

        def compute(*args, **kwargs):
            self.calls += 1
            assert self.pending, "unexpected remote evaluation"
            return copy.deepcopy(self.pending.pop(0))

        monkeypatch.setattr(ee.data, "computeValue", compute)


def recipe(dataset="sentinel2", **section):
    value = dict(version=1, workflow="coarse", dataset=dataset, aoi=copy.deepcopy(AOI), start="2025-01-01",
                 end="2026-01-01", destination={"drive_folder": "geecomposer_coarse"},
                 coarse=dict(dict(resolution=1000, periods="monthly"), **section))
    if dataset == "sentinel2":
        value.update(bands=list(BANDS), mask="s2_cloud_score_plus")
    else:
        value.update(bands=["VV", "VH"], filters={"orbitPass": "DESCENDING", "polarizations": ["VV", "VH"]})
    return value


def plan(value=None, **section):
    value = validate_recipe(value or recipe(**section))
    return runner_coarse.validate(value, value["limits"])


class Budget:
    def timeout(self):
        return 10


def read(monkeypatch, responses, value=None, limit=1000):
    answers = Answers(monkeypatch, responses)
    snapshot = runner_coarse.read_snapshot(plan(value), Budget(), limit)
    return snapshot, answers


def test_realistic_responses_decode_into_a_complete_snapshot(real_ee, monkeypatch):
    snap, answers = read(monkeypatch, [collection(C1), collection(C2), collection(CS)])
    assert answers.calls == 3 and answers.pending == []
    members = {i for g in snap.groups for i in g[2]}
    assert members == {"SYNTH_N1_Z11", "SYNTH_N1_Z12", "SYNTH_N1_Z13", "SYNTH_N2_Z12", "SYNTH_N2_Z16",
                       "SYNTH_N3_Z16", "SYNTH_N4_Z16", "SYNTH_N4_Z13"}  # end-crossing member kept
    assert [k[1][-6:] for k in snap.excluded] == ["000009"]  # began before the run: excluded
    assert dict(snap.extents)["SYNTH_N1_Z12"] == (TILE, TILE)
    table = dict(snap.projections)["SYNTH_N2_Z16"]
    assert {b for b, _, _ in table} == {"B2", "B3", "B4", "B8", "cs_cdf"}
    assert {(c, t) for _, c, t in table} == {("EPSG:32616", (10.0, 0.0, 399960.0, 0.0, -10.0, 2300040.0))}


def test_sentinel1_reads_two_responses_and_no_cloud_score(real_ee, monkeypatch):
    s1 = [s1_feature("SYNTH_S1_Z12", "2025-06-12T00:10:00Z", 4242, "Z12"),
          s1_feature("SYNTH_S1_Z16", "2025-06-12T00:10:20Z", 4242, "Z16")]
    snap, answers = read(monkeypatch, [collection(s1), collection(s1)], recipe("sentinel1_float"))
    assert answers.calls == 2 and snap.bands == ("VH", "VV") and len(snap.groups) == 1


def test_an_empty_candidate_read_makes_one_call(real_ee, monkeypatch):
    snap, answers = read(monkeypatch, [collection([])])
    assert answers.calls == 1 and snap.groups == () and snap.extents == ()


def test_pixel_origin_is_folded_into_the_native_affine(real_ee, monkeypatch):
    shifted = [s2_feature("SYNTH_O", "2025-06-10T18:20:00Z", 1, "Z12", origin=[2, 3])]
    snap, _ = read(monkeypatch, [collection(shifted), collection(shifted),
                                 collection([cs_feature("SYNTH_O", "Z12", origin=[2, 3])])])
    _, c, f = TILES["Z12"]
    assert dict(snap.projections)["SYNTH_O"][0][2] == (10.0, 0.0, c + 20.0, 0.0, -10.0, f - 30.0)


@pytest.mark.parametrize(
    "mutate, status, message",
    [
        (lambda b: b.pop("dimensions"), "uncertain", "native dimensions missing from source metadata"),
        (lambda b: b.update(dimensions=[0, 10980]), "uncertain", "coarse source metadata unusable"),
        (lambda b: b.update(dimensions=[10980.0, 10980]), "uncertain", "coarse source metadata unusable"),
        (lambda b: b.update(dimensions=[True, 10980]), "uncertain", "coarse source metadata unusable"),
        (lambda b: b.update(dimensions=[10980]), "uncertain", "coarse source metadata unusable"),
        (lambda b: b.update(crs_transform=[10, 0, "x", 0, -10, 0]), "uncertain", "coarse source metadata unusable"),
        (lambda b: b.update(origin=[-1, 0]), "uncertain", "coarse source metadata unusable"),
        (lambda b: b.pop("crs"), "uncertain", "coarse source metadata unusable"),
    ],
)
def test_malformed_native_band_metadata_fails_clearly(real_ee, monkeypatch, mutate, status, message):
    rows = copy.deepcopy(C1[:1])
    mutate(rows[0]["bands"][2])
    with pytest.raises(RunnerError) as err:
        read(monkeypatch, [collection(rows)])
    assert (err.value.status, str(err.value)) == (status, message)


@pytest.mark.parametrize(
    "case, message",
    [
        ("id_mismatch", "coarse source metadata unusable"),
        ("missing_band", "coarse request refused: missing_metadata"),
        ("mixed_dimensions", "coarse request refused: source_projection_mismatch"),
        ("c2_time_changed", "coarse request refused: uncertain"),
        ("c2_dimensions_changed", "coarse request refused: source_projection_mismatch"),
        ("member_missing_from_c2", "coarse request refused: uncertain"),
        ("cloud_score_missing", "Cloud Score+ metadata missing for a source image"),
        ("duplicate_c1", "coarse request refused: uncertain"),
    ],
)
def test_contradictory_or_incomplete_reads_are_refused(real_ee, monkeypatch, case, message):
    c1, c2, cs = copy.deepcopy(C1), copy.deepcopy(C2), copy.deepcopy(CS)
    if case == "id_mismatch":
        c1[0]["id"] = f"{S2_COLLECTION}/SOMETHING_ELSE"
    elif case == "missing_band":
        c1[0]["bands"] = c1[0]["bands"][:3]
    elif case == "mixed_dimensions":
        c1[0]["bands"][3]["dimensions"] = [5490, 5490]
    elif case == "c2_time_changed":  # same image, candidate June 10, completion a day later
        c2[0]["properties"]["system:time_start"] = ms("2025-06-11T18:20:00Z")
    elif case == "c2_dimensions_changed":
        for b in c2[1]["bands"]:
            b["dimensions"] = [10980, 10979]
    elif case == "member_missing_from_c2":
        c2 = c2[1:]
    elif case == "cloud_score_missing":
        cs = cs[1:]
    elif case == "duplicate_c1":
        c1 = c1 + c1[:1]
    with pytest.raises(RunnerError) as err:
        read(monkeypatch, [collection(c1), collection(c2), collection(cs)])
    assert str(err.value) == message


def test_c1_and_c2_must_agree_on_native_dimensions(real_ee, monkeypatch):
    c1 = [s1_feature("SYNTH_S1_Z12", "2025-06-12T00:10:00Z", 4242, "Z12")]
    c2 = copy.deepcopy(c1)
    for b in c2[0]["bands"]:
        b["dimensions"] = [10980, 10979]
    with pytest.raises(RunnerError) as err:
        read(monkeypatch, [collection(c1), collection(c2)], recipe("sentinel1_float"))
    assert str(err.value) == "coarse request refused: uncertain"


def test_read_overflow_is_detected_before_the_completion_read(real_ee, monkeypatch):
    with pytest.raises(RunnerError) as err:
        read(monkeypatch, [collection(C1[:6])], limit=5)
    assert str(err.value) == "coarse source read limit exceeded"
    queries = []

    def capture(value, *args, **kwargs):
        queries.append(ee.serializer.encode(value, for_cloud_api=True))
        return collection([])

    monkeypatch.setattr(ee.data, "computeValue", capture)
    runner_coarse.read_snapshot(plan(), Budget(), 5)
    assert '"limit"' in json.dumps(queries[0]) and '{"constantValue": 6}' in json.dumps(queries[0])


def test_compact_snapshot_round_trips_through_json_and_detects_tampering(real_ee, monkeypatch):
    snap, _ = read(monkeypatch, [collection(C1), collection(C2), collection(CS)])
    p = plan()
    stored = json.loads(json.dumps(runner_coarse.encode_snapshot(snap)))
    again = runner_coarse.decode_snapshot(stored, p, "conflict")
    assert again.fingerprint == snap.fingerprint and again.extents == snap.extents
    tampering = [
        lambda s: s["members"]["SYNTH_N1_Z12"][1].__setitem__(2, 699970.0),
        lambda s: s["members"]["SYNTH_N1_Z12"].__setitem__(2, [10980, 10000]),
        lambda s: s["groups"][0].__setitem__(1, s["groups"][0][1] + 1),
        lambda s: s.__setitem__("fingerprint", "0" * 64),
        lambda s: s.__setitem__("scope", "1" * 64),
        lambda s: s["members"].pop("SYNTH_N3_Z16"),
        lambda s: s.__setitem__("extra", 1),
    ]
    for change in tampering:
        bad = copy.deepcopy(stored)
        change(bad)
        with pytest.raises(RunnerError) as err:
            runner_coarse.decode_snapshot(bad, p, "conflict")
        assert (err.value.status, str(err.value)) == ("conflict", "persisted coarse snapshot changed")
    other = plan(recipe(temporal=["mean"]))  # same scope: temporal statistics are not part of it
    assert runner_coarse.decode_snapshot(stored, other, "conflict").fingerprint == snap.fingerprint
    narrower = recipe()
    narrower["aoi"]["coordinates"] = narrower["aoi"]["coordinates"][:2]
    with pytest.raises(RunnerError):  # another AOI is another scope
        runner_coarse.decode_snapshot(stored, plan(narrower), "conflict")


def test_inline_snapshot_replaces_live_reads(real_ee, monkeypatch):
    snap, _ = read(monkeypatch, [collection(C1), collection(C2), collection(CS)])
    value = recipe(snapshot=runner_coarse.encode_snapshot(snap))
    validated = validate_recipe(json.loads(json.dumps(value)))
    assert runner_coarse.validate(validated, validated["limits"]).section["snapshot"]["fingerprint"] == snap.fingerprint
    bad = copy.deepcopy(value)
    bad["coarse"]["snapshot"]["members"]["SYNTH_N1_Z12"][2] = [1, 1]
    with pytest.raises(RunnerError) as err:
        validate_recipe(bad)
    assert err.value.status == "validation"


def test_coarse_scope_binds_dataset_bands_and_grid():
    s2 = plan()
    assert coarse.snapshot_scope(s2.spec, AOI, s2.grid) != coarse.snapshot_scope(plan(resolution=500).spec, AOI,
                                                                                coarse.grid(500))


# ------------------------------------------------------------------ effective scene ceiling (S03-F1)


def eight_members(monkeypatch):
    """The review's valid snapshot: eight members (datatakes 1, 2, 3 and 4; datatake 9
    began before the run), built by the real decoder from the synthetic national reads."""
    snap, _ = read(monkeypatch, [collection(C1), collection(C2), collection(CS)])
    assert len(snap.projections) == 8 and len(snap.groups) == 4
    return snap


def scene_limited(value, source, scenes):
    """``value`` under a recipe-imposed or a stricter policy-imposed scenes ceiling."""
    if source == "recipe":
        value["limits"] = {"scenes": scenes}
        return value, None
    policy = policy_value()
    policy["limits"] = {"scenes": scenes}
    value["destination"] = dict(bucket="output-bucket", prefix="products/coarse")
    return value, validate_policy(policy)


@pytest.mark.parametrize("source", ["recipe", "policy"])
@pytest.mark.parametrize("scenes", [1, 7, 8, 1000])
def test_inline_snapshot_must_fit_the_effective_scene_ceiling(real_ee, monkeypatch, source, scenes):
    """Members of the complete validated snapshot are counted (not groups, periods or
    bands); an oversized snapshot is refused whole, never truncated."""
    snap = eight_members(monkeypatch)
    inline = json.loads(json.dumps(recipe(snapshot=runner_coarse.encode_snapshot(snap))))
    value, policy = scene_limited(inline, source, scenes)
    if scenes < 8:
        with pytest.raises(RunnerError) as err:
            validate_recipe(value, policy)
        assert (err.value.status, str(err.value)) == ("validation", "coarse snapshot exceeds the scene ceiling")
    else:
        validated = validate_recipe(value, policy)
        assert validated["limits"]["scenes"] == scenes
        current = runner_coarse.validate(validated, validated["limits"])
        accepted = runner_coarse.decode_snapshot(validated["coarse"]["snapshot"], current, "validation")
        assert accepted.fingerprint == snap.fingerprint and accepted.projections == snap.projections


@pytest.mark.parametrize("scenes", [1, 7, 8])
def test_persisted_snapshot_must_fit_the_effective_scene_ceiling(real_ee, monkeypatch, scenes):
    snap = eight_members(monkeypatch)
    stored = json.loads(json.dumps(runner_coarse.encode_snapshot(snap)))
    value = recipe()
    value["limits"] = {"scenes": scenes}
    current = plan(value)
    if scenes < 8:
        with pytest.raises(RunnerError) as err:
            runner_coarse.decode_snapshot(stored, current, "conflict")
        assert (err.value.status, str(err.value)) == ("conflict", "coarse snapshot exceeds the scene ceiling")
    else:
        again = runner_coarse.decode_snapshot(stored, current, "conflict")
        assert again.fingerprint == snap.fingerprint and len(again.projections) == 8
