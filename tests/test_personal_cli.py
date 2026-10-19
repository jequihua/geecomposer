"""Personal-default CLI: explicit user EE credentials, Drive tasks, local state.

Fake credential/transport/EE-task boundaries and temporary stores only. No test
reads a real saved login, initializes live Earth Engine or opens a connection.
"""

import importlib
import inspect
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import ee
import google_auth_httplib2
import pytest
from ee import oauth
from google.oauth2.credentials import Credentials

import runner_fakes as fakes
from geecomposer import cli, export_to_drive, initialize
from geecomposer import runner_personal as personal
from geecomposer.exceptions import GeeComposerError
from geecomposer.recipe import RunnerError, canonical, validate_policy, validate_recipe
from geecomposer.runner import LEASE_SECONDS, Runner
from geecomposer.runner_cloud import EarthEngine, EETransport
from geecomposer.runner_science import config_fingerprint, prepare
from runner_fakes import Boundary, Clock, Crash, World, policy_value, recipe_value

pytestmark = pytest.mark.usefixtures("runner_offline")

SECRET = "SECRET-REFRESH-TOKEN"
PROJECT = "personal-project"
FOLDER = "geecomposer-tests"
drive_module = importlib.import_module("geecomposer.export.drive")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect every default location; no real saved login can be opened."""
    home = tmp_path / "home"
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setattr(
        oauth, "get_credentials_path", lambda: str(home / ".config/earthengine/credentials")
    )
    return home


def drive_recipe(**changes):
    value = recipe_value()
    value["destination"] = {"drive_folder": FOLDER}
    value.update(changes)
    return value


def credential_file(directory, **fields):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "credentials"
    path.write_text(json.dumps(dict(dict(refresh_token=SECRET, scopes=oauth.SCOPES), **fields)))
    return path


class Store(personal.LocalStore):
    """Real local store plus the read-only view the fake engine inspects."""

    @property
    def records(self):
        return {p.stem: self._read(p.stem) for p in self.root.glob("*.json")}


class Engine(fakes.Engine):
    def initialize(self, credentials, project):
        assert type(credentials) is Credentials
        assert credentials.refresh_token == SECRET
        assert project == self.project
        super().initialize(credentials, project)


class Personal:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.clock, self.boundary = Clock(), Boundary()
        self.state = tmp_path / "state"
        self.credentials = credential_file(tmp_path / "secrets")
        self.engine = Engine(self.boundary, None)
        self.engine.project = PROJECT
        self.calls = []

    def connector(self, settings, budget, *, credentials, state):
        self.calls.append(dict(settings=settings, credentials=credentials, state=state))
        store = Store(state)
        self.engine.store = store
        if credentials is None:
            return store, None, None
        return store, self.engine, personal.load_credentials(credentials)

    def recipe(self, value=None):
        path = self.tmp / "recipe.json"
        path.write_bytes(canonical(drive_recipe() if value is None else value))
        return path

    def options(self, project=PROJECT):
        return [
            "--project",
            project,
            "--credentials",
            str(self.credentials),
            "--state-dir",
            str(self.state),
        ]

    def main(self, capsys, *args, options=None):
        code = cli.main(
            [*(self.options() if options is None else options), *args],
            connector=self.connector,
            budget_factory=self.clock.budget,
        )
        return code, json.loads(capsys.readouterr().out)

    def record(self, run):
        return Store(self.state)._read(run)


def fail(*args, **kwargs):
    pytest.fail("personal preconditions reached credentials, state or EE")


# ---------------------------------------------------------------- recipe shape


@pytest.mark.parametrize(
    "value",
    [
        {"drive_folder": "a/b"},
        {"drive_folder": "a\\b"},
        {"drive_folder": ""},
        {"drive_folder": " leading"},
        {"drive_folder": "gs://bucket"},
        {"drive_folder": "x" * 101},
        {"drive_folder": 5},
        {"drive_folder": FOLDER, "bucket": "output-bucket"},
        {"drive_folder": FOLDER, "prefix": "products"},
        {"folder": FOLDER},
        [FOLDER],
    ],
)
def test_drive_destination_is_strict_and_never_mixed(value):
    with pytest.raises(RunnerError) as exc:
        validate_recipe(drive_recipe(destination=value))
    assert exc.value.status == "validation"


def test_destination_shapes_keep_institutional_schema():
    assert validate_recipe(drive_recipe())["destination"] == {"drive_folder": FOLDER}
    # Existing GCS recipes still validate without policy and are not reinterpreted.
    assert validate_recipe(recipe_value())["destination"] == recipe_value()["destination"]
    policy = validate_policy(policy_value())
    with pytest.raises(RunnerError) as exc:
        validate_recipe(drive_recipe(), policy)
    assert exc.value.status == "validation"
    with pytest.raises(RunnerError):
        personal.validate(recipe_value())


# ------------------------------------------------------------ CLI orchestration


def test_personal_default_run_status_and_credential_state_isolation(tmp_path, capsys):
    w = Personal(tmp_path)
    recipe = w.recipe()
    code, out = w.main(capsys, "run", "--recipe", str(recipe), "--run-id", "first")
    assert code == 0
    assert out["status"] == "succeeded"
    assert out["mode"] == "personal"
    assert out["project"] == PROJECT
    assert out["raster_verification"] == "pending"
    assert out["destination"] == {"drive_folder": FOLDER}
    assert out["remote_pending"] is False
    state, generation = w.record("first")
    assert generation > 3
    assert len(out["tasks"]) == 3 == len(w.engine.submitted)
    for task, saved in zip(out["tasks"], state["tasks"]):
        assert task["state"] == "COMPLETED"
        assert task["request_id"] == saved["request_id"]
        assert saved["operation"].endswith("/" + task["task_id"])
        assert task["file_name_prefix"] == f"{state['prefix']}_{task['name']}"
    assert "http" not in json.dumps(out) and "file_id" not in json.dumps(out)
    assert state["mode"] == "personal" and state["project"] == PROJECT
    assert not {"bucket_preflight", "inventory", "principal", "image_version"} & state.keys()
    call = w.calls[0]
    assert call["credentials"] == w.credentials and call["state"] == w.state
    assert call["settings"] == personal.settings(PROJECT)
    # State is separate from credentials and holds no credential material.
    assert sorted(p.name for p in w.state.iterdir()) == ["first.json", "first.lock"]
    for path in w.state.iterdir():
        assert SECRET not in path.read_bytes().decode("utf-8", "replace")
    assert SECRET not in json.dumps(out)
    if os.name != "nt":
        assert (w.state / "first.json").stat().st_mode & 0o777 == 0o600
    # status is a local read: no credential file, even an absent one.
    w.credentials.unlink()
    code, status = w.main(capsys, "status", "--run-id", "first")
    # Offline status only has saved claims; the run itself verified them remotely.
    assert {t["operation_evidence"] for t in out["tasks"]} == {"verified"}
    assert {t["operation_evidence"] for t in status["tasks"]} == {"saved"}
    for task in status["tasks"]:
        task["operation_evidence"] = "verified"
    assert code == 0 and status == out
    assert w.calls[-1]["credentials"] is None
    credential_file(tmp_path / "secrets")
    code, conflict = w.main(capsys, "run", "--recipe", str(recipe), "--run-id", "first")
    assert (code, conflict["status"]) == (5, "conflict")
    assert len(w.engine.submitted) == 3


def test_documented_default_login_and_state_locations(tmp_path, capsys, isolated_home):
    w = Personal(tmp_path)
    saved = credential_file(isolated_home / ".config/earthengine")
    code, out = w.main(
        capsys,
        "run",
        "--recipe",
        str(w.recipe()),
        "--run-id",
        "defaults",
        options=["--project", PROJECT],
    )
    assert code == 0 and out["status"] == "succeeded"
    assert w.calls[0]["credentials"] == saved
    assert w.calls[0]["state"] == isolated_home / ".geecomposer" / "runs"
    assert (isolated_home / ".geecomposer" / "runs" / "defaults.json").is_file()


@pytest.mark.parametrize(
    "case",
    [
        "no_project",
        "bad_project",
        "gcs_recipe",
        "gs_recipe",
        "generation",
        "empty_state",
        "empty_credentials",
        "state_contains_credentials",
        "state_beside_credentials",
        "no_run_id",
    ],
)
def test_personal_preconditions_precede_credentials_and_state(tmp_path, monkeypatch, capsys, case):
    w = Personal(tmp_path)
    monkeypatch.setattr(personal, "load_credentials", fail)
    monkeypatch.setattr(personal, "LocalStore", fail)
    options = w.options()
    args = ["run", "--recipe", str(w.recipe()), "--run-id", "x"]
    if case == "no_project":
        options = options[2:]
    elif case == "bad_project":
        options[1] = "Bad_Project"
    elif case == "gcs_recipe":
        args[2] = str(w.recipe(recipe_value()))
    elif case == "gs_recipe":
        args[2] = "gs://input-bucket/recipes/x.json"
    elif case == "generation":
        args += ["--recipe-generation", "1"]
    elif case == "empty_state":
        options[5] = ""
    elif case == "empty_credentials":
        options[3] = ""
    elif case == "state_contains_credentials":
        options[5] = str(w.credentials.parent)
    elif case == "state_beside_credentials":
        options[5] = str(w.credentials.parent / "state")
    else:
        args = args[:3]
    code = cli.main([*options, *args], connector=fail, budget_factory=w.clock.budget)
    output = capsys.readouterr().out
    assert code == 2 and json.loads(output)["status"] == "validation"
    assert SECRET not in output and str(tmp_path) not in output
    assert not w.state.exists()


def write_hosted(tmp_path, monkeypatch, value=None):
    path = tmp_path / "runtime_policy.json"
    path.write_bytes(canonical(value or dict(policy_value(), mode="hosted")))
    monkeypatch.setattr(cli, "BUNDLED_POLICY", path)


@pytest.mark.parametrize("mode", ["bundled", "hosted_flag", "policy"])
@pytest.mark.parametrize("option", ["--project", "--credentials", "--state-dir"])
@pytest.mark.parametrize("command", ["run", "status", "validate"])
def test_institutional_modes_reject_personal_options_before_any_read(
    tmp_path, monkeypatch, capsys, mode, option, command
):
    if mode == "bundled":
        write_hosted(tmp_path, monkeypatch)
    prefix = {"bundled": [], "hosted_flag": ["--hosted"], "policy": ["--policy", "SECRET.json"]}
    values = {
        "--project": PROJECT,
        "--credentials": str(tmp_path / "SECRET-credentials"),
        "--state-dir": str(tmp_path / "SECRET-state"),
    }
    args = {
        "run": ["run", "--recipe", "gs://input-bucket/r.json", "--recipe-generation", "1", "--run-id", "x"],
        "status": ["status", "--run-id", "x"],
        "validate": ["validate", "SECRET.json"],
    }[command]
    # Rejection precedes even the policy read, credential loading and state creation.
    monkeypatch.setattr(cli, "read_json", fail)
    monkeypatch.setattr(personal, "load_credentials", fail)
    code = cli.main([*prefix[mode], option, values[option], *args], connector=fail)
    output = capsys.readouterr().out
    assert code == 2
    assert json.loads(output)["message"] == "personal options are forbidden in institutional or hosted mode"
    assert "SECRET" not in output
    assert not (tmp_path / "SECRET-state").exists()


def test_bundled_hosted_image_rejects_drive_recipe_and_never_downgrades(tmp_path, monkeypatch, capsys):
    w = World()
    w.policy["mode"] = "hosted"
    write_hosted(tmp_path, monkeypatch, w.policy)
    w.store.read_recipe = lambda uri, generation: (drive_recipe(), {"uri": uri, "generation": 1})
    monkeypatch.setattr(personal, "connect", fail)
    args = ["run", "--recipe", "gs://input-bucket/recipes/r.json", "--recipe-generation", "1"]
    code = cli.main(
        [*args, "--run-id", "hosted"],
        connector=lambda policy, budget: (w.store, w.engine, object()),
        budget_factory=w.clock.budget,
    )
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "validation"
    assert w.engine.submitted == [] and w.store.records == {}
    # No personal fallback: status still uses the attached-account connector.
    code = cli.main(["status", "--run-id", "hosted"], connector=lambda p, b: (w.store, w.engine, object()))
    assert code == 5 and json.loads(capsys.readouterr().out)["status"] == "conflict"


def test_institutional_policy_keeps_gcs_semantics(tmp_path, capsys):
    policy = tmp_path / "policy.json"
    policy.write_bytes(canonical(policy_value()))
    w = Personal(tmp_path)
    drive = w.recipe()
    assert cli.main(["--policy", str(policy), "validate", str(drive)]) == 2
    capsys.readouterr()
    code = cli.main(
        ["--policy", str(policy), "run", "--recipe", str(drive), "--run-id", "x"], connector=fail
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "validation"
    world = World()
    gcs = tmp_path / "gcs.json"
    gcs.write_bytes(canonical(recipe_value()))
    code = cli.main(
        ["--policy", str(policy), "run", "--recipe", str(gcs), "--run-id", "inst"],
        connector=lambda p, b: (world.store, world.engine, object()),
        budget_factory=world.clock.budget,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["outputs"][0].startswith("gs://output-bucket/products/test/inst-")


@pytest.mark.parametrize("options", [[], ["--project", PROJECT, "--credentials", "absent"]])
@pytest.mark.parametrize("recipe", ["drive", "gcs"])
@pytest.mark.parametrize("command", ["validate", "dry-run"])
def test_offline_personal_commands_never_open_credentials(
    tmp_path, monkeypatch, capsys, options, recipe, command
):
    monkeypatch.setattr(personal, "load_credentials", fail)
    monkeypatch.setattr(personal, "connect", fail)
    monkeypatch.setattr(personal, "LocalStore", fail)
    monkeypatch.setattr(ee, "Initialize", fail)
    path = tmp_path / "recipe.json"
    path.write_bytes(canonical(drive_recipe() if recipe == "drive" else recipe_value()))
    args = ["validate", str(path)] if command == "validate" else ["run", "--recipe", str(path), "--dry-run"]
    assert cli.main([*options, *args], connector=fail) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("validated" if command == "validate" else "dry_run")
    assert result["destination"] == (drive_recipe() if recipe == "drive" else recipe_value())["destination"]


# ------------------------------------------------------ crash, recovery, cancel


def crash_at(w, point):
    crashed = []

    def hook(name):
        if name == point and not crashed:
            crashed.append(name)
            raise Crash(name)

    w.boundary.hook = hook


@pytest.mark.parametrize("point", ["submit.before", "submit.after"])
def test_crash_around_submission_never_submits_blindly(tmp_path, capsys, point):
    w = Personal(tmp_path)
    w.engine.products = 1
    crash_at(w, point)
    with pytest.raises(Crash):
        cli.main(
            [*w.options(), "run", "--recipe", str(w.recipe()), "--run-id", "crash"],
            connector=w.connector,
            budget_factory=w.clock.budget,
        )
    capsys.readouterr()
    state, _ = w.record("crash")
    (task,) = state["tasks"]
    # Identity and intent were durable before the remote call.
    assert task["state"] == "submitting" and task["request_id"] == "request-1"
    assert task["marker"] and task["fingerprint"] and "operation" not in task
    # A dead process is not an EE cancellation; status says remote work may exist.
    code, status = w.main(capsys, "status", "--run-id", "crash")
    assert (code, status["status"], status["remote_pending"]) == (10, "running", True)
    code, busy = w.main(capsys, "resume", "--run-id", "crash")
    assert (code, busy["status"]) == (5, "conflict")
    w.clock.now += LEASE_SECONDS + 1
    code, resumed = w.main(capsys, "resume", "--run-id", "crash")
    assert w.engine.allocated == ["request-1"]
    if point == "submit.before":
        # Marker discovery finds nothing: unresolved, never resent.
        assert (code, resumed["status"]) == (6, "uncertain")
        assert w.engine.submitted == []
        assert resumed["tasks"][0]["state"] == "UNKNOWN"
        assert resumed["remote_pending"] is True
    else:
        assert (code, resumed["status"]) == (0, "succeeded")
        assert w.engine.submitted == ["request-1"]
        assert resumed["tasks"][0]["task_id"] == "server-1"


@pytest.mark.parametrize("point,code,status", [("submit.after", 8, "cancelled"), ("submit.before", 6, "uncertain")])
def test_cancel_after_crash_is_reconciled_honestly(tmp_path, capsys, point, code, status):
    w = Personal(tmp_path)
    w.engine.products = 1
    w.engine.states = {"request-1": ["RUNNING"]}
    crash_at(w, point)
    with pytest.raises(Crash):
        cli.main(
            [*w.options(), "run", "--recipe", str(w.recipe()), "--run-id", "stop"],
            connector=w.connector,
            budget_factory=w.clock.budget,
        )
    capsys.readouterr()
    # Owner lease still live: only a durable stop request, no EE call.
    result = w.main(capsys, "cancel", "--run-id", "stop")
    assert result[0] == 10 and result[1]["status"] == "cancelling"
    assert w.engine.cancel_calls == []
    w.clock.now += LEASE_SECONDS + 1
    result = w.main(capsys, "resume", "--run-id", "stop")
    assert result[0] == code and result[1]["status"] == status
    if point == "submit.after":
        assert w.engine.cancel_calls == ["request-1"]
        assert result[1]["remote_pending"] is False
    else:
        # Undiscoverable submission: cancellation cannot be claimed.
        assert w.engine.cancel_calls == []
        assert result[1]["remote_pending"] is True
    assert w.engine.submitted == (["request-1"] if point == "submit.after" else [])


def test_personal_view_never_shows_an_older_attempt_as_current_after_reload(tmp_path, capsys):
    """Real local JSON state (S03-F3): attempt 2 without valid EECU stays unknown
    through reload, offline status and a late attempt-1 response on resume."""
    w = Personal(tmp_path)
    w.engine.products = 1
    w.engine.states = {"request-1": ["RUNNING"]}
    replies = [
        {"batch_eecu_usage_seconds": 5.5, "attempt": 1},
        {"batch_eecu_usage_seconds": float("nan"), "attempt": 2},
    ]
    status = w.engine.status

    def reply(request_id, operation=None):
        result = status(request_id, operation)
        if replies:
            result.update(replies.pop(0))
        return result

    w.engine.status = reply
    polls = []

    def crash(name):
        if name == "status.before":
            polls.append(name)
            if len(polls) == 3:
                raise Crash(name)

    w.boundary.hook = crash
    with pytest.raises(Crash):
        cli.main(
            [*w.options(), "run", "--recipe", str(w.recipe()), "--run-id", "eecu"],
            connector=w.connector,
            budget_factory=w.clock.budget,
        )
    capsys.readouterr()
    w.boundary.hook = None
    unknown = {"attempt": 2, "batch_eecu_usage_seconds": None}
    state, _ = w.record("eecu")
    assert state["tasks"][0]["telemetry"] == unknown
    code, shown = w.main(capsys, "status", "--run-id", "eecu")
    assert code == 10 and shown["tasks"][0]["telemetry"] == unknown
    w.clock.now += LEASE_SECONDS + 1
    replies += [
        {"batch_eecu_usage_seconds": 5.5, "attempt": 1},
        {"batch_eecu_usage_seconds": 6.25, "attempt": 2},
    ]
    w.engine.states = {"request-1": ["RUNNING", "RUNNING", "COMPLETED"]}
    code, resumed = w.main(capsys, "resume", "--run-id", "eecu")
    current = {"attempt": 2, "batch_eecu_usage_seconds": 6.25}
    assert code == 0 and resumed["tasks"][0]["telemetry"] == current
    assert w.record("eecu")[0]["tasks"][0]["telemetry"] == current


@pytest.mark.parametrize("form", ["window", "scenes"])
def test_personal_former_bypass_record_keeps_status_and_cancel_commands(real_ee, tmp_path, monkeypatch, capsys, form):
    """S03-F4 through the public personal commands, the existing fake connector and
    real local JSON state. SYNTHETIC former-bypass record (see test_coarse_runner's
    as_legacy); no actual old record was inspected."""
    from geecomposer import runner_coarse
    from test_coarse_metadata import eight_members, recipe as coarse_recipe
    from test_coarse_recipe import SUPPLIED_MODEL
    from test_coarse_runner import as_legacy

    if form == "window":
        value = coarse_recipe(product="rao", rao={"model": SUPPLIED_MODEL})
        value["bands"] = ["B8", "B3", "B2", "B4"]
        lower, refusal = {"window": 1}, "coarse Rao 3x3 support exceeds the window ceiling"
    else:
        snapshot = runner_coarse.encode_snapshot(eight_members(monkeypatch))
        value = json.loads(json.dumps(coarse_recipe(snapshot=snapshot)))
        lower, refusal = {"scenes": 1}, "coarse snapshot exceeds the scene ceiling"
    w = Personal(tmp_path)
    crash_at(w, "status.before")
    with pytest.raises(Crash):
        cli.main(
            [*w.options(), "run", "--recipe", str(w.recipe(value)), "--run-id", "legacy"],
            connector=w.connector,
            budget_factory=w.clock.budget,
        )
    capsys.readouterr()
    w.boundary.hook = None
    state, generation = w.record("legacy")
    legacy = as_legacy(state, personal.settings(PROJECT), w.engine, **lower)
    Store(w.state).write("legacy", legacy, generation)
    active = [t["request_id"] for t in legacy["tasks"] if t.get("operation")]
    w.engine.states = {r: ["RUNNING"] for r in active}
    code, shown = w.main(capsys, "status", "--run-id", "legacy")
    assert (code, shown["status"]) == (10, "running")
    code, refused = w.main(capsys, "resume", "--run-id", "legacy")
    assert (code, refused) == (2, {"message": refusal, "status": "validation"})
    code, stopping = w.main(capsys, "cancel", "--run-id", "legacy")
    assert (code, stopping["status"]) == (10, "cancelling") and w.engine.cancel_calls == []
    w.clock.now += LEASE_SECONDS + 1
    code, done = w.main(capsys, "cancel", "--run-id", "legacy")
    assert (code, done["status"]) == (8, "cancelled") and sorted(w.engine.cancel_calls) == sorted(active)
    saved, _ = w.record("legacy")
    assert w.engine.submitted == active and saved["recipe"] == legacy["recipe"]
    assert saved["binding"] == legacy["binding"] and saved["stop"] == "cancelled"


def test_interrupt_while_polling_cancels_through_fenced_control_flow(tmp_path, capsys):
    w = Personal(tmp_path)
    w.engine.products = 1
    w.engine.states = {"request-1": ["RUNNING"]}
    original = signal.getsignal(signal.SIGINT)
    sent = []

    def interrupt(name):
        if name == "status.before" and not sent:
            sent.append(name)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    w.boundary.hook = interrupt
    code, out = w.main(capsys, "run", "--recipe", str(w.recipe()), "--run-id", "sigint")
    assert (code, out["status"]) == (8, "cancelled")
    assert w.engine.cancel_calls == ["request-1"]
    assert signal.getsignal(signal.SIGINT) is original
    state, _ = w.record("sigint")
    assert state["owner"] is None and state["stop"] == "cancelled"


@pytest.mark.parametrize("change", ["project", "recipe", "corrupt", "foreign"])
def test_resume_fails_closed_on_binding_changes(tmp_path, capsys, change):
    w = Personal(tmp_path)
    w.engine.products = 1
    crash_at(w, "submit.before")
    with pytest.raises(Crash):
        cli.main(
            [*w.options(), "run", "--recipe", str(w.recipe()), "--run-id", "bound"],
            connector=w.connector,
            budget_factory=w.clock.budget,
        )
    capsys.readouterr()
    w.clock.now += LEASE_SECONDS + 1
    record = w.state / "bound.json"
    options = w.options()
    if change == "project":
        options = w.options(project="other-project")
    elif change == "recipe":
        state, generation = w.record("bound")
        state["recipe"]["destination"] = {"drive_folder": "someone-else"}
        Store(w.state).write("bound", state, generation)
    elif change == "corrupt":
        record.write_bytes(record.read_bytes()[:-5])
    else:
        record.write_bytes(canonical({"unrelated": "file"}))
    before = record.read_bytes()
    code, out = w.main(capsys, "resume", "--run-id", "bound", options=options)
    expected = {"project": 5, "recipe": 5, "corrupt": 6, "foreign": 6}[change]
    assert code == expected, out
    assert record.read_bytes() == before
    assert w.engine.submitted == []


# ------------------------------------------------------------------ local store


def test_local_store_generation_contract_and_private_files(tmp_path):
    store = personal.LocalStore(tmp_path / "runs")
    assert store.read("run") == (None, 0)
    assert list((tmp_path / "runs").iterdir()) == []
    state = {"run_id": "run", "value": 1}
    assert store.write("run", state, 0) == 1
    with pytest.raises(RunnerError) as exc:
        store.write("run", state, 0)
    assert exc.value.status == "conflict"
    assert store.write("run", dict(state, value=2), 1) == 2
    assert store.read("run") == (dict(state, value=2), 2)
    with pytest.raises(RunnerError) as exc:
        personal.LocalStore(tmp_path / "other").write("other", state, 0)
    assert exc.value.status == "conflict"
    assert list((tmp_path / "other").iterdir()) == []
    with pytest.raises(RunnerError):
        store.read("../escape")
    assert not (tmp_path / "escape.lock").exists()
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == ["run.json", "run.lock"]


@pytest.mark.parametrize(
    "content",
    [b"unrelated notes", b"", canonical({"format": "x", "generation": 1, "state": {}}), b"{" * 10],
)
def test_local_store_never_replaces_unrecognized_files(tmp_path, content):
    store = personal.LocalStore(tmp_path)
    (tmp_path / "run.json").write_bytes(content)
    for action in (lambda: store.read("run"), lambda: store.write("run", {"run_id": "run"}, 0)):
        with pytest.raises(RunnerError) as exc:
            action()
        assert exc.value.status == "uncertain"
    assert (tmp_path / "run.json").read_bytes() == content


def test_local_store_record_bound_to_its_run(tmp_path):
    store = personal.LocalStore(tmp_path)
    store.write("one", {"run_id": "one"}, 0)
    (tmp_path / "two.json").write_bytes((tmp_path / "one.json").read_bytes())
    with pytest.raises(RunnerError) as exc:
        store.read("two")
    assert exc.value.status == "uncertain"


def test_local_store_concurrent_owner_is_excluded(tmp_path, monkeypatch):
    store = personal.LocalStore(tmp_path)
    store.write("run", {"run_id": "run"}, 0)
    monkeypatch.setattr(personal, "LOCK_ATTEMPTS", 2)
    monkeypatch.setattr(personal, "LOCK_PAUSE", 0)
    fd = os.open(tmp_path / "run.lock", os.O_RDWR)
    try:
        personal._lock(fd)
        for action in (lambda: store.write("run", {"run_id": "run"}, 1), lambda: store.read("run")):
            with pytest.raises(RunnerError) as exc:
                action()
            assert exc.value.status == "conflict"
        personal._unlock(fd)
    finally:
        os.close(fd)
    assert store.write("run", {"run_id": "run", "next": True}, 1) == 2


def test_local_store_persistence_failure_keeps_prior_record(tmp_path, monkeypatch):
    store = personal.LocalStore(tmp_path)
    store.write("run", {"run_id": "run"}, 0)
    before = (tmp_path / "run.json").read_bytes()

    def broken(*args):
        raise OSError("disk full PRIVATE")

    monkeypatch.setattr(personal.os, "replace", broken)
    with pytest.raises(RunnerError) as exc:
        store.write("run", {"run_id": "run", "x": 1}, 1)
    assert exc.value.status == "uncertain" and "PRIVATE" not in str(exc.value)
    assert (tmp_path / "run.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.json", "run.lock"]


def test_local_store_size_bound(tmp_path):
    store = personal.LocalStore(tmp_path)
    with pytest.raises(RunnerError) as exc:
        store.write("run", {"run_id": "run", "x": "y" * personal.MAX_STATE}, 0)
    assert exc.value.status == "artifact"
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------ credential boundary


def test_saved_login_becomes_explicit_user_credentials(tmp_path):
    minimal = personal.load_credentials(credential_file(tmp_path / "a"))
    assert type(minimal) is Credentials
    assert minimal.token is None and minimal.refresh_token == SECRET
    assert minimal.token_uri == oauth.TOKEN_URI
    assert minimal.client_id == oauth.CLIENT_ID
    assert list(minimal.scopes) == oauth.SCOPES
    full = personal.load_credentials(
        credential_file(
            tmp_path / "b",
            client_id="client",
            client_secret="secret",
            redirect_uri="http://localhost",
            project="saved-project",
        )
    )
    assert full.client_id == "client" and full.client_secret == "secret"
    # The explicit CLI project, not a saved one, selects the EE project.
    assert full.quota_project_id is None


@pytest.mark.parametrize(
    "content",
    [
        None,
        "directory",
        json.dumps({"type": "service_account", "private_key": SECRET, "client_email": "a@b"}),
        json.dumps({"type": "authorized_user", "refresh_token": SECRET}),
        json.dumps({"type": "external_account", "credential_source": {}}),
        json.dumps({"scopes": oauth.SCOPES}),
        json.dumps({"refresh_token": ""}),
        json.dumps({"refresh_token": 5}),
        json.dumps({"refresh_token": SECRET, "scopes": []}),
        json.dumps({"refresh_token": SECRET, "scopes": ["earthengine"]}),
        json.dumps({"refresh_token": SECRET, "client_id": {"x": 1}}),
        '{"refresh_token": "%s", "refresh_token": "b"}' % SECRET,
        "not json " + SECRET,
        json.dumps([SECRET]),
        "oversize",
    ],
)
def test_only_supported_personal_refresh_credentials(tmp_path, content):
    path = tmp_path / "credentials"
    if content == "oversize":
        path.write_text(json.dumps({"refresh_token": SECRET, "pad": "x" * personal.MAX_CREDENTIAL}))
    elif content == "directory":
        path.mkdir()
    elif content is not None:
        path.write_text(content)
    with pytest.raises(RunnerError) as exc:
        personal.load_credentials(path)
    assert exc.value.status == "auth"
    assert SECRET not in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_personal_engine_initializes_with_explicit_object_and_bounded_transport(tmp_path, monkeypatch):
    creds = personal.load_credentials(credential_file(tmp_path))
    calls = []
    monkeypatch.setattr(ee, "Reset", lambda: calls.append("reset"))
    monkeypatch.setattr(ee.data, "setMaxRetries", lambda n: calls.append(n))
    monkeypatch.setattr(ee, "Initialize", lambda **kwargs: calls.append(kwargs))
    engine = personal.PersonalEarthEngine(Clock().budget())
    engine.initialize(creds, PROJECT)
    assert calls[:2] == ["reset", 0]
    assert calls[2]["credentials"] is creds and calls[2]["project"] == PROJECT
    transport = calls[2]["http_transport"]
    assert type(transport) is EETransport
    assert transport.hosts == ("earthengine.googleapis.com", "oauth2.googleapis.com")
    assert isinstance(engine.auth_request, google_auth_httplib2.Request)
    assert engine.auth_request.http.hosts == transport.hosts


def test_modes_do_not_accept_each_others_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "Initialize", fail)
    personal_creds = personal.load_credentials(credential_file(tmp_path))
    for engine, creds in (
        (EarthEngine(Clock().budget()), personal_creds),
        (personal.PersonalEarthEngine(Clock().budget()), object()),
        (personal.PersonalEarthEngine(Clock().budget()), Credentials("token")),
    ):
        with pytest.raises(RunnerError) as exc:
            engine.initialize(creds, PROJECT)
        assert exc.value.status == "auth"


def test_initialization_failure_is_auth_without_token_text(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError

    creds = personal.load_credentials(credential_file(tmp_path))

    def refused(**kwargs):
        raise RefreshError(
            "invalid_grant " + SECRET,
            {"error": "invalid_grant", "error_description": SECRET},
        )

    monkeypatch.setattr(ee, "Initialize", refused)
    with pytest.raises(RunnerError) as exc:
        personal.PersonalEarthEngine(Clock().budget()).initialize(creds, PROJECT)
    assert exc.value.status == "auth" and SECRET not in str(exc.value)
    assert exc.value.__cause__ is None


def token_transport(monkeypatch, pages=()):
    calls, pending = [], list(pages)

    def read_url(
        url, budget, *, headers=None, method="GET", body=None, limit=16384, identity=False
    ):
        calls.append(dict(url=url, method=method, headers=dict(headers or {})))
        if url == oauth.TOKEN_URI:
            data = {"access_token": "ACCESS", "expires_in": 3600, "token_type": "Bearer"}
            return 200, {"content-type": "application/json"}, json.dumps(data).encode()
        return 200, {}, canonical(pending.pop(0))

    monkeypatch.setattr("geecomposer.runner_cloud._read_url", read_url)
    return calls


def test_refresh_uses_only_the_fixed_token_host(tmp_path, monkeypatch):
    calls = token_transport(monkeypatch)
    budget = Clock().budget()
    creds = personal.load_credentials(credential_file(tmp_path))
    transport = EETransport(budget, personal.PersonalEarthEngine.hosts)
    creds.refresh(google_auth_httplib2.Request(transport))
    assert creds.token == "ACCESS"
    assert [(c["url"], c["method"]) for c in calls] == [(oauth.TOKEN_URI, "POST")]
    for bad, hosts in (
        (oauth.TOKEN_URI, EarthEngine.hosts),
        ("https://www.googleapis.com/drive/v3/files", personal.PersonalEarthEngine.hosts),
        ("http://oauth2.googleapis.com/token", personal.PersonalEarthEngine.hosts),
    ):
        with pytest.raises(RunnerError) as exc:
            EETransport(budget, hosts).request(bad, "POST")
        assert exc.value.status == "auth"
    assert len(calls) == 1


def test_personal_marker_discovery_refreshes_through_bounded_transport(tmp_path, monkeypatch):
    marker = "gc-" + "a" * 64
    page = {
        "operations": [
            {
                "name": f"projects/{PROJECT}/operations/found",
                "metadata": {"description": marker, "state": "RUNNING"},
            }
        ]
    }
    calls = token_transport(monkeypatch, [page])
    budget = Clock().budget()
    engine = personal.PersonalEarthEngine(budget)
    engine.credentials = personal.load_credentials(credential_file(tmp_path))
    engine.project = PROJECT
    engine.auth_request = google_auth_httplib2.Request(EETransport(budget, engine.hosts))
    assert engine.discover(marker) == f"projects/{PROJECT}/operations/found"
    assert calls[0]["url"] == oauth.TOKEN_URI
    assert calls[1]["url"].startswith(
        f"https://earthengine.googleapis.com/v1/projects/{PROJECT}/operations?"
    )
    assert calls[1]["headers"]["authorization"] == "Bearer ACCESS"


# ----------------------------------------------------------- native Drive graphs


def expression_chain(config, recipe):
    clipped = config["expression"]
    assert clipped.func.getSignature()["name"] == "Image.clip"
    projected = clipped.args["input"]
    assert projected.func.getSignature()["name"] == "Image.reproject"
    doubled = projected.args["image"]
    assert doubled.func.getSignature()["name"] == "Image.toDouble"
    grid = recipe["grid"]
    assert projected.args["crs"].serialize() == ee.Projection(grid["crs"]).serialize()
    assert json.loads(projected.args["crsTransform"].serialize()) == json.loads(
        ee.List(grid["transform"]).serialize()
    )
    # M004-S19: the AOI selection precedes the fill; the constructor clips to the window rectangle
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    filled = doubled.args["value"]
    assert filled.func.getSignature()["name"] == "Image.unmask"
    selected = filled.args["input"]
    assert selected.func.getSignature()["name"] == "Image.clip"
    assert selected.args["geometry"].serialize() == region.serialize()
    assert '"GeometryConstructors.Rectangle"' in clipped.args["geometry"].serialize()
    return doubled.args["value"].serialize()


@pytest.mark.parametrize(
    "workflow", ["s2_ndvi", "s1_linear_gamma", "yearly", "rao_raw", "rao_gn", "seasonal"]
)
def test_all_science_paths_build_native_drive_tasks(real_ee, monkeypatch, workflow):
    from test_runner_science import BOUNDS, evaluation

    value = drive_recipe(grid=dict(crs="EPSG:32632", transform=[20, 0, 571660, 0, -20, 5539120]))
    answers = [BOUNDS, 1, ["scene_a"]]
    if workflow == "s1_linear_gamma":
        value.update(dataset="sentinel1_float", bands=["VV", "VH"], preprocess="gamma_map")
        value.pop("transform")
    elif workflow == "yearly":
        value.update(workflow="yearly", years=[2022, 2023])
        value.pop("start")
        value.pop("end")
        answers.extend([1, ["scene_b"]])
    elif workflow.startswith("rao"):
        value.update(workflow="rao", bands=["B4", "B8", "B11"], mask="s2_cloud_score_plus")
        value.pop("transform")
        value["rao"] = {"normalization": workflow[4:], "window_size": 3}
        if workflow == "rao_gn":
            stats = {}
            for band in ("B4", "B8", "B11"):
                stats.update({f"{band}_count": 9, f"{band}_mean": 0.5, f"{band}_min": 0, f"{band}_max": 1})
            covariance = {f"c_{i}_{j}": 1.0 if i == j else 0.0 for i in range(3) for j in range(3)}
            answers.extend([9, stats, covariance])
    elif workflow == "seasonal":
        value.update(workflow="seasonal_nirv", seasonal={"window_size": 1, "scene_outputs": False})
        value.pop("transform")
        from datetime import datetime, timezone

        times = [int(datetime(2024, 1, d, tzinfo=timezone.utc).timestamp() * 1000) for d in (1, 2)]
        answers = [BOUNDS, 2, ["scene_0", "scene_1"], 2]
        answers.append([dict(id=f"scene_{i}", time=times[i], spacecraft="S2A", orbit=1) for i in range(2)])
        answers.append({"total": 9})
        for ndvi in (0.1, 0.9):
            answers.extend(
                [
                    {"ndvi_count": 9, "ndvi_median": ndvi},
                    {"nirv_count": 9, "nirv_mean": 0.5, "nirv_min": 0, "nirv_max": 1},
                    {"c_0_0": 2.25},
                    {"rao_gn_count": 9, "rao_gn_median": 0.01},
                ]
            )
    pending = evaluation(monkeypatch, answers)
    monkeypatch.setattr(ee.batch.Task, "start", fail)
    recipe = personal.validate(value)
    prefix = "run-" + "0" * 32
    tasks, provenance = prepare(recipe, prefix, Clock().budget())
    assert pending == []
    assert provenance["dtype"] == "float64" and provenance["nodata"] == -9999
    for task in tasks:
        config = task["config"]
        assert config["fileExportOptions"] == {
            "fileFormat": "GEO_TIFF",
            "driveDestination": {"folder": FOLDER, "filenamePrefix": f"{prefix}_{task['name']}"},
            "geoTiffOptions": {"noData": {"floatValue": -9999}},
        }
        assert config["workloadTag"] == ""
        assert task["fingerprint"] == config_fingerprint(config)
        source = expression_chain(config, recipe)
        # The export reads the frozen scene snapshot, not a fresh catalogue query.
        assert "S2_SR_HARMONIZED/scene_" in source or "S1_GRD_FLOAT/scene_" in source
    names = [t["name"] for t in tasks]
    if workflow.startswith("rao"):
        assert names == ["rao"]
        assert ("calibration_sha256" in provenance) is (workflow == "rao_gn")
    if workflow == "seasonal":
        assert names == ["seasonal_mean", "observation_count"]
    if workflow == "yearly":
        assert names == ["year_2022", "year_2023"]


def test_cli_with_native_drive_graph_persists_before_export(real_ee, tmp_path, monkeypatch, capsys):
    from test_runner_science import BOUNDS, evaluation

    w = Personal(tmp_path)
    pending = evaluation(monkeypatch, [BOUNDS, 1, ["scene_a"]])
    monkeypatch.setattr(ee.data, "newTaskId", lambda: ["native-request"])
    exported = []

    def export(request_id, config):
        state, _ = w.record("native")
        (task,) = state["tasks"]
        assert (task["request_id"], task["state"]) == (request_id, "submitting")
        assert task["fingerprint"] == config_fingerprint(config)
        assert config["description"] == task["marker"]
        assert config["fileExportOptions"]["driveDestination"] == {
            "folder": FOLDER,
            "filenamePrefix": f"{state['prefix']}_composite",
        }
        assert isinstance(config["expression"], ee.Image)
        exported.append(request_id)
        return {"name": f"projects/{PROJECT}/operations/native-operation"}

    monkeypatch.setattr(ee.data, "exportImage", export)
    monkeypatch.setattr(ee.data, "getTaskStatus", lambda op: [{"state": "COMPLETED"}])

    def connector(settings, budget, *, credentials, state):
        engine = personal.PersonalEarthEngine(budget)

        def initialize(creds, project):
            assert type(creds) is Credentials and project == PROJECT
            engine.credentials, engine.project = creds, project

        engine.initialize = initialize
        return Store(state), engine, personal.load_credentials(credentials)

    code = cli.main(
        [*w.options(), "run", "--recipe", str(w.recipe()), "--run-id", "native"],
        connector=connector,
        budget_factory=w.clock.budget,
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "succeeded", out
    assert exported == ["native-request"] and pending == []
    assert out["tasks"][0]["task_id"] == "native-operation"
    assert out["raster_verification"] == "pending"


# ------------------------------------------------- library/API compatibility


def test_export_to_drive_nodata_is_keyword_only_metadata(real_ee):
    params = inspect.signature(export_to_drive).parameters
    assert list(params)[:7] == [
        "image",
        "description",
        "folder",
        "region",
        "scale",
        "file_name_prefix",
        "max_pixels",
    ]
    assert params["no_data"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["no_data"].default is None
    region = ee.Geometry.Rectangle([9, 47, 9.01, 47.01], geodesic=False)
    image = ee.Image.constant(1).toDouble()
    grid = dict(crs="EPSG:32632", crs_transform=[20, 0, 500000, 0, -20, 5300000])
    plain = export_to_drive(image, "plain", FOLDER, region, **grid)
    assert "geoTiffOptions" not in plain.config["fileExportOptions"]
    task = export_to_drive(image, "masked", FOLDER, region, max_pixels=2500, no_data=-9999, **grid)
    assert task.state == ee.batch.Task.State.UNSUBMITTED and task.id is None
    assert task.config["fileExportOptions"]["geoTiffOptions"] == {"noData": {"floatValue": -9999}}
    serialized = task.config["expression"].serialize()
    assert "Image.unmask" not in serialized


@pytest.mark.parametrize("value", [True, "-9999", float("nan"), float("inf"), [1]])
def test_export_to_drive_invalid_nodata_before_aoi_read(monkeypatch, value):
    monkeypatch.setattr(drive_module, "to_ee_geometry", fail)
    monkeypatch.setattr(ee.batch.Export.image, "toDrive", fail)
    with pytest.raises(GeeComposerError):
        export_to_drive(object(), "x", FOLDER, "missing.geojson", scale=10, no_data=value)


def test_public_initialize_signature_unchanged():
    params = inspect.signature(initialize).parameters
    assert list(params) == ["project", "authenticate", "credentials"]
    assert params["project"].default is None and params["authenticate"].default is False
    assert params["credentials"].kind is inspect.Parameter.KEYWORD_ONLY


def test_import_selects_no_mode_and_initializes_nothing(tmp_path):
    code = f"""
