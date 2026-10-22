"""Named periods, complete-acquisition snapshots and one-call monthly products.

S01 fixtures F08/F11 and contract A5/A6/A14-A16. Snapshot rows are synthetic;
graph checks run offline (``real_ee``) and any evaluation fails the test.
"""

import dataclasses
from datetime import datetime, timezone

import pytest

from geecomposer import coarse, grouping
from geecomposer.exceptions import GeeComposerError
from geecomposer.rao import RaoGrid, RaoNormalization
from ee_graph import Graph

AOI = {"type": "Polygon", "coordinates": [[[-110.05, 27.3], [-109.95, 27.3], [-109.95, 27.4], [-110.05, 27.4], [-110.05, 27.3]]]}
UTM = RaoGrid("EPSG:32612", (1000, 0, 400000, 0, -1000, 3100000))
PROJ = {"crs": "EPSG:32612", "transform": [10, 0, 499980, 0, -10, 3100020]}
BANDS = ["B2", "B3", "B4", "B8"]
S2 = dict(dataset="sentinel2", aoi=AOI, grid=UTM, select=BANDS, mask="s2_cloud_score_plus")


def ms(text):
    return round(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def row(image_id, when, datatake, spacecraft="Sentinel-2A", **drop):
    value = {
        "id": image_id,
        "time_ms": ms(when),
        "SPACECRAFT_NAME": spacecraft,
        "DATATAKE_IDENTIFIER": f"GS2A_20250101T000000_{datatake:06d}_N05.11",
        "projections": {b: dict(PROJ) for b in BANDS + ["cs_cdf"]},
    }
    for key in drop:
        value.pop(key)
    return value


# F08 outer_boundaries catalogue (synthetic ids; DT numbers stand for datatakes A-E)
CAT = {
    "SYNTH_A1": row("SYNTH_A1", "2024-12-31T23:58:00Z", 1),
    "SYNTH_A2": row("SYNTH_A2", "2025-01-01T00:02:00Z", 1),
    "SYNTH_B1": row("SYNTH_B1", "2025-12-31T23:59:00Z", 2),
    "SYNTH_B2": row("SYNTH_B2", "2026-01-01T00:01:00Z", 2),
    "SYNTH_C1": row("SYNTH_C1", "2025-01-31T23:59:59.500Z", 3, "Sentinel-2B"),
    "SYNTH_C2": row("SYNTH_C2", "2025-02-01T00:00:00.400Z", 3, "Sentinel-2B"),
    "SYNTH_D1": row("SYNTH_D1", "2025-06-10T17:07:11Z", 4, "Sentinel-2B"),
}
CANDIDATES = ["SYNTH_A2", "SYNTH_B1", "SYNTH_C1", "SYNTH_C2", "SYNTH_D1"]
COMPLETION = ["SYNTH_A1", "SYNTH_A2", "SYNTH_B1", "SYNTH_B2", "SYNTH_C1", "SYNTH_C2", "SYNTH_D1"]


def snapshot(candidates=CANDIDATES, completion=COMPLETION, catalogue=CAT, **kw):
    return coarse.build_snapshot(start="2025-01-01", end="2026-01-01",
                                 candidate_rows=[catalogue[i] for i in candidates],
                                 completion_rows=[catalogue[i] for i in completion], **{**S2, **kw})


def test_monthly_2025_is_twelve_gap_free_half_open_utc_periods():
    months = grouping.monthly_periods(2025)
    assert [m[0] for m in months] == [f"2025-{i:02d}" for i in range(1, 13)]
    assert months[0][1:] == ("2025-01-01", "2025-02-01") and months[-1][1:] == ("2025-12-01", "2026-01-01")
    assert all(a[2] == b[1] for a, b in zip(months, months[1:]))
    days = [(datetime.fromisoformat(e) - datetime.fromisoformat(s)).days for _, s, e in months]
    assert days == [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31] and sum(days) == 365
    for year, feb in ((2024, 29), (2025, 28), (2000, 29), (2100, 28)):
        _, s, e = grouping.monthly_periods(year)[1]
        assert (datetime.fromisoformat(e) - datetime.fromisoformat(s)).days == feb
    assert grouping.validate_periods(months) == months


@pytest.mark.parametrize(
    "periods, code",
    [
        ([("a", "2025-01-01", "2025-02-01"), ("a", "2025-02-01", "2025-03-01")], "duplicate_label"),
        ([("a", "2025-01-01", "2025-02-15"), ("b", "2025-02-01", "2025-03-01")], "overlap"),
        ([("b", "2025-02-01", "2025-03-01"), ("a", "2025-01-01", "2025-02-01")], "unsorted"),
        ([("a", "2025-01-01", "2025-01-01")], "empty_interval"),
        ([("a", "2025-02-01", "2025-01-01")], "empty_interval"),
        ([("a", "2025-02-29", "2025-03-01")], "invalid_date"),
        ([("a", "2025-01-01T00:00:00+01:00", "2025-02-01")], "invalid_date"),
        ([("a b", "2025-01-01", "2025-02-01")], "bad_label"),
        ([("", "2025-01-01", "2025-02-01")], "bad_label"),
        (list(grouping.monthly_periods(2025)) + [("2026-01", "2026-01-01", "2026-02-01")], "too_many"),
        ([], "empty_list"),
    ],
)
def test_invalid_periods_are_rejected(periods, code):
    with pytest.raises(GeeComposerError, match=code):
        grouping.validate_periods(periods)


def test_whole_acquisitions_at_both_outer_boundaries():
    snap = snapshot()
    keys = {g[0][1][-6:]: g for g in snap.groups}  # datatake number -> group
    assert [k[1][-6:] for k in snap.excluded] == ["000001"]  # began 2024-12-31: excluded, not reassigned
    assert keys["000002"][2] == ("SYNTH_B1", "SYNTH_B2")  # December group keeps its 2026 member
    assert keys["000003"][2] == ("SYNTH_C1", "SYNTH_C2")
    assert [g[2] for g in snap.groups_in("2025-01-01", "2025-02-01")] == [("SYNTH_C1", "SYNTH_C2")]
    assert snap.groups_in("2025-02-01", "2025-03-01") == ()
    assert [g[2] for g in snap.groups_in("2025-12-01", "2026-01-01")] == [("SYNTH_B1", "SYNTH_B2")]
    assert [g[1] for g in snap.groups] == sorted(g[1] for g in snap.groups)
    with pytest.raises(coarse.CoarseError) as err:
        snap.groups_in("2025-12-01", "2026-02-01")
    assert err.value.code == "snapshot_interval"


@pytest.mark.parametrize(
    "case, code",
    [
        ("missing_time", "uncertain"),
        ("missing_key", "uncertain"),
        ("absent", "uncertain"),
        ("limit", "limit"),
        ("candidate_outside", "uncertain"),
        ("missing_projection", "missing_metadata"),
        ("mismatch", "source_projection_mismatch"),
        ("foreign_member", "uncertain"),
    ],
)
def test_malformed_or_oversized_snapshots_fail(case, code):
    catalogue = dict(CAT)
    candidates, completion, kw = CANDIDATES, COMPLETION, {}
    if case == "missing_time":
        catalogue["SYNTH_B2"] = row("SYNTH_B2", "2026-01-01T00:01:00Z", 2, time_ms=1)
    elif case == "missing_key":
        catalogue["SYNTH_C2"] = row("SYNTH_C2", "2025-02-01T00:00:00.400Z", 3, "Sentinel-2B", DATATAKE_IDENTIFIER=1)
    elif case == "absent":
        completion = [i for i in COMPLETION if i != "SYNTH_D1"]
    elif case == "limit":
        kw = dict(limit=6)
    elif case == "candidate_outside":
        candidates = CANDIDATES + ["SYNTH_A1"]
    elif case == "missing_projection":
        catalogue["SYNTH_D1"] = dict(CAT["SYNTH_D1"], projections={"B2": PROJ})
    elif case == "mismatch":
        bad = {b: dict(PROJ) for b in BANDS}
        bad["cs_cdf"] = {"crs": "EPSG:32612", "transform": [20, 0, 499980, 0, -20, 3100020]}
        catalogue["SYNTH_D1"] = dict(CAT["SYNTH_D1"], projections=bad)
    elif case == "foreign_member":
        catalogue["SYNTH_E1"] = row("SYNTH_E1", "2026-01-05T17:07:11Z", 5)
        completion = COMPLETION + ["SYNTH_E1"]
    with pytest.raises(coarse.CoarseError) as err:
        snapshot(candidates, completion, catalogue, **kw)
    assert err.value.code == code


def test_query_builders_separate_candidate_and_completion_roles(real_ee, runner_offline):
    candidate = Graph(coarse.candidate_query(start="2025-01-01", end="2026-01-01", **S2))
    keys = [k for k in {coarse._row_key("sentinel2", CAT[i]) for i in CANDIDATES}]
    completion = Graph(coarse.completion_query(keys=keys, **S2))
    assert list(candidate.calls("Filter.dateRangeContains")) and not list(completion.calls("Filter.dateRangeContains"))
    for graph in (candidate, completion):
        (limit,) = list(graph.calls("Collection.limit"))
        assert graph.constant(limit["limit"]) == coarse.READ_LIMIT + 1
        assert list(graph.calls("Geometry.coveringGrid"))
    # ee.Filter.inList serializes as Filter.listContains(leftValue=list, rightField=property)
    (in_list,) = [c for c in completion.calls("Filter.listContains") if "rightField" in c]
    assert completion.constant(in_list["rightField"]) == "geecomposer_acquisition"
    assert sorted(completion.constant(in_list["leftValue"])) == sorted(k[1] for k in keys)
    # the mapped acquisition-key body is a function definition; check its serialized text
    text = coarse.completion_query(keys=keys, **S2).serialize()
    assert "String.replace" in text and coarse._DATATAKE_SUFFIX.replace("\\", "\\\\") in text


def test_snapshot_must_match_the_query_scope(real_ee):
    snap = snapshot()
    other = dict(AOI, coordinates=[[[-110.0, 27.3], [-109.95, 27.3], [-109.95, 27.4], [-110.0, 27.4], [-110.0, 27.3]]])
    for kwargs, code in (
        (dict(snapshot=None), "missing_metadata"),
        (dict(snapshot=snap, aoi=other), "snapshot_scope"),
        (dict(snapshot=list(CAT.values())), "snapshot_scope"),
    ):
        with pytest.raises(coarse.CoarseError) as err:
            coarse.composite(start="2025-01-01", end="2025-02-01", **{**S2, **kwargs})
        assert err.value.code == code


def model():
    features = coarse.feature_names(BANDS, "median")
    return RaoNormalization(bands=features, means=[0.05, 0.07, 0.06, 0.3], scales=[0.02, 0.02, 0.03, 0.1],
                            loadings=[[1 if i == j else 0 for j in range(4)] for i in range(4)],
                            retained_fraction=1.0, n_sigmas=6.0, sample_count=10, decimal_places=None,
                            calibration="test-frozen")


def renamed(graph):
    names = []
    for call in graph.calls("Image.rename"):
        value = graph.constant(call["names"])
        names.extend(value if isinstance(value, list) else [value])
    return set(names)


def test_twelve_monthly_outputs_from_one_call_with_empty_months(real_ee, runner_offline):
    snap = snapshot()
    months = grouping.monthly_periods(2025)
    results = coarse.composite_periods(months, snapshot=snap, temporal=("median", "sd_pop"), **S2)
    assert list(results) == [m[0] for m in months]
    spec = coarse._spec("sentinel2", BANDS, "s2_cloud_score_plus", None, ("median", "sd_pop"), 0.5, None, False)
    for label, image in results.items():
        graph = Graph(image)
        stages = list(graph.calls("Image.reduceResolution"))
        assert set(coarse.output_names(spec)) <= renamed(graph)  # schema kept for every month
        if label in ("2025-01", "2025-12"):
            assert len(stages) == 2 * 2  # two members x two stages
        elif label == "2025-06":
            assert len(stages) == 2
        else:
            assert stages == []  # empty month: masked statistics, zero count, no task-like work


def test_monthly_rao_reuses_one_frozen_model_without_fitting(real_ee, runner_offline):
    frozen = model()
    before = frozen.fingerprint
    results = coarse.composite_periods(grouping.monthly_periods(2025), model=frozen, snapshot=snapshot(),
                                       temporal=("median",), **S2)
    assert len(results) == 12 and frozen.fingerprint == before
    for image in results.values():
        assert not list(Graph(image).calls("Image.reduceRegion"))  # no fit in period functions
        assert before in image.serialize()  # the same calibration fingerprint every month
    with pytest.raises(coarse.CoarseError):
        coarse.composite_periods(grouping.monthly_periods(2025), model=frozen, snapshot=snapshot(),
                                 temporal=("median", "mean"), **S2)
    with pytest.raises(coarse.CoarseError):
        coarse.composite_periods(grouping.monthly_periods(2025), model=frozen, snapshot=snapshot(),
                                 **{**S2, "select": ["B8", "B4", "B3", "B2"]})


def test_composite_records_source_identity_and_snapshot(real_ee):
    snap = snapshot()
    image = coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snap, **S2)
    text = image.serialize()
    for value in ("SYNTH_C1", "SYNTH_C2", snap.fingerprint, "coarse:stage_max_pixels"):
        assert value in text
    assert "SYNTH_D1" not in text and "SYNTH_A1" not in text


