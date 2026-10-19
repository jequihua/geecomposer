"""S03: the coarse workflow through the real runner, real preparation and native configs.

Only the service boundaries are fakes: the run store, task allocation/submission/
status (runner_fakes) and EE evaluation (a counted ``computeValue`` queue). The
production validator, runner state machine, ``runner_science.prepare``, metadata
decoder, ``coarse`` science and unstarted native export constructors run unchanged.
"""

import copy
import json

import ee
import pytest

from geecomposer import coarse, runner_coarse
from geecomposer import runner_personal as personal
from geecomposer.recipe import CEILINGS, RunnerError, digest, validate_policy, validate_recipe
from geecomposer.runner import LEASE_SECONDS, Runner, binding, observe_telemetry, public_status, telemetry
from geecomposer.rao import RaoNormalization
from geecomposer.runner_science import (config_fingerprint, discovery_marker, export_encoded, native_task, prepare,
                                         window_rectangle)
from ee_graph import Graph
from runner_fakes import Boundary, Clock, Crash, Engine, Store, policy_value, recipe_value
from test_coarse_metadata import BOUNDS, C1, C2, CS, Answers, collection, read, recipe
from test_coarse_recipe import SUPPLIED_MODEL

pytestmark = pytest.mark.usefixtures("runner_offline")
READS = [BOUNDS, collection(C1), collection(C2), collection(CS)]
REGION = {"type": "Polygon", "coordinates": [[[-110.2, 27.2], [-109.6, 27.2], [-109.6, 27.6], [-110.2, 27.6],
                                              [-110.2, 27.2]]]}
REGION_BOUNDS = [[[1840, -1620], [1900, -1620], [1900, -1570], [1840, -1570], [1840, -1620]]]
FIT = [9, {"B2__s_mean__t_median_count": 9}, {}]


class ScienceEngine(Engine):
    """Fake allocation/submission/status around the real preparation."""

    def __init__(self, boundary, store, clock):
        super().__init__(boundary, store)
        self.clock = clock
        self.telemetry = {}
        self.prepared = 0
        self.budget = None  # the runner's shared budget, as EarthEngine.prepare uses it

    def prepare(self, recipe, prefix, previous=None):
        self.boundary.event("prepare.before")
        self.prepared += 1
        result = prepare(recipe, prefix, self.budget or self.clock.budget(), previous)
        self.boundary.event("prepare.after")
        return result

    def submit(self, request_id, config):
        self.boundary.event("submit.before")
        state = next(s for s, _ in self.store.records.values()
                     if any(t["request_id"] == request_id for t in s["tasks"]))
        task = next(t for t in state["tasks"] if t["request_id"] == request_id)
        assert task["state"] == "submitting" and task["fingerprint"] == config_fingerprint(config)
        assert task["marker"] == config["description"] and request_id not in self.submitted
        assert sum(t["state"] in ("READY", "RUNNING", "submitting") for t in state["tasks"]) <= 2
        self.submitted.append(request_id)
        self.configs = getattr(self, "configs", {})
        self.configs[task["name"]] = config
        self.operations[request_id] = f"projects/test-project/operations/server-{len(self.submitted)}"
        self.markers[config["description"]] = self.operations[request_id]
        self.boundary.event("submit.after")
        return {"name": self.operations[request_id]}

    def status(self, request_id, operation=None):
        result = super().status(request_id, operation)
        sequence = self.telemetry.get(request_id)
        if sequence:
            result.update(sequence.pop(0) if len(sequence) > 1 else sequence[0])
        return result


class World:
    def __init__(self, value, mode="gcs"):
        self.clock, self.boundary = Clock(), Boundary()
        self.store = Store(self.boundary)
        self.engine = ScienceEngine(self.boundary, self.store, self.clock)
        if mode == "gcs":
            self.policy = validate_policy(policy_value())
            value["destination"] = dict(bucket="output-bucket", prefix="products/coarse")
            self.recipe = validate_recipe(value, self.policy)
        else:
            self.policy = personal.settings("test-project")
            self.recipe = personal.validate(value)
        self.mode = mode

    def runner(self):
        cls = Runner if self.mode == "gcs" else personal.PersonalRunner
        self.engine.budget = self.clock.budget()
        return cls(self.policy, self.store, self.engine, object(), self.engine.budget)


def run(monkeypatch, value, reads=READS, mode="gcs"):
    world = World(value, mode)
    answers = Answers(monkeypatch, reads)
    result = world.runner().run(world.recipe, "coarse-run")
    return world, answers, result


def record(world):
    return world.store.records["coarse-run"][0]