import socket, sys
def deny(*a, **k):
    raise RuntimeError("network")
socket.socket.connect = deny
socket.create_connection = deny
socket.getaddrinfo = deny
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
import ee, geecomposer, geecomposer.cli
assert not ee.data.is_initialized()
assert "geecomposer.runner_personal" not in sys.modules
assert "geecomposer.runner" not in sys.modules
print("clean")
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_", "EARTHENGINE_"))}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert result.returncode == 0 and result.stdout.strip() == "clean", result.stderr[-2000:]
    assert list(tmp_path.iterdir()) == []


def test_cli_help_documents_personal_options(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for option in ("--project", "--credentials", "--state-dir", "--policy", "--hosted"):
        assert option in text


# ------------------------------------------- round 2: operation binding (F1)

ORIGINAL = "projects/test-project/operations/server-1"
FOREIGN = "projects/test-project/operations/foreign"


def run_until_first_poll(w, capsys, run="swap"):
    """Submit one export, persist its operation, then die at the first poll."""
    w.engine.products = 1
    crash_at(w, "status.before")
    with pytest.raises(Crash):
        cli.main(
            [*w.options(), "run", "--recipe", str(w.recipe()), "--run-id", run],
            connector=w.connector,
            budget_factory=w.clock.budget,
        )
    capsys.readouterr()
    w.boundary.hook = None
    state, generation = w.record(run)
    assert state["tasks"][0]["operation"] == ORIGINAL
    return state, generation


@pytest.mark.parametrize("command", ["resume", "cancel"])
@pytest.mark.parametrize(
    "evidence,code",
    [("foreign", 5), ("absent", 6), ("ambiguous", 6), ("auth", 3), ("match", None)],
)
def test_recovery_never_uses_an_unconfirmed_saved_operation(
    tmp_path, monkeypatch, capsys, command, evidence, code
):
    w = Personal(tmp_path)
    # Resume polls to completion; cancel must find the export still active.
    w.engine.states["request-1"] = ["RUNNING", "COMPLETED"] if command == "resume" else ["RUNNING"]
    state, generation = run_until_first_poll(w, capsys)
    marker = state["tasks"][0]["marker"]
    if evidence != "match":
        # Only the saved operation changes: recipe, marker and request remain valid.
        w.engine.operations["other-request"] = FOREIGN
        state["tasks"][0]["operation"] = FOREIGN
        Store(w.state).write("swap", state, generation)
    if evidence == "absent":
        w.engine.markers.pop(marker)
    elif evidence in ("ambiguous", "auth"):
        message = {"ambiguous": "ambiguous operation discovery", "auth": "operation listing permission denied"}

        def discover(value):
            w.boundary.event("discover.before")
            raise RunnerError("uncertain" if evidence == "ambiguous" else "auth", message[evidence])

        monkeypatch.setattr(w.engine, "discover", discover)
    record = w.state / "swap.json"
    before = record.read_bytes()
    w.clock.now += LEASE_SECONDS + 1
    w.boundary.trace.clear()
    polled = len(w.engine.polled)
    result, out = w.main(capsys, command, "--run-id", "swap")
    assert w.boundary.trace.count("discover.before") == 1
    if evidence == "match":
        expected = (0, "succeeded") if command == "resume" else (8, "cancelled")
        assert (result, out["status"]) == expected
        assert set(w.engine.polled[polled:]) == {ORIGINAL}
        assert w.engine.cancelled_operations == ([ORIGINAL] if command == "cancel" else [])
        assert out["tasks"][0]["operation_evidence"] == "verified"
    else:
        # Fails before takeover: nothing polled, cancelled, rewritten or resubmitted.
        assert result == code, out
        assert w.engine.polled[polled:] == [] and w.engine.cancelled_operations == []
        assert record.read_bytes() == before
    assert FOREIGN not in w.engine.polled + w.engine.cancelled_operations
    assert w.engine.submitted == ["request-1"] and w.engine.allocated == ["request-1"]


def test_cancellation_adopted_after_generation_conflict_rechecks_operation(tmp_path, capsys):
    w = Personal(tmp_path)
    w.engine.products = 1
    w.engine.states["request-1"] = ["RUNNING"]
    w.engine.operations["other-request"] = FOREIGN
    tampered = []

    def tamper(name):
        # A concurrent writer records a stop and a substituted operation while
        # the owner is polling; the owner adopts that newer generation.
        if name == "status.after" and not tampered:
            tampered.append(name)
            state, generation = w.record("adopt")
            state.update(stop="cancelled", status="cancelling")
            state["tasks"][0]["operation"] = FOREIGN
            Store(w.state).write("adopt", state, generation)

    w.boundary.hook = tamper
    code, out = w.main(capsys, "run", "--recipe", str(w.recipe()), "--run-id", "adopt")
    assert tampered
    # Round 3: the stop is merged into trusted worker state, so the substituted
    # name never replaces the confirmed original, which is then cancelled.
    assert (code, out["status"], out["remote_pending"]) == (8, "cancelled", False)
    assert FOREIGN not in w.engine.polled + w.engine.cancelled_operations
    assert w.engine.cancelled_operations == [ORIGINAL]
    assert w.engine.submitted == ["request-1"]
    assert out["tasks"][0]["operation_evidence"] == "verified"


# ------------------------------------------ round 2: special run records (F2)


def fake_lstat(monkeypatch, target, result):
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            return result(real)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(personal.os, "lstat", lstat)


def refuse_open_of(monkeypatch, target):
    real = os.open

    def guarded(path, *args, **kwargs):
        assert os.fspath(path) != os.fspath(target), "special record was opened"
        return real(path, *args, **kwargs)

    monkeypatch.setattr(personal.os, "open", guarded)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "character_device", "directory"])