def test_building_every_product_requests_nothing_and_starts_no_task(real_ee, runner_offline, monkeypatch):
    import ee

    def deny(*args, **kwargs):
        pytest.fail("graph construction made an Earth Engine request or started a task")

    monkeypatch.setattr(ee.data, "_execute_cloud_call", deny)
    monkeypatch.setattr(ee.batch.Task, "start", deny)
    months = grouping.monthly_periods(2025)
    fine = RaoGrid("EPSG:32612", (10, 0, 400000, 0, -10, 3100000))
    keys = sorted({coarse._row_key("sentinel2", CAT[i]) for i in CANDIDATES})
    products = [
        *coarse.composite_periods(months, snapshot=snapshot(), temporal=coarse.TEMPORAL, index="ndvi", **S2).values(),
        *coarse.composite_periods(months, model=model(), snapshot=snapshot(), temporal=("median",), **S2).values(),
        coarse.fine_first_reference(start="2025-01-01", end="2025-02-01", snapshot=snapshot(), fine_grid=fine, **S2),
        coarse.candidate_query(start="2025-01-01", end="2026-01-01", **S2),
        coarse.completion_query(keys=keys, **S2),
    ]
    assert len(products) == 27 and all(p.serialize() for p in products)


# ------------------------------------------------------------------ round 2: F2-F4 (prompt 203)


