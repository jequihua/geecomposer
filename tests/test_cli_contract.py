"""M004-S14: the real personal CLI-to-record contract and preparation diagnostics.

Every test drives `geecomposer.cli.main` with the default personal connector and
the public Rao GN recipe; only the service/clock boundaries in cli_contract.REPLACED
are faked. No record is hand-written as positive evidence.
"""

import copy
import json
import os

import ee
import pytest

import cli_contract as C
from geecomposer import runner_personal as personal
from geecomposer.recipe import CEILINGS, canonical, validate_recipe
from geecomposer.runner_science import config_fingerprint

pytestmark = pytest.mark.usefixtures("runner_offline")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(home))
    yield
    ee.Reset()


def raw_bytes(value=None):
    return canonical(C.rao_recipe() if value is None else value)


def test_validation_inserts_defaults_into_the_execution_recipe():
    raw = C.rao_recipe()
    normalized = validate_recipe(copy.deepcopy(raw))
    assert set(raw["limits"]) < set(normalized["limits"]) == set(CEILINGS)
    assert {k: normalized["limits"][k] for k in raw["limits"]} == raw["limits"]
    assert {k: v for k, v in normalized.items() if k != "limits"} == {
        k: v for k, v in raw.items() if k != "limits"}
    assert validate_recipe(copy.deepcopy(normalized)) == normalized  # idempotent


def test_real_cli_success_record_satisfies_the_canonical_contract(tmp_path, monkeypatch, capsys):
    service = C.Service(monkeypatch)
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes())
    body = json.loads(out)
    assert code == 0 and body["status"] == "succeeded", body
    assert service.evaluated == list(C.STEPS) and service.http == []
    state, problems = C.contract(data, C.rao_recipe())
    assert problems == []
    # The stored execution recipe is the normalized one, never the raw plan bytes.
    assert state["recipe"] != C.rao_recipe() and state.get("error") is None
    assert state["recipe_sha256"] != C.digest(C.rao_recipe())
    ((request_id, config),) = service.exports
    (task,) = state["tasks"]
    assert (task["request_id"], task["state"], task["name"]) == (C.REQUEST_ID, "COMPLETED", "rao")
    assert config["description"] == task["marker"]
    assert config_fingerprint(config) == task["fingerprint"]
    # The plan binds the prepared (pre-marker) config; equality with the task never holds.
    assert config_fingerprint(dict(config, description="rao")) == state["plan"][0]["fingerprint"]
    assert config["fileExportOptions"]["driveDestination"]["filenamePrefix"] == state["prefix"] + "_rao"
    assert state["provenance"]["sources"]["all"] == sorted(C.service_steps()["scene_ids"])
    assert "calibration_sha256" in state["provenance"]
    assert body["raster_verification"] == "pending" and body["error"] is None
    assert body["tasks"][0]["operation_evidence"] == "verified"
    assert body["tasks"][0]["file_name_prefix"] == state["prefix"] + "_rao"


@pytest.mark.parametrize("variant", ["explicit_defaults", "override"])
def test_default_variants_and_legitimate_overrides(tmp_path, monkeypatch, capsys, variant):
    raw = C.rao_recipe()
    if variant == "explicit_defaults":
        raw["limits"] = validate_recipe(copy.deepcopy(raw))["limits"]
    else:
        raw["limits"]["scenes"] = 50
    C.Service(monkeypatch)
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes(raw))
    assert code == 0, out
    assert C.contract(data, raw)[1] == []
    expected = [] if variant == "explicit_defaults" else ["binding", "recipe", "recipe_sha256", "source"]
    assert C.contract(data, C.rao_recipe())[1] == expected


TAMPER = {
    "science": (lambda s: s["recipe"].update(reducer="mean"), ["recipe"]),
    "limit": (lambda s: s["recipe"]["limits"].update(pixels=19999), ["recipe"]),
    "recipe_digest": (lambda s: s.update(recipe_sha256="0" * 64), ["recipe_sha256"]),
    "unexpected_field": (lambda s: s.update(extra=1), ["keys"]),
    "project": (lambda s: s.update(project="other-project"), ["project"]),
    "source": (lambda s: s.update(source={"kind": "local", "sha256": "0" * 64}), ["source"]),
    "binding": (lambda s: s.update(binding="0" * 64), ["binding", "marker"]),
    "request": (lambda s: s["tasks"][0].update(request_id="OTHER"), ["marker"]),
    "marker": (lambda s: s["tasks"][0].update(marker="gc-" + "0" * 64), ["marker"]),
    # A swapped well-formed digest needs the graph rebuild (integration rehearsal).
    "fingerprint_plan_copy": (lambda s: s["tasks"][0].update(fingerprint=s["plan"][0]["fingerprint"]),
                              ["fingerprint"]),
    "fingerprint_shape": (lambda s: s["tasks"][0].update(fingerprint="0" * 63), ["fingerprint"]),
    "plan_missing": (lambda s: s.pop("plan"), ["fingerprint"]),
    "task_field": (lambda s: s["tasks"][0].update(note="x"), ["tasks"]),
}