def test_special_records_are_rejected_before_open(tmp_path, monkeypatch, kind):
    """Fake-boundary type check on every platform; real FIFOs are tested separately."""
    import stat

    store = personal.LocalStore(tmp_path)
    record = tmp_path / "run.json"
    if kind == "directory":
        record.mkdir()
    else:
        record.write_bytes(b"x")
        mode = {"fifo": stat.S_IFIFO, "symlink": stat.S_IFLNK, "character_device": stat.S_IFCHR}[kind]
        fake_lstat(monkeypatch, record, lambda real: os.stat_result((mode | 0o600,) + tuple(real(record))[1:]))
    refuse_open_of(monkeypatch, record)
    for action in (lambda: store.read("run"), lambda: store.write("run", {"run_id": "run"}, 0)):
        with pytest.raises(RunnerError) as exc:
            action()
        assert exc.value.status == "uncertain"
    assert record.is_dir() if kind == "directory" else record.read_bytes() == b"x"


def test_record_replaced_between_check_and_open_is_rejected(tmp_path, monkeypatch):
    store = personal.LocalStore(tmp_path)
    store.write("run", {"run_id": "run"}, 0)
    other = tmp_path / "other.bin"
    other.write_bytes(b"regular but different")
    before = (tmp_path / "run.json").read_bytes()
    # The pre-open check saw one regular file; the opened descriptor is another.
    fake_lstat(monkeypatch, tmp_path / "run.json", lambda real: real(other))
    for action in (lambda: store.read("run"), lambda: store.write("run", {"run_id": "run"}, 1)):
        with pytest.raises(RunnerError) as exc:
            action()
        assert exc.value.status == "uncertain"
    assert (tmp_path / "run.json").read_bytes() == before
    monkeypatch.undo()
    assert store.read("run") == ({"run_id": "run"}, 1)