def _fields(snap):
    return {f.name: getattr(snap, f.name) for f in dataclasses.fields(snap)}


def _corrupt(snap, case):
    groups, projections = list(snap.groups), list(snap.projections)
    table = dict(projections)
    first_id = groups[0][2][0]
    if case == "duplicate_group":  # the review's counterexample: one acquisition counted twice
        return dict(groups=snap.groups + (snap.groups[0],))
    if case == "member_in_two_groups":
        key, t, ids = groups[1]
        groups[1] = (key, t, tuple(sorted(ids + (first_id,))))
        return dict(groups=tuple(groups))
    if case == "duplicate_member":
        key, t, ids = groups[0]
        groups[0] = (key, t, ids + ids[:1])
        return dict(groups=tuple(groups))
    if case == "included_and_excluded":
        return dict(excluded=snap.excluded + (groups[0][0],))
    if case == "time_outside":
        key, _, ids = groups[0]
        groups[0] = (key, ms("2024-12-31T00:00:00Z"), ids)
        return dict(groups=tuple(groups))
    if case == "time_bool":
        key, _, ids = groups[0]
        groups[0] = (key, True, ids)
        return dict(groups=tuple(groups))
    if case == "missing_projection":
        return dict(projections=tuple(p for p in projections if p[0] != first_id))
    if case == "extra_projection":
        return dict(projections=snap.projections + (("SYNTH_ZZ", table[first_id]),))
    if case == "missing_band":
        table[first_id] = tuple(e for e in table[first_id] if e[0] != "cs_cdf")
        return dict(projections=tuple(sorted(table.items())))
    if case == "mixed_grid":
        band, crs, transform = table[first_id][0]
        shifted = (band, crs, tuple(transform[:2]) + (transform[2] + 10,) + tuple(transform[3:]))
        table[first_id] = (shifted,) + tuple(table[first_id][1:])
        return dict(projections=tuple(sorted(table.items())))
    if case == "nonfinite_transform":
        table[first_id] = tuple((b, c, (10.0, 0.0, float("nan"), 0.0, -10.0, 0.0)) for b, c, _ in table[first_id])
        return dict(projections=tuple(sorted(table.items())))
    if case == "bad_key":
        key, t, ids = groups[0]
        groups[0] = (("Sentinel-9X", key[1]), t, ids)
        return dict(groups=tuple(groups))
    if case == "bad_member_id":
        key, t, ids = groups[0]
        groups[0] = (key, t, ("not an id",) + ids[1:])
        return dict(groups=tuple(groups))
    if case == "bad_dataset":
        return dict(dataset="landsat")
    if case == "groups_not_a_sequence":
        return dict(groups=None)
    if case == "key_not_a_pair":
        key, t, ids = groups[0]
        groups[0] = (key[0], t, ids)
        return dict(groups=tuple(groups))
    if case == "too_many_keys":
        extra = tuple(("Sentinel-2A", f"GS2A_20241201T000000_{i:06d}") for i in range(100, 701))
        return dict(excluded=snap.excluded + extra)
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case, code",
    [
        ("duplicate_group", "snapshot_invalid"),
        ("member_in_two_groups", "snapshot_invalid"),
        ("duplicate_member", "snapshot_invalid"),
        ("included_and_excluded", "snapshot_invalid"),
        ("time_outside", "snapshot_invalid"),
        ("time_bool", "snapshot_invalid"),
        ("missing_projection", "snapshot_invalid"),
        ("extra_projection", "snapshot_invalid"),
        ("missing_band", "snapshot_invalid"),
        ("mixed_grid", "snapshot_invalid"),
        ("nonfinite_transform", "snapshot_invalid"),
        ("bad_key", "snapshot_invalid"),
        ("bad_member_id", "snapshot_invalid"),
        ("bad_dataset", "snapshot_invalid"),
        ("groups_not_a_sequence", "snapshot_invalid"),
        ("key_not_a_pair", "snapshot_invalid"),
        ("too_many_keys", "limit"),
    ],
)
def test_f2_every_snapshot_construction_path_is_validated(case, code):
    snap = snapshot()
    change = _corrupt(snap, case)
    with pytest.raises(coarse.CoarseError) as err:  # dataclasses.replace
        dataclasses.replace(snap, **change)
    assert err.value.code == code
    with pytest.raises(coarse.CoarseError) as err:  # direct public construction
        coarse.Snapshot(**{**_fields(snap), **change})
    assert err.value.code == code