def test_monthly_institutional_run_end_to_end(real_ee, monkeypatch):
    world, answers, result = run(monkeypatch, recipe())
    assert result["status"] == "succeeded" and answers.calls == 4 and answers.pending == []
    state = record(world)
    assert [t["name"] for t in state["tasks"]] == ["p2025_06", "p2025_07", "p2025_12"]
    outcomes = {p["label"]: p["outcome"] for p in state["provenance"]["coarse"]["periods"]}
    assert sum(o == "empty" for o in outcomes.values()) == 9 and outcomes["2025-06"] == "populated"
    assert len(world.engine.submitted) == 3 and world.engine.prepared == 1
    snapshot = state["provenance"]["coarse"]["snapshot"]
    assert set(snapshot["members"]) >= {"SYNTH_N1_Z11", "SYNTH_N2_Z16", "SYNTH_N4_Z13"}
    assert snapshot["members"]["SYNTH_N1_Z12"][2] == [10980, 10980]
    assert result["outputs"] and all(o.startswith("gs://output-bucket/products/coarse/") for o in result["outputs"])


def test_personal_drive_run_and_unstarted_config(real_ee, monkeypatch):
    world, _, result = run(monkeypatch, recipe(), mode="personal")
    assert result["status"] == "succeeded" and result["mode"] == "personal"
    config = world.engine.configs["p2025_06"]
    options = config["fileExportOptions"]
    assert options["driveDestination"]["folder"] == "geecomposer_coarse"
    assert options["geoTiffOptions"]["noData"] == {"floatValue": -9999}
    assert config["maxPixels"] == {"value": str(CEILINGS["pixels"])}
    # Native constructor: reprojection to the exact MX-AEA grid, then the core rectangle.
    graph = Graph(config["expression"])
    root = graph.root["functionInvocationValue"]
    assert root["functionName"] == "Image.clip"
    rectangle = graph.resolve(root["arguments"]["geometry"])["functionInvocationValue"]
    window = record(world)["provenance"]["export_window"]
    assert graph.constant(rectangle["arguments"]["coordinates"]) == [
        window["col_off"], window["row_off"], window["col_off"] + window["width"], window["row_off"] + window["height"]]
    reproject = graph.resolve(root["arguments"]["input"])["functionInvocationValue"]
    assert reproject["functionName"] == "Image.reproject"
    assert 'PROJCS[\\"MX-AEA\\"' in config["expression"].serialize()


def test_python_api_and_runner_graphs_agree(real_ee, monkeypatch):
    world, _, _ = run(monkeypatch, recipe(index="ndvi"))
    state = record(world)
    plan = runner_coarse.validate(state["recipe"], state["recipe"]["limits"])
    snap = runner_coarse.decode_snapshot(state["provenance"]["coarse"]["snapshot"], plan, "conflict")
    direct = coarse.composite(start="2025-06-01", end="2025-07-01", snapshot=snap, dataset="sentinel2", aoi=recipe()["aoi"],
                              select=["B2", "B3", "B4", "B8"], grid=coarse.grid(1000), mask="s2_cloud_score_plus",
                              temporal=("median",), index="ndvi")
    produced = world.engine.configs["p2025_06"]["expression"]
    window = state["provenance"]["export_window"]
    expected = native_task("p2025_06", export_encoded(direct), window_rectangle(coarse.grid(1000), window),
                           coarse.grid(1000), CEILINGS["pixels"], state["prefix"], state["recipe"]["destination"])
    assert ee.serializer.encode(produced, for_cloud_api=True) == ee.serializer.encode(
        expected["config"]["expression"], for_cloud_api=True)
    text = produced.serialize()
    for image_id in ("SYNTH_N1_Z11", "SYNTH_N1_Z12", "SYNTH_N1_Z13", "SYNTH_N2_Z12", "SYNTH_N2_Z16"):
        assert f"COPERNICUS/S2_SR_HARMONIZED/{image_id}" in text  # every zone/seam/island member participates


def test_resume_replays_the_persisted_snapshot_without_rediscovery(real_ee, monkeypatch):
    world = World(recipe())
    Answers(monkeypatch, READS)
    world.boundary.hook = lambda name: (_ for _ in ()).throw(Crash(name)) if name == "submit.after" else None
    with pytest.raises(Crash):
        world.runner().run(world.recipe, "coarse-run")
    world.boundary.hook = None
    persisted = copy.deepcopy(record(world)["provenance"])
    world.clock.now += 200  # lease expired
    replay = Answers(monkeypatch, [])
    result = world.runner().resume("coarse-run")
    assert result["status"] == "succeeded" and replay.calls == 0
    assert record(world)["provenance"] == persisted and len(world.engine.submitted) == 3


