"""Self-contained public CLI contract: the real personal CLI over declared service fakes.

Product code exercised unchanged: strict recipe validation, CLI dispatch, the
default personal connector, saved-login loading, PersonalEarthEngine.initialize,
PersonalRunner, LocalStore files, runner_science.prepare (scene snapshot, GN
calibration, real EE graph construction) and the real unstarted Drive task
constructor. Only the boundaries in REPLACED are faked, with deterministic
responses; no task is started and no socket is opened.
"""

import copy
import json
from pathlib import Path

import ee
from ee import apitestcase, oauth

from geecomposer import cli, runner
from geecomposer import runner_personal as personal
from geecomposer.recipe import RunnerError, digest
from geecomposer.runner_science import discovery_marker
from runner_fakes import Clock

FIXTURES = Path(__file__).with_name("fixtures")
PROJECT = "contract-project"
RUN_ID = "s14-contract-run"
# Secret-shaped text placed in every injected failure; it must never reach output or state.
SECRET = "S14SECRET https://leak.invalid/v1?access_token=ya29.S14SECRET /home/user/.config/creds"
REQUEST_ID = "S14REQUEST0000000000000001"
REPLACED = (
    "ee.data._install_cloud_api_resource (no discovery document fetch)",
    "ee.data.getAlgorithms (installed apitestcase algorithm table)",
    "ee.deprecation._FetchDataCatalogStac",
    "ee.data.computeValue (scripted STEPS responses)",
    "ee.data.newTaskId",
    "ee.data.exportImage (records the unstarted config; no task)",
    "ee.data.getTaskStatus",
    "geecomposer.runner_cloud._read_url (OAuth refresh and operation listing)",
    "Budget clock (runner_fakes.Clock)",
)
STEPS = (
    "bounds",
    "scene_count",
    "scene_ids",
    "calibration_count",
    "calibration_summary",
    "calibration_covariance",
)
STATE_REQUIRED = frozenset(
    {"schema", "run_id", "recipe", "source", "recipe_sha256", "binding", "policy_sha256",
     "package_version", "project", "created", "updated", "deadline", "owner", "lease_until",
     "status", "tasks", "stop", "mode", "prefix"}
)
STATE_OPTIONAL = frozenset({"remote_pending", "plan", "provenance", "error"})
TASK_REQUIRED = frozenset({"name", "marker", "fingerprint", "request_id", "state"})


def rao_recipe():
    """The public personal Rao GN example (copied fixture), as raw JSON."""
    return json.loads((FIXTURES / "s14_personal_rao_gn.json").read_text(encoding="utf-8"))


def service_steps():
    return json.loads((FIXTURES / "s14_rao_gn_service.json").read_text(encoding="utf-8"))["steps"]


class Service:
    """Deterministic EE/OAuth service. `faults` maps a boundary name (a STEPS entry,
    "initialize", "new_id", "export", "status") to the exception it raises; "status"
    raises on every call. `listing` is what operation discovery returns."""

    def __init__(self, monkeypatch, steps=None, faults=None, states=("COMPLETED",), project=PROJECT):
        self.project = project
        self.steps = dict(service_steps() if steps is None else steps)
        self.faults = dict(faults or {})
        self.states = list(states)
        self.evaluated, self.exports, self.polled, self.http = [], [], [], []
        self.listing = []
        ee.Reset()
        monkeypatch.setattr(ee.data, "_install_cloud_api_resource", lambda: None)
        monkeypatch.setattr(ee.data, "getAlgorithms", self.algorithms)
        monkeypatch.setattr(ee.deprecation, "_FetchDataCatalogStac", lambda: {})
        monkeypatch.setattr(ee.data, "computeValue", self.compute)
        monkeypatch.setattr(ee.data, "newTaskId", self.new_id)
        monkeypatch.setattr(ee.data, "exportImage", self.export)
        monkeypatch.setattr(ee.data, "getTaskStatus", self.status)
        monkeypatch.setattr("geecomposer.runner_cloud._read_url", self.read_url)

    def _fault(self, name, keep=False):
        if name in self.faults:
            raise (self.faults[name] if keep else self.faults.pop(name))

    def algorithms(self):
        self._fault("initialize")
        return apitestcase.GetAlgorithms()

    def compute(self, *args, **kwargs):
        step = STEPS[len(self.evaluated)]
        self.evaluated.append(step)
        self._fault(step)
        return copy.deepcopy(self.steps[step])

    def new_id(self, count=1):
        self._fault("new_id")
        return [REQUEST_ID]

    def export(self, request_id, config):
        self.exports.append((request_id, config))
        operation = f"projects/{self.project}/operations/S14OPERATION1"
        self._fault("export")  # after acceptance: the response, not the request, is lost
        return {"name": operation}

    def status(self, task_id):
        self.polled.append(task_id)
        self._fault("status", keep=True)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return [{"state": state, "error_message": SECRET}]

    def read_url(self, url, budget, *, headers=None, method="GET", body=None, limit=16384,
                 identity=False):
        self.http.append(url.split("?")[0])
        if url == oauth.TOKEN_URI:
            data = {"access_token": "S14ACCESS", "expires_in": 3600, "token_type": "Bearer"}
        else:
            data = {"operations": self.listing}
        return 200, {"content-type": "application/json"}, json.dumps(data).encode()