def test_f2_valid_direct_and_empty_snapshots_still_work(real_ee):
    snap = snapshot()
    assert coarse.Snapshot(**_fields(snap)).fingerprint == snap.fingerprint
    assert dataclasses.replace(snap).fingerprint == snap.fingerprint
    empty = snapshot(candidates=[], completion=[])
    assert empty.groups == () and empty.excluded == () and empty.projections == ()
    image = coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=empty, **S2)
    assert not list(Graph(image).calls("Image.reduceResolution"))


def test_f2_unordered_members_are_canonicalized_and_keep_the_lowest_id_tie():
    snap = snapshot()
    reordered = tuple((k, t, tuple(reversed(ids))) for k, t, ids in snap.groups)
    shuffled = dataclasses.replace(snap, groups=tuple(reversed(reordered)), projections=tuple(reversed(snap.projections)))
    assert shuffled.groups == snap.groups and shuffled.fingerprint == snap.fingerprint
    assert all(list(ids) == sorted(ids) for _, _, ids in shuffled.groups)


def test_f2_caller_containers_cannot_change_an_accepted_snapshot():
    snap = snapshot()
    groups = [[list(k), t, list(ids)] for k, t, ids in snap.groups]
    excluded = [list(k) for k in snap.excluded]
    projections = [[i, [[b, c, list(t)] for b, c, t in table]] for i, table in snap.projections]
    held = coarse.Snapshot(snap.dataset, snap.start, snap.end, snap.scope, groups, excluded, projections)
    before = held.fingerprint
    assert held.fingerprint == snap.fingerprint
    groups[0][2].append("SYNTH_ZZ")
    groups.append(groups[0])
    excluded.clear()
    projections[0][1][0][2][2] = 0.0
    assert held.fingerprint == before and held.groups == snap.groups and held.projections == snap.projections
    assert isinstance(held.groups[0][2], tuple) and isinstance(held.projections[0][1][0][2], tuple)