@pytest.mark.parametrize("tamper", ["member_grid", "dimensions", "fingerprint", "window", "model"])
def test_changed_persisted_data_refuses_before_submission(real_ee, monkeypatch, tamper):
    if tamper == "model":
        monkeypatch.setattr(runner_coarse, "fit_normalization", fitted([]))
        value, reads = regional_rao(), REGIONAL + [9]
    else:
        value, reads = recipe(), READS
    world = World(value)
    Answers(monkeypatch, reads)
    world.boundary.hook = crash_after_persist(world)
    with pytest.raises(Crash):
        world.runner().run(world.recipe, "coarse-run")
    world.boundary.hook = None
    state, generation = world.store.records["coarse-run"]
    coarse_state = state["provenance"]["coarse"]
    if tamper == "member_grid":
        coarse_state["snapshot"]["members"]["SYNTH_N1_Z12"][1][2] += 10
    elif tamper == "dimensions":
        coarse_state["snapshot"]["members"]["SYNTH_N1_Z12"][2] = [10980, 10000]
    elif tamper == "fingerprint":
        coarse_state["snapshot"]["fingerprint"] = "0" * 64
    elif tamper == "window":
        state["provenance"]["export_window"]["width"] += 1
    else:
        coarse_state["model"] = coarse_state["model"].replace("6.0", "5.0")
    world.store.records["coarse-run"] = (state, generation)
    world.clock.now += 200
    submitted = len(world.engine.submitted)
    Answers(monkeypatch, [])
    result = world.runner().resume("coarse-run")
    assert result["status"] == "conflict" and len(world.engine.submitted) == submitted
    assert result["error"]["reason"] in ("conflict: persisted coarse snapshot changed",
                                         "conflict: persisted coarse model changed",
                                         "conflict: scientific provenance changed on resume",
                                         "conflict: prepared configuration changed on resume")


def crash_after_persist(world):
    """Crash at the first allocation, after the plan and provenance were persisted."""
    def hook(name):
        if name == "new_id.before":
            raise Crash(name)
    return hook


def regional_rao(**extra):
    value = recipe(product="rao", periods=None, rao={"fit": {"start": "2025-01-01", "end": "2026-01-01"}}, **extra)
    value["aoi"] = REGION
    return value


REGIONAL = [REGION_BOUNDS, collection(C1[:2]), collection(C1[:2]),
            collection([c for c in CS if c["properties"]["system:index"] in ("SYNTH_N1_Z11", "SYNTH_N1_Z12")])]


def test_reference_fit_runs_once_and_is_replayed(real_ee, monkeypatch):
    calls = []
    monkeypatch.setattr(runner_coarse, "fit_normalization", fitted(calls))
    world, answers, result = run(monkeypatch, regional_rao(), reads=REGIONAL + [9])
    assert result["status"] == "succeeded" and len(calls) == 1 and answers.calls == 5
    state = record(world)
    model = state["provenance"]["coarse"]["model"]
    assert state["provenance"]["coarse"]["calibration_sha256"] == digest_of(model)
    assert digest_of(model) in world.engine.configs["pall"]["expression"].serialize()
    state["status"], state["owner"] = "running", None  # pretend a later resume of the same run
    world.store.records["coarse-run"] = (state, world.store.records["coarse-run"][1])
    replay = Answers(monkeypatch, [])
    world.runner().resume("coarse-run")
    assert replay.calls == 0 and len(calls) == 1


def digest_of(model_json):
    from hashlib import sha256

    return sha256(model_json.encode()).hexdigest()


def fitted(calls):
    from geecomposer.rao import RaoNormalization

    def fit(image, bands, **kwargs):
        calls.append(kwargs)
        assert kwargs["max_pixels"] == CEILINGS["calibration_pixels"]
        return RaoNormalization(bands=list(bands), means=[0.05, 0.07, 0.06, 0.3], scales=[0.02, 0.02, 0.03, 0.1],
                                loadings=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], retained_fraction=1.0,
                                n_sigmas=6.0, sample_count=9, decimal_places=None, calibration="synthetic-fit")
    return fit


def test_interrupted_calibration_is_never_refit_automatically(real_ee, monkeypatch):
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        raise Crash("calibration interrupted")

    monkeypatch.setattr(runner_coarse, "fit_normalization", interrupted)
    world = World(regional_rao())
    Answers(monkeypatch, REGIONAL + [9])
    with pytest.raises(Crash):
        world.runner().run(world.recipe, "coarse-run")
    assert record(world)["calibration"] == "started" and "provenance" not in record(world)
    world.clock.now += 200
    prepared = world.engine.prepared
    result = world.runner().resume("coarse-run")
    assert result["status"] == "uncertain" and world.engine.prepared == prepared and len(calls) == 1
    assert result["error"]["reason"] == "uncertain: coarse calibration was interrupted; no durable model to replay"