def test_tampered_product_records_are_rejected(tmp_path, monkeypatch, capsys):
    C.Service(monkeypatch)
    code, _, data = C.run_cli(tmp_path, capsys, raw_bytes())
    assert code == 0
    for name, (change, expected) in TAMPER.items():
        record = json.loads(data)
        change(record["state"])
        assert C.contract(json.dumps(record).encode(), C.rao_recipe())[1] == expected, name


def replace_fails_on_submitting(monkeypatch):
    """Filesystem boundary: the atomic replace of the pre-submission intent fails."""
    real = os.replace

    def replace(source, target, *a, **k):
        with open(source, "rb") as stream:
            if b'"state":"submitting"' in stream.read():
                raise OSError(C.SECRET)
        return real(source, target, *a, **k)

    monkeypatch.setattr(personal.os, "replace", replace)


def graph_fails(monkeypatch):
    def refuse(*a, **k):
        raise ValueError(C.SECRET)

    monkeypatch.setattr(ee.batch.Export.image, "toDrive", refuse)


#   name: (faults, step overrides, extra setup, exit, status, phase, reason, exports, remote_pending)
FAILURES = {
    "initialization": (dict(initialize=TimeoutError(C.SECRET)), {}, None, 6, "uncertain", "initialize",
                       "uncertain: EE initialization response unavailable", 0, False),
    "bounds": (dict(bounds=ee.EEException(C.SECRET)), {}, None, 6, "uncertain", "bounds",
               "unexpected service error", 0, False),
    "scene_enumeration": (dict(scene_ids=TimeoutError(C.SECRET)), {}, None, 6, "uncertain", "scenes",
                          "unexpected timeout", 0, False),
    "no_scenes": ({}, dict(scene_count=0), None, 4, "no_data", "scenes", "no_data: no input scenes", 0, False),
    "calibration": (dict(calibration_summary=ConnectionError(C.SECRET)), {}, None, 6, "uncertain",
                    "calibration", "unexpected transport or system error", 0, False),
    "calibration_samples": ({}, dict(calibration_count=1), None, 4, "no_data", "calibration",
                            "no_data: insufficient calibration samples", 0, False),
    "config_creation": ({}, {}, graph_fails, 6, "uncertain", "graph", "unexpected internal error", 0, False),
    "request_id": (dict(new_id=TimeoutError(C.SECRET)), {}, None, 6, "uncertain", "allocate",
                   "unexpected timeout", 0, False),
    "submit_ambiguity": (dict(export=TimeoutError(C.SECRET)), {}, None, 6, "uncertain", "submit",
                         "uncertain: submission outcome unknown", 1, True),
    "polling": (dict(status=TimeoutError(C.SECRET)), {}, None, 6, "uncertain", "poll",
                "uncertain: EE status reconciliation unavailable", 1, True),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_preparation_and_submission_failures_are_diagnosable(tmp_path, monkeypatch, capsys, name):
    faults, overrides, setup, exit_code, status, phase, reason, exports, pending = FAILURES[name]
    steps = dict(C.service_steps(), **overrides)
    service = C.Service(monkeypatch, steps=steps, faults=faults)
    if setup:
        setup(monkeypatch)
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes())
    body = json.loads(out)
    assert (code, body["status"]) == (exit_code, status), body
    assert body["error"] == dict(phase=phase, reason=reason, persisted=True)
    assert len(service.exports) == exports and body["remote_pending"] is pending
    assert "S14SECRET" not in out and b"S14SECRET" not in data and len(out) < 4096
    state, problems = C.contract(data, C.rao_recipe())
    assert problems == [] and state["error"] == dict(phase=phase, reason=reason)
    assert state["status"] == status and state["owner"] is None
    if phase in ("initialize", "bounds", "scenes", "calibration", "graph"):
        assert state["tasks"] == [] and "plan" not in state  # stopped before dependent stages
    # Offline status reports the persisted diagnosis unchanged; no credentials are read.
    code, out, _ = C.run_cli(tmp_path, capsys, raw_bytes(), command="status")
    assert json.loads(out)["error"] == dict(phase=phase, reason=reason, persisted=True)