def test_f2_a_duplicated_acquisition_never_reaches_the_temporal_graph_twice(real_ee):
    snap = snapshot()
    image = coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=snap, **S2)
    graph = Graph(image)
    lists = [graph.resolve(c["images"])["arrayValue"]["values"] for c in graph.calls("ImageCollection.fromImages")]
    assert lists and all(len(images) == 1 for images in lists)  # one acquisition in January
    with pytest.raises(coarse.CoarseError):
        dataclasses.replace(snap, groups=snap.groups + (snap.groups[0],))


def test_f2_consumers_bind_the_requested_contributing_bands(real_ee):
    snap = snapshot()
    narrowed = tuple((i, tuple(e for e in table if e[0] != "B8")) for i, table in snap.projections)
    forged = dataclasses.replace(snap, projections=narrowed)  # structurally valid, wrong band set
    with pytest.raises(coarse.CoarseError) as err:
        coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=forged, **S2)
    assert err.value.code == "snapshot_scope"


def _shifted(row_value, **changes):
    value = dict(row_value)
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "case",
    ["jan31_feb1", "changed_key", "changed_projection", "duplicate_identical", "duplicate_conflicting"],
)
def test_f3_candidate_and_completion_rows_must_agree(case):
    candidates, completion = [CAT[i] for i in CANDIDATES], [CAT[i] for i in COMPLETION]
    if case == "jan31_feb1":  # same image: candidate on January 31, completion on February 1
        completion = [_shifted(r, time_ms=ms("2025-02-01T00:00:00.100Z")) if r["id"] == "SYNTH_C1" else r
                      for r in completion]
    elif case == "changed_key":  # completion moves SYNTH_D1 into another candidate acquisition
        completion = [_shifted(r, DATATAKE_IDENTIFIER=CAT["SYNTH_C1"]["DATATAKE_IDENTIFIER"],
                               SPACECRAFT_NAME="Sentinel-2B") if r["id"] == "SYNTH_D1" else r for r in completion]
    elif case == "changed_projection":
        moved = {b: {"crs": "EPSG:32612", "transform": [10, 0, 499990, 0, -10, 3100020]} for b in BANDS + ["cs_cdf"]}
        completion = [_shifted(r, projections=moved) if r["id"] == "SYNTH_D1" else r for r in completion]
    elif case == "duplicate_identical":
        candidates = candidates + [CAT["SYNTH_D1"]]
    elif case == "duplicate_conflicting":
        candidates = candidates + [_shifted(CAT["SYNTH_D1"], time_ms=ms("2025-06-10T17:07:12Z"))]
    with pytest.raises(coarse.CoarseError) as err:
        coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=candidates,
                              completion_rows=completion, **S2)
    assert err.value.code == "uncertain"