def test_supplied_model_needs_no_fit_and_reaches_every_month(real_ee, monkeypatch):
    model = dict(bands=["B8__s_mean__t_median", "B3__s_mean__t_median", "B2__s_mean__t_median", "B4__s_mean__t_median"],
                 means=[0.3, 0.07, 0.05, 0.06], scales=[0.1, 0.02, 0.02, 0.03],
                 loadings=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], retained_fraction=1.0,
                 n_sigmas=6.0, sample_count=10, decimal_places=None, calibration="synthetic-test-model")
    value = recipe(product="rao", rao={"model": model})
    value["bands"] = ["B8", "B3", "B2", "B4"]
    monkeypatch.setattr(runner_coarse, "fit_normalization", lambda *a, **k: pytest.fail("refit"))
    world, answers, result = run(monkeypatch, value)
    assert result["status"] == "succeeded" and answers.calls == 4
    expected = RaoNormalization(**model).fingerprint
    for name in ("p2025_06", "p2025_07", "p2025_12"):
        text = world.engine.configs[name]["expression"].serialize()
        assert expected in text and "coarse_rao_gn" in text


def test_all_empty_run_is_no_data_without_tasks(real_ee, monkeypatch):
    world, answers, result = run(monkeypatch, recipe(), reads=[BOUNDS, collection([])])
    assert result["status"] == "no_data" and answers.calls == 2 and world.engine.submitted == []
    assert result["error"]["reason"] == "no_data: no acquisitions in any declared period"


def test_legacy_ten_output_records_keep_their_policy(monkeypatch):
    """A record written under the former ten-output default is read, resumed and
    reported under that policy; it is never rebound to twelve."""
    policy = validate_policy(policy_value())
    legacy = dict(policy, limits=dict(policy["limits"], outputs=10))
    world = World.__new__(World)
    world.clock, world.boundary = Clock(), Boundary()
    world.store = Store(world.boundary)
    world.engine = Engine(world.boundary, world.store)
    value = recipe_value()
    value["limits"] = {"outputs": 10}
    old = validate_recipe(value, legacy)
    Runner(legacy, world.store, world.engine, object(), world.clock.budget()).run(old, "legacy-run")
    state = world.store.records["legacy-run"][0]
    assert state["policy_sha256"] == digest(legacy) and state["recipe"]["limits"]["outputs"] == 10
    current = Runner(policy, world.store, world.engine, object(), world.clock.budget())
    assert current.status("legacy-run")["status"] == "succeeded"
    assert current.policy["limits"]["outputs"] == 10 and state["binding"] == binding(old, legacy, state["source"])
    other = dict(policy, limits=dict(policy["limits"], outputs=9))
    with pytest.raises(RunnerError):  # an unrelated policy is still a mismatch
        Runner(other, world.store, world.engine, object(), world.clock.budget()).status("legacy-run")
    settings = personal.settings("test-project")
    old_settings = dict(settings, limits=dict(settings["limits"], outputs=10))
    runner = personal.PersonalRunner(old_settings, world.store, world.engine, object(), world.clock.budget())
    value = recipe_value()
    value["destination"] = {"drive_folder": "geecomposer"}
    runner.run(personal.validate(value), "legacy-personal")
    reader = personal.PersonalRunner(settings, world.store, world.engine, object(), world.clock.budget())
    assert reader.status("legacy-personal")["status"] == "succeeded" and reader.policy["limits"]["outputs"] == 10


def test_latest_attempt_telemetry_is_kept_never_summed_or_invented(real_ee, monkeypatch):
    world = World(recipe())
    world.engine.telemetry = {
        "request-1": [{"batch_eecu_usage_seconds": 5.5, "attempt": 1}, {"batch_eecu_usage_seconds": 2.0, "attempt": 2}],
        "request-2": [{"batch_eecu_usage_seconds": float("nan"), "attempt": 1}],
        "request-3": [{"batch_eecu_usage_seconds": True, "attempt": 1}],
    }
    world.engine.states = {"request-1": ["RUNNING", "COMPLETED"]}
    Answers(monkeypatch, READS)
    result = world.runner().run(world.recipe, "coarse-run")
    tasks = {t["request_id"]: t for t in record(world)["tasks"]}
    assert tasks["request-1"]["telemetry"] == {"batch_eecu_usage_seconds": 2.0, "attempt": 2}
    # A valid attempt is observed even when its EECU is malformed: usage unknown, never zero.
    unknown = {"batch_eecu_usage_seconds": None, "attempt": 1}
    assert tasks["request-2"]["telemetry"] == tasks["request-3"]["telemetry"] == unknown
    shown = {t["name"]: t for t in result["tasks"]}
    assert shown["p2025_06"]["telemetry"]["batch_eecu_usage_seconds"] == 2.0 and shown["p2025_07"]["telemetry"] == unknown
    task = {"telemetry": {"batch_eecu_usage_seconds": 7.0, "attempt": 3}}
    observe_telemetry(task, {"batch_eecu_usage_seconds": 9.0, "attempt": 2})  # an older attempt never wins
    observe_telemetry(task, {"state": "RUNNING"})  # missing stays as it was, never zero
    assert task["telemetry"] == {"batch_eecu_usage_seconds": 7.0, "attempt": 3}
    assert telemetry({"batch_eecu_usage_seconds": -1, "attempt": 1}) is None
    forged = copy.deepcopy(record(world))
    forged["tasks"][1]["telemetry"] = {"batch_eecu_usage_seconds": "lots", "attempt": 1}
    assert "telemetry" not in public_status(forged, world.policy)["tasks"][1]