def write_inputs(directory, recipe_bytes):
    directory.mkdir(parents=True, exist_ok=True)
    secrets = directory / "secrets"
    secrets.mkdir(exist_ok=True)
    credential = secrets / "credentials"
    credential.write_text(json.dumps({"refresh_token": "S14SECRET-REFRESH", "scopes": oauth.SCOPES}))
    recipe = directory / "recipe.json"
    recipe.write_bytes(recipe_bytes)
    return credential, recipe


def run_cli(directory, capsys, recipe_bytes, *, project=PROJECT, run_id=RUN_ID, command="run",
            clock=None):
    """One real `geecomposer` personal invocation (default connector); returns
    (exit, stdout text, record bytes or None)."""
    credential, recipe = write_inputs(directory, recipe_bytes)
    state = directory / "state"
    argv = ["--project", project, "--credentials", str(credential), "--state-dir", str(state)]
    argv += ["run", "--recipe", str(recipe), "--run-id", run_id] if command == "run" else [
        command, "--run-id", run_id]
    code = cli.main(argv, budget_factory=(clock or Clock()).budget)
    out = capsys.readouterr().out
    path = state / f"{run_id}.json"
    return code, out, path.read_bytes() if path.exists() else None


def contract(data, raw_recipe, *, project=PROJECT, run_id=RUN_ID):
    """Independent record contract: the EXPECTED recipe is the caller's raw recipe
    normalized through the same public validation the personal CLI applies; the
    record is never its own authority. Returns (state, sorted problem names)."""
    record = json.loads(data)
    expected = personal.validate(copy.deepcopy(raw_recipe))
    policy = personal.settings(project)
    source = {"kind": "local", "sha256": digest(expected)}
    s = record.get("state") if isinstance(record, dict) else None
    if not (isinstance(record, dict) and record.keys() == {"format", "generation", "state"}
            and record["format"] == personal.STATE_FORMAT and isinstance(s, dict)):
        return None, ["envelope"]
    checks = dict(
        keys=STATE_REQUIRED <= s.keys() <= STATE_REQUIRED | STATE_OPTIONAL,
        run_id=s.get("run_id") == run_id,
        mode=s.get("mode") == "personal",
        project=s.get("project") == project,
        recipe=s.get("recipe") == expected,
        recipe_sha256=s.get("recipe_sha256") == digest(expected),
        source=s.get("source") == source,
        policy=s.get("policy_sha256") == digest(policy),
        binding=s.get("binding") == runner.binding(expected, policy, source),
        identity=runner.identity(s) is not None,
    )
    tasks = s.get("tasks")
    checks["tasks"] = isinstance(tasks, list) and len(tasks) <= expected["limits"]["outputs"]
    plan = {p.get("name"): p.get("fingerprint") for p in s.get("plan") or [] if isinstance(p, dict)}
    for task in tasks if checks["tasks"] else []:
        checks["tasks"] = checks["tasks"] and isinstance(task, dict) and (
            TASK_REQUIRED <= task.keys() <= TASK_REQUIRED | {"operation"})
        if checks["tasks"]:
            checks["marker"] = checks.get("marker", True) and task["marker"] == discovery_marker(
                task["request_id"], s.get("binding"), task["name"])
            # The plan binds the prepared config, the task its marker-bound request
            # config: both are present digests, never equal (the marker differs).
            checks["fingerprint"] = checks.get("fingerprint", True) and all(
                type(v) is str and len(v) == 64 for v in (plan.get(task["name"]), task["fingerprint"])
            ) and plan[task["name"]] != task["fingerprint"]
    return s, sorted(k for k, v in checks.items() if not v)


def require_contract(data, raw_recipe, **kw):
    state, problems = contract(data, raw_recipe, **kw)
    if problems:
        raise RunnerError("conflict", f"record contract: {problems}")
    return state