def test_f3_valid_reads_are_deterministic_and_accept_equivalent_representations():
    reference = snapshot()
    rows_c, rows_k = [CAT[i] for i in CANDIDATES], [CAT[i] for i in COMPLETION]
    for order in (lambda r: list(reversed(r)), lambda r: r[2:] + r[:2], lambda r: sorted(r, key=lambda x: x["time_ms"])):
        snap = coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=order(rows_c),
                                     completion_rows=order(list(reversed(rows_k))), **S2)
        assert snap.fingerprint == reference.fingerprint and snap.groups == reference.groups
    floats = {b: {"crs": "EPSG:32612", "transform": [10.0, 0.0, 499980.0, 0.0, -10.0, 3100020.0]} for b in BANDS + ["cs_cdf"]}
    floats["B5"] = {"crs": "EPSG:32612", "transform": [20, 0, 499980, 0, -20, 3100020]}  # unrelated band
    equivalent = [_shifted(r, projections=floats, CLOUDY_PIXEL_PERCENTAGE=3.5) for r in rows_k]
    snap = coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=rows_c,
                                 completion_rows=equivalent, **S2)
    assert snap.fingerprint == reference.fingerprint


BAD_LIMITS = [0, -1, 1001, True, 10.0, float("nan"), float("inf"), "10", None]


class Untouchable:
    """An input the builder must not consume: any iteration fails the test."""

    def __iter__(self):
        pytest.fail("input consumed before the limit was validated")


@pytest.mark.parametrize("limit", BAD_LIMITS)
def test_f4_limits_must_be_positive_integers_up_to_the_fixed_ceiling(limit, monkeypatch):
    import ee

    monkeypatch.setattr(ee, "ImageCollection", lambda *a, **k: pytest.fail("query built before limit validation"))
    keys = [coarse._row_key("sentinel2", CAT["SYNTH_D1"])]
    for build in (
        lambda: coarse.candidate_query(start="2025-01-01", end="2026-01-01", limit=limit, **S2),
        lambda: coarse.completion_query(keys=keys, limit=limit, **S2),
        lambda: coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=Untouchable(),
                                      completion_rows=Untouchable(), limit=limit, **S2),
    ):
        with pytest.raises(coarse.CoarseError) as err:
            build()
        assert err.value.code == "limit"