def test_declared_tasks_beyond_the_ceiling_fail_before_any_service_call(monkeypatch):
    value = recipe(tiles={"columns": 2})
    value["destination"] = dict(bucket="output-bucket", prefix="products/coarse")
    world = World(copy.deepcopy(recipe()))
    with pytest.raises(RunnerError) as err:
        world.runner().run(value, "too-many")
    assert str(err.value) == "declared periods times tile cores exceed the output ceiling"
    assert world.store.records == {} and world.boundary.trace == []


def test_sentinel1_national_monthly_run(real_ee, monkeypatch):
    from test_coarse_metadata import s1_feature

    s1 = [s1_feature("SYNTH_S1_Z12", "2025-06-12T00:10:00Z", 4242, "Z12"),
          s1_feature("SYNTH_S1_Z16", "2025-06-12T00:10:20Z", 4242, "Z16")]
    world, answers, result = run(monkeypatch, recipe("sentinel1_float", db=True), reads=[BOUNDS, collection(s1),
                                                                                         collection(s1)])
    assert result["status"] == "succeeded" and answers.calls == 3
    text = world.engine.configs["p2025_06"]["expression"].serialize()
    assert "COPERNICUS/S1_GRD_FLOAT/SYNTH_S1_Z12" in text and "COPERNICUS/S1_GRD_FLOAT/SYNTH_S1_Z16" in text
    assert "0.0001" not in text and "s1_linear_power" in text


def test_deadline_during_metadata_reads_stops_without_tasks(real_ee, monkeypatch):
    value = recipe()
    value["limits"] = {"deadline_seconds": 120}
    world = World(value)
    answers = Answers(monkeypatch, READS)
    original = ee.data.computeValue

    def slow(*args, **kwargs):
        world.clock.now += 70  # past the 60 s work budget, inside the 120 s lease
        return original(*args, **kwargs)

    monkeypatch.setattr(ee.data, "computeValue", slow)
    result = world.runner().run(world.recipe, "coarse-run")
    assert result["status"] == "deadline" and world.engine.submitted == [] and answers.calls == 1


def test_lost_submission_response_is_recovered_by_marker_not_resubmitted(real_ee, monkeypatch):
    world = World(recipe())
    world.engine.loss = True
    original = world.engine.submit

    def submit(request_id, config):
        response = original(request_id, config)
        if world.engine.loss:
            world.engine.loss = False
            raise TimeoutError("SECRET lost response")
        return response

    world.engine.submit = submit
    Answers(monkeypatch, READS)
    result = world.runner().run(world.recipe, "coarse-run")
    assert result["status"] == "succeeded" and len(world.engine.submitted) == 3
    assert len(set(world.engine.submitted)) == 3 and world.engine.discovered


def test_repeated_polls_of_one_attempt_replace_instead_of_adding(real_ee, monkeypatch):
    world = World(recipe())
    world.engine.telemetry = {"request-1": [{"batch_eecu_usage_seconds": 3.0, "attempt": 1},
                                            {"batch_eecu_usage_seconds": 3.5, "attempt": 1},
                                            {"batch_eecu_usage_seconds": 3.5, "attempt": 1}]}
    world.engine.states = {"request-1": ["RUNNING", "RUNNING", "COMPLETED"]}
    Answers(monkeypatch, READS)
    world.runner().run(world.recipe, "coarse-run")
    task = next(t for t in record(world)["tasks"] if t["request_id"] == "request-1")
    assert task["telemetry"] == {"batch_eecu_usage_seconds": 3.5, "attempt": 1}