def test_special_lock_file_is_rejected(tmp_path):
    store = personal.LocalStore(tmp_path)
    (tmp_path / "run.lock").mkdir()
    with pytest.raises(RunnerError) as exc:
        store.write("run", {"run_id": "run"}, 0)
    assert exc.value.status == "uncertain"
    assert not (tmp_path / "run.json").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs are unsupported on this platform")
def test_posix_fifo_records_never_block(tmp_path):
    """Real no-writer FIFOs in a child process with a hard timeout, so a regression
    fails instead of hanging the suite. Includes a lstat-then-FIFO replacement race."""
    code = f"""
import json, os, sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
from geecomposer import runner_personal as personal
from geecomposer.recipe import RunnerError
root = sys.argv[1]
store = personal.LocalStore(root)
results = {{}}
def attempt(name, action):
    try:
        action()
        results[name] = "accepted"
    except RunnerError as exc:
        results[name] = exc.status
fifo = os.path.join(root, "fifo.json")
os.mkfifo(fifo)
attempt("read", lambda: store.read("fifo"))
attempt("write", lambda: store.write("fifo", {{"run_id": "fifo"}}, 0))
os.mkfifo(os.path.join(root, "locked.lock"))
attempt("lock", lambda: store.write("locked", {{"run_id": "locked"}}, 0))
store.write("real", {{"run_id": "real"}}, 0)
os.symlink(os.path.join(root, "real.json"), os.path.join(root, "link.json"))
attempt("symlink", lambda: store.read("link"))
real_lstat = os.lstat
os.lstat = lambda p, *a, **k: real_lstat(os.path.join(root, "real.json")) if os.fspath(p) == fifo else real_lstat(p, *a, **k)
attempt("race", lambda: store.read("fifo"))
os.lstat = real_lstat
results["still_fifo"] = __import__("stat").S_ISFIFO(os.lstat(fifo).st_mode)
print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout) == dict(
        read="uncertain",
        write="uncertain",
        lock="uncertain",
        symlink="uncertain",
        race="uncertain",
        still_fifo=True,
    )


# --------------- round-2 continuation: stop adopted by an error-path write


class Denied(Exception):
    code = 403


@pytest.mark.parametrize(
    "cleanup,code,status",
    [
        ("completed", 8, "cancelled"),
        ("status_unavailable", 6, "uncertain"),
        ("cancel_unavailable", 6, "uncertain"),
        ("auth", 3, "auth"),
        ("repeated_conflict", 6, "uncertain"),
        ("substituted", 8, "cancelled"),
    ],
)
def test_stop_adopted_by_error_path_write_is_cleaned_up_or_reported_honestly(
    tmp_path, monkeypatch, capsys, cleanup, code, status
):
    """The status poll fails while a concurrent stop request is written. The
    error handler's final release write adopts that request, which is not
    remote evidence: bounded cleanup runs once, or the result stays honest."""
    w = Personal(tmp_path)
    w.engine.products = 1
    w.engine.states["request-1"] = ["RUNNING"]
    w.engine.operations["other-request"] = FOREIGN
    failures, events = [], []
    real_status = w.engine.status

    def flaky_status(request_id, operation=None):
        if failures:
            w.engine.polled.append(operation)
            raise failures.pop(0)
        return real_status(request_id, operation)

    monkeypatch.setattr(w.engine, "status", flaky_status)
    if cleanup == "cancel_unavailable":

        def cancel(request_id, operation=None):
            w.engine.cancelled_operations.append(operation)
            raise TimeoutError("PRIVATE cancel response lost")

        monkeypatch.setattr(w.engine, "cancel", cancel)

    def request_stop(operation=None):
        state, generation = w.record("halt")
        state.update(stop="cancelled", status="cancelling")
        if operation:
            state["tasks"][0]["operation"] = operation
        Store(w.state).write("halt", state, generation)

    def hook(name):
        if name == "status.before" and not events:
            events.append("stop")
            request_stop(FOREIGN if cleanup == "substituted" else None)
            # One transient failure, then a non-transient one: an uncertain
            # RunnerError reaches _execute's handler before any heartbeat
            # write could adopt the stop (retry pauses stay under the lease).
            w.engine.fail_status = 1
            failures.append(ValueError("PRIVATE"))
            if cleanup == "status_unavailable":
                failures.append(ValueError("PRIVATE"))
            elif cleanup == "auth":
                failures.append(Denied("PRIVATE"))
        elif cleanup == "repeated_conflict" and name == "cancel.after" and len(events) == 1:
            events.append("second stop")
            request_stop()

    w.boundary.hook = hook
    released = []
    real_release = Runner._release_with

    def release_with(self, value):
        released.append(value)
        return real_release(self, value)

    monkeypatch.setattr(Runner, "_release_with", release_with)
    result, out = w.main(capsys, "run", "--recipe", str(w.recipe()), "--run-id", "halt")
    assert events and (result, out["status"]) == (code, status), out
    # The stop was adopted by the error handler's own release write.
    assert released == ["uncertain"]
    # Never the old bare {"status": "cancelled", "message": ...} without cleanup.
    assert out["tasks"][0]["request_id"] == "request-1"
    assert out["remote_pending"] is (cleanup not in ("completed", "substituted"))
    assert "PRIVATE" not in json.dumps(out)
    attempted = {
        "completed": [ORIGINAL],
        "cancel_unavailable": [ORIGINAL],
        "repeated_conflict": [ORIGINAL],
        # Round 3: an operation-only substitution in the stop record is ignored.
        "substituted": [ORIGINAL],
    }
    assert w.engine.cancelled_operations == attempted.get(cleanup, [])
    assert FOREIGN not in w.engine.polled + w.engine.cancelled_operations
    assert w.engine.submitted == ["request-1"] and w.engine.allocated == ["request-1"]
    state, _ = w.record("halt")
    assert state["stop"] == "cancelled"
    if cleanup == "repeated_conflict":
        # Cleanup stopped at the second adoption without recursing; the durable
        # stop remains owned until the lease expires, then plain cancel finishes.
        assert w.boundary.trace.count("cancel.before") == 1
        assert state["status"] == "cancelling" and state["owner"]
        w.clock.now += LEASE_SECONDS + 1
        again, final = w.main(capsys, "cancel", "--run-id", "halt")
        assert (again, final["status"], final["remote_pending"]) == (8, "cancelled", False)
        assert w.engine.cancelled_operations == [ORIGINAL]
    else:
        assert state["owner"] is None and state["status"] == status


# ------------- round 3: adopted cancellation must keep trusted submissions (F1)

FOREIGN_MARKER = "gc-" + "f" * 64
SECOND = "projects/test-project/operations/server-2"


def substitute(state, change):
    """Apply one change to a copied record; binding/marker math as production."""
    from geecomposer.runner_science import discovery_marker

    first = state["tasks"][0]
    if change == "marker_and_operation":
        first.update(marker=FOREIGN_MARKER, operation=FOREIGN)
    elif change == "self_consistent":
        first.update(
            request_id="other-request",
            name="product_x",
            marker=discovery_marker("other-request", state["binding"], "product_x"),
            operation=FOREIGN,
        )
    elif change == "fingerprint":
        first["fingerprint"] = "0" * 64
    elif change == "added":
        state["tasks"].append(
            dict(first, request_id="other-request", name="product_x", marker=FOREIGN_MARKER, operation=FOREIGN)
        )
    elif change == "removed":
        state["tasks"].pop()
    elif change == "duplicate":
        state["tasks"].append(dict(first))
    elif change == "cached_operation_changed_marker":
        first["marker"] = FOREIGN_MARKER
    elif change == "prefix":
        state["prefix"] = state["prefix"][:-1] + ("0" if state["prefix"][-1] != "0" else "1")
    elif change == "binding":
        state["binding"] = "0" * 64
    elif change == "operation_only":
        first["operation"] = FOREIGN
    else:
        assert change == "stop_only"


NEGATIVE = [
    "marker_and_operation",
    "self_consistent",
    "fingerprint",
    "added",
    "removed",
    "duplicate",
    "cached_operation_changed_marker",
    "prefix",
    "binding",
]


@pytest.mark.parametrize("path", ["progress", "error_handler"])
@pytest.mark.parametrize("change", NEGATIVE + ["stop_only", "operation_only"])
def test_adopted_cancellation_never_replaces_trusted_submissions(
    tmp_path, monkeypatch, capsys, path, change
):
    """A stop request plus a substituted identity reaches the worker through a
    CAS conflict, during ordinary progress or in the error handler's release
    write. The fake discovery service treats FOREIGN_MARKER as a genuine,
    accessible export, so trusting an adopted marker would be observable."""
    w = Personal(tmp_path)
    w.engine.products = 2
    w.engine.states.update({"request-1": ["RUNNING"], "request-2": ["RUNNING"], "other-request": ["RUNNING"]})
    w.engine.operations["other-request"] = FOREIGN
    w.engine.markers[FOREIGN_MARKER] = FOREIGN
    failures, tampered = [], []
    real_status = w.engine.status

    def flaky_status(request_id, operation=None):
        if failures:
            w.engine.polled.append(operation)
            raise failures.pop(0)
        return real_status(request_id, operation)

    monkeypatch.setattr(w.engine, "status", flaky_status)

    def hook(name):
        point = "status.after" if path == "progress" else "status.before"
        if name == point and not tampered:
            state, generation = w.record("swap")
            state.update(stop="cancelled", status="cancelling")
            substitute(state, change)
            Store(w.state).write("swap", state, generation)
            tampered.append((w.state / "swap.json").read_bytes())
            if path == "error_handler":
                # Non-transient poll failure: the handler's release write conflicts.
                w.engine.fail_status = 1
                failures.append(ValueError("PRIVATE"))

    w.boundary.hook = hook
    code, out = w.main(capsys, "run", "--recipe", str(w.recipe()), "--run-id", "swap")
    assert tampered, "tamper point not reached"
    assert FOREIGN_MARKER not in w.engine.discovered
    assert FOREIGN not in w.engine.polled + w.engine.cancelled_operations
    assert w.engine.submitted == ["request-1", "request-2"]
    assert w.engine.allocated == ["request-1", "request-2"]
    if change in NEGATIVE:
        # Rejected before use: not adopted, not overwritten, no cleanup claim.
        assert (code, out["status"]) == (5, "conflict"), out
        assert "identity" in out["message"]
        assert (w.state / "swap.json").read_bytes() == tampered[0]
        assert w.engine.cancelled_operations == []
    else:
        # A legitimate stop is merged into trusted worker state and cleaned up.
        assert (code, out["status"], out["remote_pending"]) == (8, "cancelled", False), out
        assert sorted(w.engine.cancelled_operations) == [ORIGINAL, SECOND]
        assert w.engine.discovered == []  # operations were already confirmed locally
        state, _ = w.record("swap")
        assert [t["operation"] for t in state["tasks"]] == [ORIGINAL, SECOND]
        assert state["stop"] == "cancelled" and state["owner"] is None


def test_stop_racing_an_unpersisted_submit_response_keeps_local_evidence(tmp_path, capsys):
    """The stop lands after EE answered exportImage but before the worker saved
    that operation: the merge keeps the locally verified operation and cancels it."""
    w = Personal(tmp_path)
    w.engine.products = 1
    w.engine.states["request-1"] = ["RUNNING"]
    tampered = []

    def hook(name):
        if name == "submit.after" and not tampered:
            state, generation = w.record("race")
            assert state["tasks"][0]["state"] == "submitting" and "operation" not in state["tasks"][0]
            state.update(stop="cancelled", status="cancelling")
            Store(w.state).write("race", state, generation)
            tampered.append(name)

    w.boundary.hook = hook
    code, out = w.main(capsys, "run", "--recipe", str(w.recipe()), "--run-id", "race")
    assert tampered and (code, out["status"]) == (8, "cancelled"), out
    assert w.engine.cancelled_operations == [ORIGINAL] and w.engine.discovered == []
    assert out["tasks"][0]["operation_evidence"] == "verified"
    assert w.engine.submitted == ["request-1"]


@pytest.mark.parametrize("change", ["marker_and_operation", "self_consistent", "removed"])
def test_identity_substitution_during_cleanup_is_rejected(tmp_path, capsys, change):
    """A legitimate stop starts cleanup; a second record with substituted
    identities then conflicts with a cleanup write. The rejection is kept
    (not converted to 'uncertain' or 'cancelled') and the record is not replaced."""
    w = Personal(tmp_path)
    w.engine.products = 2
    w.engine.states.update({"request-1": ["RUNNING"], "request-2": ["RUNNING"], "other-request": ["RUNNING"]})
    w.engine.operations["other-request"] = FOREIGN
    w.engine.markers[FOREIGN_MARKER] = FOREIGN
    events, suspect = [], []

    def hook(name):
        if name == "status.after" and not events:
            events.append("stop")
            state, generation = w.record("clean")
            state.update(stop="cancelled", status="cancelling")
            Store(w.state).write("clean", state, generation)
        elif name == "cancel.after" and events == ["stop"]:
            events.append("substituted")
            state, generation = w.record("clean")
            substitute(state, change)
            Store(w.state).write("clean", state, generation)
            suspect.append((w.state / "clean.json").read_bytes())

    w.boundary.hook = hook
    code, out = w.main(capsys, "run", "--recipe", str(w.recipe()), "--run-id", "clean")
    assert events == ["stop", "substituted"]
    assert (code, out["status"]) == (5, "conflict") and "identity" in out["message"], out
    assert (w.state / "clean.json").read_bytes() == suspect[0]
    assert w.engine.cancelled_operations == [ORIGINAL]  # stopped at the rejection
    assert FOREIGN_MARKER not in w.engine.discovered
    assert FOREIGN not in w.engine.polled + w.engine.cancelled_operations
    assert w.engine.submitted == ["request-1", "request-2"]