def test_f4_stricter_limits_keep_the_overflow_sentinel(real_ee):
    keys = sorted({coarse._row_key("sentinel2", CAT[i]) for i in CANDIDATES})
    for query in (coarse.candidate_query(start="2025-01-01", end="2026-01-01", limit=10, **S2),
                  coarse.completion_query(keys=keys, limit=10, **S2)):
        graph = Graph(query)
        (limit,) = list(graph.calls("Collection.limit"))
        assert graph.constant(limit["limit"]) == 11
    snapshot(limit=7)  # seven completion rows fit a stricter limit of seven
    with pytest.raises(coarse.CoarseError) as err:
        snapshot(limit=6)
    assert err.value.code == "limit"


def _many(count, *, per_key=1, early=()):
    """Synthetic C1/C2 rows: ``count`` acquisitions in 2025 with ``per_key`` members each;
    keys listed in ``early`` also have one completion member on 2024-12-31 (pre-run)."""
    candidates, completion = [], []
    for k in range(count):
        for m in range(per_key):
            value = row(f"SYNTH_K{k:04d}_{m}", f"2025-03-{1 + k % 28:02d}T10:{m:02d}:00Z", k)
            candidates.append(value)
            completion.append(value)
        if k in early:
            completion.append(row(f"SYNTH_K{k:04d}_E", "2024-12-31T23:00:00Z", k))
    return candidates, completion


def _build(candidates, completion, **kw):
    return coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=candidates,
                                 completion_rows=completion, **{**S2, **kw})


def test_f4_the_1000_row_ceiling_is_exact():
    candidates, completion = _many(500, per_key=2)
    assert sum(len(g[2]) for g in _build(candidates, completion).groups) == 1000
    extra = row("SYNTH_K0000_9", "2025-03-01T10:09:00Z", 0)
    with pytest.raises(coarse.CoarseError) as err:
        _build(candidates + [extra], completion + [extra])
    assert err.value.code == "limit"


def test_f4_row_iterables_are_consumed_only_up_to_the_sentinel():
    candidates, _ = _many(500, per_key=2)
    extra = row("SYNTH_K0000_9", "2025-03-01T10:09:00Z", 0)

    def rows():
        yield from candidates + [extra]
        pytest.fail("rows consumed beyond limit + 1")

    with pytest.raises(coarse.CoarseError) as err:
        _build(rows(), [])
    assert err.value.code == "limit"


def test_f4_600_candidate_keys_are_counted_before_pre_run_exclusions():
    snap = _build(*_many(600, early=(0, 1)))
    assert len(snap.groups) == 598 and len(snap.excluded) == 2
    with pytest.raises(coarse.CoarseError) as err:  # 599 included + 2 excluded = 601 candidate keys
        _build(*_many(601, early=(0, 1)))
    assert err.value.code == "limit"


def test_f4_completion_keys_are_bounded_without_materializing_the_input():
    keys = [("Sentinel-2A", f"GS2A_20250101T000000_{i:06d}") for i in range(601)]

    def stream():
        yield from keys
        pytest.fail("keys consumed beyond the 601st")

    with pytest.raises(coarse.CoarseError) as err:
        coarse.completion_query(keys=stream(), **S2)
    assert err.value.code == "limit"


def test_f4_400_groups_per_period(real_ee):
    def january(count):
        rows = [row(f"SYNTH_J{k:04d}", f"2025-01-{1 + k % 28:02d}T10:00:00Z", k) for k in range(count)]
        return _build(rows, rows)

    spec = coarse._spec("sentinel2", BANDS, "s2_cloud_score_plus", None, ("median",), 0.5, None, False)
    assert len(coarse._bind(january(400), spec, AOI, UTM, "2025-01-01", "2025-02-01")) == 400
    with pytest.raises(coarse.CoarseError) as err:
        coarse.composite(start="2025-01-01", end="2025-02-01", snapshot=january(401), **S2)
    assert err.value.code == "limit"