def test_offline_user_path_smoke_through_the_installed_cli(real_ee, monkeypatch, tmp_path, capsys):
    """Disposable offline smoke: the shipped S2 example through cli.main in personal
    mode; only the connector (store/engine/credentials) and EE evaluation are fakes."""
    from pathlib import Path

    from geecomposer import cli

    for key in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "home"))
    example = Path(__file__).resolve().parents[1] / "examples" / "recipes" / "coarse_s2_monthly_2025.json"
    clock, boundary = Clock(), Boundary()
    store = Store(boundary)
    engine = ScienceEngine(boundary, store, clock)
    connected = []

    def connector(settings, budget, *, credentials, state):
        connected.append(settings["limits"]["outputs"])
        return store, engine, object()

    credentials = tmp_path / "login" / "credentials.json"  # never read: the connector is a fake
    args = ["--project", "test-project", "--credentials", str(credentials), "--state-dir", str(tmp_path / "state")]
    answers = Answers(monkeypatch, READS)
    code = cli.main(args + ["run", "--recipe", str(example), "--run-id", "smoke-run"], connector=connector,
                    budget_factory=lambda seconds: clock.budget(seconds))
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["status"] == "succeeded" and answers.calls == 4 and connected == [12]
    assert [t["name"] for t in result["tasks"]] == ["p2025_06", "p2025_07", "p2025_12"]
    replay = Answers(monkeypatch, [])
    code = cli.main(args + ["status", "--run-id", "smoke-run"], connector=connector,
                    budget_factory=lambda seconds: clock.budget(seconds))
    assert code == 0 and json.loads(capsys.readouterr().out)["status"] == "succeeded" and replay.calls == 0


# ------------------------------------------------------------------ effective scene ceiling (S03-F1)


def inline_eight(monkeypatch, **limits):
    snap, _ = read(monkeypatch, [collection(C1), collection(C2), collection(CS)])
    value = json.loads(json.dumps(recipe(snapshot=runner_coarse.encode_snapshot(snap))))
    value["destination"] = dict(bucket="output-bucket", prefix="products/coarse")
    if limits:
        value["limits"] = limits
    return snap, value


@pytest.mark.parametrize("source", ["recipe", "policy"])
def test_oversized_inline_snapshot_is_refused_before_any_state_or_service(real_ee, monkeypatch, source):
    _, value = inline_eight(monkeypatch, **({"scenes": 1} if source == "recipe" else {}))
    world = World(recipe())
    if source == "policy":
        world.policy = validate_policy(dict(policy_value(), limits={"scenes": 7}))
    answers = Answers(monkeypatch, [])
    with pytest.raises(RunnerError) as err:
        world.runner().run(value, "oversized")
    assert (err.value.status, str(err.value)) == ("validation", "coarse snapshot exceeds the scene ceiling")
    assert world.store.records == {} and world.boundary.trace == [] and answers.calls == 0
    assert world.engine.prepared == 0 and world.engine.submitted == []


def test_inline_snapshot_at_the_exact_ceiling_runs_with_every_member(real_ee, monkeypatch):
    snap, value = inline_eight(monkeypatch, scenes=8)
    world = World(value)
    answers = Answers(monkeypatch, [BOUNDS])
    result = world.runner().run(world.recipe, "coarse-run")
    assert result["status"] == "succeeded" and answers.calls == 1  # bounds only: no rediscovery
    stored = record(world)["provenance"]["coarse"]["snapshot"]
    assert stored["fingerprint"] == snap.fingerprint and sorted(stored["members"]) == sorted(dict(snap.projections))
    assert [t["name"] for t in record(world)["tasks"]] == ["p2025_06", "p2025_07", "p2025_12"]


@pytest.mark.parametrize("fits", [False, True])
@pytest.mark.parametrize("product", ["composite", "rao_fit"])
def test_persisted_snapshot_replay_respects_the_scene_ceiling_before_calibration(real_ee, monkeypatch, product, fits):
    """The actual preparation replays a run's persisted snapshot (8 members; 2 for the
    regional fit) under an effective scenes ceiling at, or one below, its members."""
    calls = []
    monkeypatch.setattr(runner_coarse, "fit_normalization", fitted(calls))
    if product == "composite":
        value, reads, members = recipe(), READS, 8
    else:
        value, reads, members = regional_rao(), REGIONAL + [9], 2
    world, _, result = run(monkeypatch, copy.deepcopy(value), reads=reads)
    previous = record(world)["provenance"]
    assert result["status"] == "succeeded" and len(previous["coarse"]["snapshot"]["members"]) == members
    value["limits"] = {"scenes": members if fits else members - 1}
    replay = World(value).recipe
    fitted_before, science = len(calls), []
    original = runner_coarse.coarse.composite
    monkeypatch.setattr(runner_coarse.coarse, "composite", lambda *a, **k: science.append(1) or original(*a, **k))
    answers = Answers(monkeypatch, [])  # no rediscovery and no new fit
    if fits:
        tasks, provenance = prepare(replay, "prefix", world.clock.budget(), previous)
        assert tasks and provenance["coarse"]["snapshot"] == previous["coarse"]["snapshot"]
    else:
        with pytest.raises(RunnerError) as err:
            prepare(replay, "prefix", world.clock.budget(), previous)
        assert (err.value.status, str(err.value), err.value.phase) == (
            "conflict", "coarse snapshot exceeds the scene ceiling", "scenes")
        assert science == []
    assert answers.calls == 0 and len(calls) == fitted_before