def test_pre_submit_write_failure_is_reported_but_not_claimed_persisted(tmp_path, monkeypatch, capsys):
    service = C.Service(monkeypatch)
    replace_fails_on_submitting(monkeypatch)
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes())
    body = json.loads(out)
    assert code == 6 and body["status"] == "uncertain", body
    assert body["message"] == "local state write failed; inspect the state directory"
    reason = "uncertain: local state write failed; inspect the state directory"
    assert body["error"] == dict(phase="persist", reason=reason, persisted=False)
    assert service.exports == [] and "S14SECRET" not in out
    state, problems = C.contract(data, C.rao_recipe())
    assert problems == [] and [t["state"] for t in state["tasks"]] == ["prepared"]
    assert "error" not in state or state["error"] is None


def test_resume_clears_a_stale_diagnosis_only_through_a_new_outcome(tmp_path, monkeypatch, capsys):
    C.Service(monkeypatch, faults=dict(status=TimeoutError(C.SECRET)))
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes())
    assert code == 6 and json.loads(out)["error"]["phase"] == "poll"
    service = C.Service(monkeypatch)
    service.listing = [dict(name=f"projects/{C.PROJECT}/operations/S14OPERATION1",
                            metadata=dict(description=json.loads(data)["state"]["tasks"][0]["marker"],
                                          state="SUCCEEDED"))]
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes(), command="resume")
    body = json.loads(out)
    assert code == 0 and body["status"] == "succeeded" and body["error"] is None, body
    assert service.exports == [] and json.loads(data)["state"].get("error") is None


# Secrets made only of characters any printable-text filter would admit.
LEGAL_SECRET = "ya29AbCdEf0123456789TokenValue /home/alice/.config/earthengine/credentials"


def test_raised_execution_error_never_echoes_unknown_text(tmp_path, monkeypatch, capsys):
    service = C.Service(monkeypatch)
    real = personal.LocalStore.write

    def write(store, identifier, state, generation):
        if any(t["state"] == "submitting" for t in state["tasks"]):
            raise personal.RunnerError("uncertain", LEGAL_SECRET)
        return real(store, identifier, state, generation)

    monkeypatch.setattr(personal.LocalStore, "write", write)
    code, out, _ = C.run_cli(tmp_path, capsys, raw_bytes())
    body = json.loads(out)
    assert code == 6 and body["status"] == "uncertain" and service.exports == []
    assert body["error"] == dict(phase="persist", reason="uncertain", persisted=False)
    assert body["message"] == "execution failed; inspect run status"
    assert "ya29" not in out and "alice" not in out


@pytest.mark.parametrize("edited", [
    {"phase": "scenes", "reason": "uncertain: " + LEGAL_SECRET},
    {"phase": "scenes", "reason": LEGAL_SECRET},
    {"phase": ["scenes"], "reason": "unexpected timeout"},
    {"phase": "scenes", "reason": {"x": 1}},
    {"phase": "scenes", "reason": "unexpected timeout", "extra": LEGAL_SECRET},
    "unexpected timeout " + LEGAL_SECRET,
])
def test_status_of_an_edited_record_fails_safe_without_masking_status(tmp_path, monkeypatch, capsys, edited):
    C.Service(monkeypatch, faults=dict(scene_ids=TimeoutError(C.SECRET)))
    code, out, data = C.run_cli(tmp_path, capsys, raw_bytes())
    assert code == 6
    record = json.loads(data)
    record["state"]["error"] = edited
    (tmp_path / "state" / f"{C.RUN_ID}.json").write_text(json.dumps(record))
    code, out, _ = C.run_cli(tmp_path, capsys, raw_bytes(), command="status")
    body = json.loads(out)
    assert code == 6 and body["status"] == "uncertain"
    assert body["error"] == dict(phase="unknown", reason="unrecognized diagnostic", persisted=True)
    assert "ya29" not in out and "alice" not in out