# ------------------------------------------------------------------ former-bypass records (S03-F4)

SCENES = "coarse snapshot exceeds the scene ceiling"
WINDOW = "coarse Rao 3x3 support exceeds the window ceiling"
STORE_EVENTS = {"read.before", "read.after", "write.before", "write.after"}


def as_legacy(state, policy, engine, **lower):
    """SYNTHETIC pre-correction record; no actual old record was inspected.

    The stored run of an admissible twin, rebound exactly as the 207/208 code bound
    the same recipe under the lower ``limits``. The canonical recipe, local source
    digest, recipe digest, content binding and task markers all come from the real
    runner functions. The fake service keeps each submitted operation under its
    rebound marker, as if it had been submitted with it."""
    assert state["source"] == {"kind": "local", "sha256": digest(state["recipe"])}
    recipe = dict(state["recipe"], limits=dict(state["recipe"]["limits"], **lower))
    source = {"kind": "local", "sha256": digest(recipe)}
    legacy = dict(state, recipe=recipe, source=source, recipe_sha256=digest(recipe),
                  binding=binding(recipe, policy, source))
    legacy["tasks"] = [dict(t, marker=discovery_marker(t["request_id"], legacy["binding"], t["name"]))
                       for t in state["tasks"]]
    for old, new in zip(state["tasks"], legacy["tasks"]):
        if old["marker"] in engine.markers:
            engine.markers[new["marker"]] = engine.markers.pop(old["marker"])
    return legacy


def legacy_twin(monkeypatch, form):
    """An admissible twin recipe, its fake service reads, the lower limits that make
    the same recipe a former-bypass form, and the refusal strict paths give it."""
    if form == "scenes":
        _, value = inline_eight(monkeypatch)
        value["destination"] = {"drive_folder": "geecomposer_coarse"}
        return value, [BOUNDS], {"scenes": 1}, SCENES
    if form == "window_model":
        value = recipe(product="rao", rao={"model": copy.deepcopy(SUPPLIED_MODEL)})
        value["bands"] = ["B8", "B3", "B2", "B4"]
        return value, READS, {"window": 1}, WINDOW
    monkeypatch.setattr(runner_coarse, "fit_normalization", fitted([]))
    if form == "window_fit":
        return regional_rao(), REGIONAL + [9], {"window": 1}, WINDOW
    snap, _ = read(monkeypatch, REGIONAL[1:], regional_rao())  # two members
    value = json.loads(json.dumps(regional_rao(snapshot=runner_coarse.encode_snapshot(snap))))
    return value, [REGION_BOUNDS, 9], {"scenes": 1, "window": 1}, WINDOW


def submitted_record(monkeypatch, form, mode="gcs"):
    """Run the twin to its first status poll (operations saved, owner lease live),
    then store it as the former-bypass record."""
    value, reads, lower, refusal = legacy_twin(monkeypatch, form)
    world = World(value, mode)
    Answers(monkeypatch, reads)

    def crash(name):
        if name == "status.before":
            raise Crash(name)

    world.boundary.hook = crash
    with pytest.raises(Crash):
        world.runner().run(world.recipe, "legacy-run")
    world.boundary.hook = None
    state, generation = world.store.records["legacy-run"]
    state = as_legacy(state, world.policy, world.engine, **lower)
    world.store.records["legacy-run"] = (state, generation)
    active = [t["request_id"] for t in state["tasks"] if t.get("operation")]
    world.engine.states = {r: ["RUNNING"] for r in active}
    return world, state, active, refusal


@pytest.mark.parametrize("mode", ["gcs", "personal"])
@pytest.mark.parametrize("form", ["scenes", "window_model", "window_fit", "both"])
def test_former_bypass_records_keep_status_and_bounded_cancellation(real_ee, monkeypatch, form, mode):
    world, saved, active, refusal = submitted_record(monkeypatch, form, mode)
    stored = copy.deepcopy(world.store.records["legacy-run"])
    submitted, prepared = list(world.engine.submitted), world.engine.prepared
    answers = Answers(monkeypatch, [])  # no source discovery, fit or graph evaluation
    assert active and saved["owner"] and saved["status"] == "running"

    # Execution stays refused, before any preparation, state or service call.
    for attempt in (lambda: world.runner().resume("legacy-run"),
                    lambda: world.runner().run(copy.deepcopy(saved["recipe"]), "legacy-new")):
        with pytest.raises(RunnerError) as err:
            attempt()
        assert (err.value.status, str(err.value)) == ("validation", refusal)
    assert "legacy-new" not in world.store.records and world.store.records["legacy-run"] == stored

    # Status reports the trusted saved state: no service call, preparation or write.
    world.boundary.trace.clear()
    shown = world.runner().status("legacy-run")
    assert shown["status"] == "running" and [t["name"] for t in shown["tasks"]] == [t["name"] for t in saved["tasks"]]
    assert set(world.boundary.trace) <= STORE_EVENTS and world.store.records["legacy-run"] == stored

    # Live owner: a stale generation is still rejected without a write ...
    def interleave(name):
        if name == "write.before":
            world.boundary.hook = None
            state, generation = world.store.records["legacy-run"]
            world.store.records["legacy-run"] = (state, generation + 1)  # the worker saved first

    world.boundary.hook = interleave
    with pytest.raises(RunnerError) as err:
        world.runner().cancel("legacy-run")
    assert (err.value.status, str(err.value)) == ("conflict", "conditional state conflict")
    assert world.store.records["legacy-run"][0] == stored[0]
    # ... and otherwise the stop request persists through CAS, keeping owner and tasks.
    world.boundary.trace.clear()
    assert world.runner().cancel("legacy-run")["status"] == "cancelling"
    stopped, _ = world.store.records["legacy-run"]
    assert stopped["stop"] == "cancelled" and stopped["owner"] == saved["owner"]
    assert stopped["tasks"] == saved["tasks"] and stopped["binding"] == saved["binding"]
    assert set(world.boundary.trace) <= STORE_EVENTS and world.engine.cancel_calls == []

    # Expired lease: only the recorded operations, confirmed by marker, are cancelled.
    world.clock.now += LEASE_SECONDS + 1
    world.boundary.trace.clear()
    result = world.runner().cancel("legacy-run")
    final, _ = world.store.records["legacy-run"]
    assert result["status"] == "cancelled" and sorted(world.engine.cancel_calls) == sorted(active)
    assert world.engine.submitted == submitted and world.engine.prepared == prepared and answers.calls == 0
    assert not {"prepare.before", "new_id.before", "submit.before"} & set(world.boundary.trace)
    assert all(t["state"] == "prepared" for t in final["tasks"] if t["request_id"] not in active)
    assert final["recipe"] == saved["recipe"] and final["binding"] == saved["binding"] and final["owner"] is None


MARKER = "missing or invalid discovery marker; inspect private manifest and obtain operator evidence; do not resubmit"
TAMPER = {
    "policy": (lambda s, w: s.update(policy_sha256="0" * 64), ("conflict", "run policy binding mismatch")),
    "content": (lambda s, w: s["recipe"]["coarse"].update(coverage_min=0.6), ("conflict", "run content binding mismatch")),
    "source": (lambda s, w: s["source"].update(sha256="0" * 64), ("conflict", "run content binding mismatch")),
    "snapshot": (lambda s, w: next(iter(s["recipe"]["coarse"]["snapshot"]["members"].values())).__setitem__(2, [1, 1]),
                 ("validation", "coarse request refused: snapshot_invalid")),
    "recipe": (lambda s, w: s["recipe"].update(reducer="mean"),
               ("validation", "the coarse section owns reducer, transform and Rao options")),
    "prefix": (lambda s, w: s.update(prefix=s["recipe"]["destination"]["prefix"] + "/another-run-0"),
               ("conflict", "invalid reserved prefix")),
    "marker": (lambda s, w: s["tasks"][0].update(marker="0" * 64), ("uncertain", MARKER)),
    "operation": (lambda s, w: w.engine.markers.update({s["tasks"][0]["marker"]: "projects/test-project/operations/other"}),
                  ("conflict", "saved operation differs from its submission marker; not used")),
}


@pytest.mark.parametrize("case", sorted(TAMPER))
def test_compatibility_route_still_refuses_tampered_or_mismatched_records(real_ee, monkeypatch, case):
    """Identity and structure checks still refuse on the status/cancel route, each with
    its own reason (not the execution-admission refusal), before any write or cancel."""
    world, _, _, _ = submitted_record(monkeypatch, "both")
    change, expected = TAMPER[case]
    state, generation = world.store.records["legacy-run"]
    change(state, world)
    world.store.records["legacy-run"] = (state, generation)
    stored = copy.deepcopy(world.store.records["legacy-run"])
    answers = Answers(monkeypatch, [])
    calls = []
    if case != "operation":  # status and a live-owner stop never contact the service
        calls += [lambda: world.runner().status("legacy-run"), lambda: world.runner().cancel("legacy-run")]
    calls.append(lambda: (world.clock.sleep(LEASE_SECONDS + 1), world.runner().cancel("legacy-run")))
    for call in calls:
        with pytest.raises(RunnerError) as err:
            call()
        assert (err.value.status, str(err.value)) == expected
        assert world.store.records["legacy-run"] == stored and world.engine.cancel_calls == []
    assert answers.calls == 0
