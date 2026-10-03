import copy

import pytest

from geecomposer.recipe import RunnerError
from geecomposer.runner import verify_inventory
from runner_fakes import Crash, World

pytestmark = pytest.mark.usefixtures("runner_offline")


def test_success_ordering_identity_and_bounded_parallelism():
    w = World()
    result = w.runner().run(w.recipe, "run1")
    assert result["status"] == "succeeded"
    assert len(result["outputs"]) == 3
    assert len(w.engine.submitted) == 3
    first_gen, first = w.store.writes[0]
    assert first_gen == 0
    assert first["recipe"] == w.recipe
    assert first["tasks"] == []
    for previous, current in zip(w.store.writes, w.store.writes[1:]):
        assert current[0] == previous[0] + 1
    assert "SECRET" not in str(w.store.records)
    assert w.clock.now == 30
    state, _ = w.store.records["run1"]
    assert state["owner"] is None
    assert state["lease_until"] == 0
    assert all(
        task["operation"].rsplit("/", 1)[-1] != task["request_id"] for task in state["tasks"]
    )


def test_duplicate_run_and_changed_recipe_do_not_submit():
    w = World()
    w.runner().run(w.recipe, "same")
    for recipe in (w.recipe, dict(w.recipe, reducer="mean")):
        with pytest.raises(RunnerError) as exc:
            w.runner().run(recipe, "same")
        assert exc.value.status == "conflict"
    assert len(w.engine.submitted) == 3
    assert w.runner().resume("same")["status"] == "succeeded"


def test_lost_submit_response_reconciles_original_id_only():
    w = World()
    w.engine.loss = True
    result = w.runner().run(w.recipe, "lost")
    assert result["status"] == "succeeded"
    assert len(w.engine.allocated) == 3
    assert w.engine.submitted == w.engine.allocated


def _trace_length():
    w = World()
    w.runner().run(w.recipe, "crash")
    return len(w.boundary.trace)


@pytest.mark.parametrize("boundary", range(1, _trace_length() + 1))
def test_crash_before_and_after_every_remote_boundary(boundary):
    w = World()
    w.boundary.fault = boundary
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "crash")
    submitted = list(w.engine.submitted)
    w.clock.sleep(121)
    recovered = w.runner()
    if "crash" in w.store.records:
        result = recovered.resume("crash")
    else:
        result = recovered.run(w.recipe, "crash")
    assert result["status"] in ("succeeded", "uncertain")
    assert len(w.engine.submitted) == len(set(w.engine.submitted))
    assert all(identifier in w.engine.submitted for identifier in submitted)
    if result["status"] == "uncertain":
        state, _ = w.store.records["crash"]
        assert any(task["state"] == "UNKNOWN" for task in state["tasks"])
        allocated = len(w.engine.allocated)
        assert w.runner().resume("crash")["status"] == "uncertain"
        assert len(w.engine.allocated) == allocated


def test_resume_reconciles_before_takeover_and_refuses_live_owner():
    w = World()

    def crash(name):
        if name == "submit.after":
            raise Crash()

    w.boundary.hook = crash
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "owned")
    w.boundary.hook = None
    with pytest.raises(RunnerError, match="another worker"):
        w.runner().resume("owned")
    w.clock.sleep(121)
    w.boundary.trace.clear()
    old_owner = w.store.records["owned"][0]["owner"]
    writes = len(w.store.writes)
    assert w.runner().resume("owned")["status"] == "succeeded"
    assert w.store.writes[writes][1]["owner"] == old_owner
    assert w.store.writes[writes][1]["tasks"][0]["operation"]
    assert w.boundary.trace.index("write.after") < w.boundary.trace.index("status.before")


@pytest.mark.parametrize("accepted", [False, True])
def test_uncertain_state_write_never_falls_through_to_submit(accepted):
    w = World()
    write = w.store.write

    def uncertain(identifier, state, generation):
        if any(t["state"] == "submitting" for t in state["tasks"]):
            if accepted:
                write(identifier, state, generation)
            raise RunnerError("uncertain", "lost state response")
        return write(identifier, state, generation)

    w.store.write = uncertain
    with pytest.raises(RunnerError) as exc:
        w.runner().run(w.recipe, "cas")
    assert exc.value.status == "uncertain"
    assert w.engine.submitted == []


def test_competing_generation_prevents_submission():
    w = World()
    write = w.store.write

    def race(identifier, state, generation):
        if any(t["state"] == "submitting" for t in state["tasks"]):
            old, gen = w.store.records[identifier]
            w.store.records[identifier] = old, gen + 1
        return write(identifier, state, generation)

    w.store.write = race
    with pytest.raises(RunnerError) as exc:
        w.runner().run(w.recipe, "race")
    assert exc.value.status == "conflict"
    assert w.engine.submitted == []


@pytest.mark.parametrize("after_intent", [False, True])
def test_cancel_race_fences_new_submissions(after_intent):
    w = World()
    fired = False

    def cancel(name):
        nonlocal fired
        event = "submit.before" if after_intent else "new_id.after"
        if name == event and not fired:
            fired = True
            assert w.runner().cancel("cancel")["status"] == "cancelling"

    w.boundary.hook = cancel
    result = w.runner().run(w.recipe, "cancel")
    assert result["status"] == "cancelled"
    assert len(w.engine.submitted) == int(after_intent)
    assert not result["remote_pending"]


def test_signal_cancellation_is_bounded_and_preserves_partial_objects():
    w = World()
    runner = w.runner()

    def signal(name):
        if name == "submit.after":
            runner.cancel_requested = True
            w.engine.states["request-1"] = ["RUNNING"]

    w.boundary.hook = signal
    result = runner.run(w.recipe, "signal")
    assert result["status"] == "cancelled"
    assert w.engine.cancel_calls == ["request-1"]
    assert len(w.engine.submitted) == 1


def test_partial_failure_cancels_sibling_and_does_not_start_third():
    w = World()
    w.engine.states = {"request-1": ["FAILED"], "request-2": ["RUNNING"]}
    result = w.runner().run(w.recipe, "failure")
    assert result["status"] == "export"
    assert len(w.engine.submitted) == 2
    assert w.engine.cancel_calls == ["request-2"]


def test_deadline_cancels_and_resume_never_extends_work_deadline():
    w = World()
    w.recipe["limits"]["deadline_seconds"] = 120
    w.engine.states = {"request-1": ["RUNNING"], "request-2": ["RUNNING"]}
    result = w.runner().run(w.recipe, "deadline")
    assert result["status"] == "deadline"
    assert w.clock.now <= 120
    assert len(w.engine.cancel_calls) == 2
    assert len(w.engine.submitted) == 2
    assert w.runner().resume("deadline")["status"] == "deadline"
    assert len(w.engine.submitted) == 2


@pytest.mark.parametrize("failures,status", [(3, "succeeded"), (4, "uncertain")])
def test_exact_transient_status_retry_budget(failures, status):
    w = World()
    w.engine.fail_status = failures
    assert w.runner().run(w.recipe, "retries")["status"] == status
    assert w.engine.fail_status == 0
    assert w.clock.now <= 180


def test_no_data_does_not_allocate_or_submit():
    w = World()
    w.engine.preparation_error = RunnerError("no_data", "empty collection")
    assert w.runner().run(w.recipe, "empty")["status"] == "no_data"
    assert w.engine.allocated == []
    assert w.engine.submitted == []


@pytest.mark.parametrize(
    "mutation", ["missing", "zero", "generation", "checksum", "foreign", "duplicate"]
)
def test_no_premature_success_for_invalid_artifacts(mutation):
    w = World()
    result = w.runner().run(w.recipe, "inventory")
    state, _ = w.store.records["inventory"]
    inventory = copy.deepcopy(state["inventory"])
    if mutation == "missing":
        inventory.pop()
    elif mutation == "zero":
        inventory[0]["size"] = 0
    elif mutation == "generation":
        inventory[0]["generation"] = "None"
    elif mutation == "checksum":
        inventory[0]["crc32c"] = None
    elif mutation == "foreign":
        inventory[0]["name"] += ".extra"
    else:
        inventory.append(inventory[0])
    assert result["status"] == "succeeded"
    with pytest.raises(RunnerError) as exc:
        verify_inventory(state, inventory)
    assert exc.value.status == "artifact"


def test_shards_and_missing_outputs_in_real_runner():
    w = World()
    inventory = w.store.inventory

    def shards(bucket, prefix):
        values = inventory(bucket, prefix)
        first = values.pop(0)
        values.extend(
            dict(first, name=first["name"].replace(".tif", suffix))
            for suffix in ("-0000000000-0000000000.tif", "-0000000000-0000000256.tif")
        )
        return values

    w.store.inventory = shards
    result = w.runner().run(w.recipe, "shards")
    assert result["status"] == "succeeded"
    assert len(result["outputs"]) == 4
    w = World()
    w.store.artifacts = []
    assert w.runner().run(w.recipe, "missing")["status"] == "artifact"


@pytest.mark.parametrize("event", ["new_id.before", "new_id.after", "submit.before"])
def test_changed_native_plan_is_rejected_on_resume(event):
    w = World()

    def crash(name):
        if name == event:
            raise Crash()

    w.boundary.hook = crash
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "changed")
    w.boundary.hook = None
    w.engine.products = 4
    w.clock.sleep(121)
    result = w.runner().resume("changed")
    assert result["status"] == ("uncertain" if event == "submit.before" else "conflict")
    assert w.engine.submitted == []


@pytest.mark.parametrize(
    "event", ["cancel.before", "cancel.after", "inventory.before", "inventory.after"]
)
def test_crash_during_cancellation_preserves_intent_and_recovers(event):
    w = World()
    runner = w.runner()

    def interrupted(name):
        if name == "submit.after":
            runner.cancel_requested = True
            w.engine.states["request-1"] = ["RUNNING"]
        if name == event:
            raise Crash()

    w.boundary.hook = interrupted
    with pytest.raises(Crash):
        runner.run(w.recipe, "stop")
    state, _ = w.store.records["stop"]
    assert state["stop"] == "cancelled"
    assert state["status"] == "cancelling"
    w.boundary.hook = None
    w.clock.sleep(121)
    result = w.runner().cancel("stop")
    assert result["status"] == "cancelled"
    assert len(w.engine.submitted) == 1


def test_unknown_submission_cannot_claim_completed_cancellation():
    w = World()

    def crash(name):
        if name == "submit.before":
            raise Crash()

    w.boundary.hook = crash
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "unknown")
    w.boundary.hook = None
    w.clock.sleep(121)
    result = w.runner().cancel("unknown")
    assert result["status"] == "uncertain"
    assert result["remote_pending"] is True
    assert w.engine.submitted == []


def test_original_policy_is_bound_for_status_and_resume():
    w = World()
    w.runner().run(w.recipe, "policy")
    w.policy["poll_seconds"] = 60
    with pytest.raises(RunnerError, match="policy binding"):
        w.runner().status("policy")
    with pytest.raises(RunnerError, match="policy binding"):
        w.runner().resume("policy")


@pytest.mark.parametrize("failure", ["conflict", "lost_before", "lost_after"])
def test_discovery_save_failure_fences_stale_worker(failure):
    w = World()

    def crash(name):
        if name == "submit.after":
            raise Crash()

    w.boundary.hook = crash
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "discover-cas")
    w.boundary.hook = None
    w.clock.sleep(121)
    original_write = w.store.write

    def write(identifier, state, generation):
        if state["tasks"][0].get("operation"):
            if failure == "lost_after":
                original_write(identifier, state, generation)
            elif failure == "conflict":
                old, gen = w.store.records[identifier]
                w.store.records[identifier] = old, gen + 1
            raise RunnerError("conflict" if failure == "conflict" else "uncertain", "save failed")
        return original_write(identifier, state, generation)

    w.store.write = write
    w.boundary.trace.clear()
    stale = w.runner()
    with pytest.raises(RunnerError):
        stale.resume("discover-cas")
    assert "status.before" not in w.boundary.trace
    assert "cancel.before" not in w.boundary.trace
    assert len(w.engine.submitted) == 1
    ids = list(w.engine.allocated)
    w.store.write = original_write
    assert w.runner().resume("discover-cas")["status"] == "succeeded"
    assert w.engine.allocated == ids
    assert len(w.engine.submitted) == len(set(w.engine.submitted)) == 3
    with pytest.raises(RunnerError):
        stale._save()


@pytest.mark.parametrize("sent", [False, True])
def test_cancel_discovers_original_operation_or_keeps_unsent_intent_uncertain(sent):
    w = World()

    def crash(name):
        if name == ("submit.after" if sent else "submit.before"):
            raise Crash()

    w.boundary.hook = crash
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "discover-cancel")
    w.boundary.hook = None
    w.clock.sleep(121)
    w.engine.states["request-1"] = ["RUNNING"]
    ids = list(w.engine.allocated)
    result = w.runner().cancel("discover-cancel")
    assert result["status"] == ("cancelled" if sent else "uncertain")
    assert w.engine.cancel_calls == (["request-1"] if sent else [])
    assert w.engine.allocated == ids
    assert len(w.engine.submitted) == int(sent)


def test_legacy_missing_marker_never_invents_discovery_evidence():
    w = World()
    w.runner().run(w.recipe, "legacy")
    del w.store.records["legacy"][0]["tasks"][0]["marker"]
    w.boundary.trace.clear()
    with pytest.raises(RunnerError, match="operator evidence") as exc:
        w.runner().resume("legacy")
    assert exc.value.status == "uncertain"
    assert "initialize.before" not in w.boundary.trace
    assert "discover.before" not in w.boundary.trace


def test_fake_request_uuid_lookup_is_unknown():
    w = World()
    w.runner().run(w.recipe, "distinct")
    assert w.engine.status("request-1", "request-1")["state"] == "UNKNOWN"


@pytest.mark.parametrize(
    "event", ["discover.before", "discover.after", "write.before", "write.after"]
)
def test_crash_during_discovery_recovery_keeps_original_ids(event):
    w = World()

    def crash_submit(name):
        if name == "submit.after":
            raise Crash()

    w.boundary.hook = crash_submit
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "discover-crash")
    w.clock.sleep(121)
    ids = list(w.engine.allocated)

    def crash_recovery(name):
        if name == event:
            raise Crash()

    w.boundary.hook = crash_recovery
    with pytest.raises(Crash):
        w.runner().resume("discover-crash")
    w.boundary.hook = None
    assert w.runner().resume("discover-crash")["status"] == "succeeded"
    assert w.engine.allocated == ids
    assert len(w.engine.submitted) == len(set(w.engine.submitted)) == 3


def test_auth_during_cleanup_is_not_reclassified_as_uncertain():
    w = World()
    runner = w.runner()

    def stop(name):
        if name == "submit.after":
            runner.cancel_requested = True

    w.boundary.hook = stop

    def rejected(*args):
        raise RunnerError("auth", "attached identity rejected")

    w.engine.status = rejected
    result = runner.run(w.recipe, "cleanup-auth")
    assert result["status"] == "auth"
    assert result["remote_pending"] is True
    assert w.engine.cancel_calls == []
    assert w.engine.submitted == ["request-1"]


FOREIGN = "projects/test-project/operations/foreign"


@pytest.mark.parametrize("command", ["resume", "cancel"])
@pytest.mark.parametrize("evidence", ["foreign", "match"])
def test_institutional_recovery_confirms_saved_operation_by_marker(command, evidence):
    w = World()
    w.engine.products = 1
    w.engine.states["request-1"] = ["RUNNING", "COMPLETED"]

    def crash(name):
        if name == "status.before":
            raise Crash()

    w.boundary.hook = crash
    with pytest.raises(Crash):
        w.runner().run(w.recipe, "swap")
    w.boundary.hook = None
    state, generation = w.store.records["swap"]
    assert state["tasks"][0]["operation"] == "projects/test-project/operations/server-1"
    if evidence == "foreign":
        # Another accessible export; recipe, marker and request bindings stay valid.
        w.engine.operations["other-request"] = FOREIGN
        state["tasks"][0]["operation"] = FOREIGN
        w.store.records["swap"] = (state, generation)
    w.clock.sleep(121)
    w.boundary.trace.clear()
    polled = len(w.engine.polled)
    runner = w.runner()
    if evidence == "foreign":
        with pytest.raises(RunnerError) as exc:
            getattr(runner, command)("swap")
        assert exc.value.status == "conflict"
        assert w.store.records["swap"] == (state, generation)
        assert w.engine.polled[polled:] == []
    else:
        result = getattr(runner, command)("swap")
        assert result["status"] == ("succeeded" if command == "resume" else "cancelled")
        # One bounded discovery confirms the saved name; later polls reuse it.
        assert w.boundary.trace.count("discover.before") == 1
        assert set(w.engine.polled[polled:]) == {"projects/test-project/operations/server-1"}
    assert FOREIGN not in w.engine.polled
    assert FOREIGN not in w.engine.cancelled_operations
    assert w.engine.submitted == ["request-1"] and w.engine.allocated == ["request-1"]


@pytest.mark.parametrize("remote", ["cancelled", "still_active"])
def test_generic_error_path_write_adopting_stop_runs_bounded_cleanup(remote):
    """Institutional path: a controlled store failure escapes as a generic error;
    the handler's release write adopts a concurrent stop request."""
    w = World()
    w.engine.products = 1
    w.engine.states["request-1"] = ["RUNNING"]
    if remote == "still_active":

        def cancel(request_id, operation=None):
            w.engine.cancelled_operations.append(operation)
            raise TimeoutError("PRIVATE")

        w.engine.cancel = cancel
    armed = []

    def hook(name):
        if name == "status.after" and not armed:
            armed.append("armed")
        elif name == "write.before" and armed == ["armed"]:
            armed.append("fired")
            state, generation = w.store.records["generic"]
            state = copy.deepcopy(state)
            state.update(stop="cancelled", status="cancelling")
            w.store.records["generic"] = (state, generation + 1)
            raise RuntimeError("PRIVATE store failure")

    w.boundary.hook = hook
    result = w.runner().run(w.recipe, "generic")
    assert armed == ["armed", "fired"]
    assert result["status"] == ("cancelled" if remote == "cancelled" else "uncertain")
    assert result["remote_pending"] is (remote == "still_active")
    assert w.engine.cancelled_operations == ["projects/test-project/operations/server-1"]
    assert w.engine.submitted == ["request-1"]
    state, _ = w.store.records["generic"]
    assert state["owner"] is None and state["stop"] == "cancelled"
    assert state["status"] == result["status"]
    # The original cause survives adoption and cleanup, as a category only.
    cause = dict(phase="poll", reason="unexpected internal error")
    assert state["error"] == cause and result["error"] == dict(cause, persisted=True)
    assert "PRIVATE" not in str(result) + str(state)


SECRET_TEXT = "PRIVATE https://x.invalid/?token=ya29.PRIVATE C:/Users/someone/creds.json"


@pytest.mark.parametrize(
    "error, phase, reason",
    [
        (RunnerError("no_data", "no input scenes"), "prepare", "no_data: no input scenes"),
        (RunnerError("no_data", "empty collection"), "prepare", "no_data"),  # not a fixed product text
        (RunnerError("validation", "bad\nline"), "prepare", "validation"),
        (TimeoutError(SECRET_TEXT), "prepare", "unexpected timeout"),
        (ConnectionResetError(SECRET_TEXT), "prepare", "unexpected transport or system error"),
        (KeyError(SECRET_TEXT), "prepare", "unexpected internal error"),
    ],
)
def test_institutional_failure_diagnosis_is_allowlisted_and_bounded(error, phase, reason):
    w = World()
    w.engine.preparation_error = error
    result = w.runner().run(w.recipe, "diag")
    state, _ = w.store.records["diag"]
    assert state["error"] == dict(phase=phase, reason=reason)
    assert result["error"] == dict(phase=phase, reason=reason, persisted=True)
    assert result["status"] == state["status"] == getattr(error, "status", "uncertain")
    assert "PRIVATE" not in str(result) + str(state) and len(str(result["error"])) < 300
    assert w.engine.allocated == [] and w.engine.submitted == []


def test_diagnosis_tags_and_identity_boundaries():
    from geecomposer.runner import diagnosis, identity, public_error

    exc = TimeoutError(SECRET_TEXT)
    exc.phase = "scenes"
    assert diagnosis(exc, "prepare") == dict(phase="scenes", reason="unexpected timeout")
    exc.phase = "../" + SECRET_TEXT  # an unknown tag is ignored, never echoed
    assert diagnosis(exc, "prepare") == dict(phase="prepare", reason="unexpected timeout")
    for tampered in ({"phase": "x", "reason": "y"}, {"phase": "poll", "reason": SECRET_TEXT + "\n"},
                     {"phase": "poll", "reason": "r", "extra": 1}, "text"):
        assert public_error(tampered) == dict(phase="unknown", reason="unrecognized diagnostic",
                                              persisted=True)
    w = World()
    w.runner().run(w.recipe, "ident")
    state, _ = w.store.records["ident"]
    assert identity(dict(state, error=dict(phase="poll", reason="r"))) == identity(state)


def test_failed_error_path_write_never_claims_a_persisted_diagnosis():
    w = World()
    w.engine.preparation_error = TimeoutError(SECRET_TEXT)

    def hook(name):
        if name == "write.before" and len(w.store.writes) == 1:
            raise RunnerError("uncertain", "remote write outcome unknown")

    w.boundary.hook = hook
    with pytest.raises(RunnerError) as exc:
        w.runner().run(w.recipe, "unsaved")
    assert exc.value.status == "uncertain"
    assert exc.value.diagnosis == dict(phase="prepare", reason="unexpected timeout")
    state, _ = w.store.records["unsaved"]
    assert "error" not in state and state["status"] == "running"


FOREIGN_MARKER = "gc-" + "f" * 64


@pytest.mark.parametrize(
    "change",
    ["marker_and_operation", "self_consistent", "fingerprint", "removed", "duplicate", "stop_only"],
)
def test_institutional_stop_adoption_keeps_trusted_submission_identity(change):
    from geecomposer.runner_science import discovery_marker

    w = World()
    w.engine.products = 2
    w.engine.states.update({"request-1": ["RUNNING"], "request-2": ["RUNNING"]})
    w.engine.operations["other-request"] = FOREIGN
    w.engine.markers[FOREIGN_MARKER] = FOREIGN
    suspect = []

    def hook(name):
        if name == "status.after" and not suspect:
            state, generation = copy.deepcopy(w.store.records["inst"])
            state.update(stop="cancelled", status="cancelling")
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
            elif change == "removed":
                state["tasks"].pop()
            elif change == "duplicate":
                state["tasks"].append(copy.deepcopy(first))
            w.store.records["inst"] = (state, generation + 1)
            suspect.append(copy.deepcopy(w.store.records["inst"]))

    w.boundary.hook = hook
    if change == "stop_only":
        result = w.runner().run(w.recipe, "inst")
        assert (result["status"], result["remote_pending"]) == ("cancelled", False)
        assert sorted(w.engine.cancelled_operations) == [
            "projects/test-project/operations/server-1",
            "projects/test-project/operations/server-2",
        ]
    else:
        with pytest.raises(RunnerError, match="identity") as exc:
            w.runner().run(w.recipe, "inst")
        assert exc.value.status == "conflict"
        assert w.store.records["inst"] == suspect[0]
        assert w.engine.cancelled_operations == []
    assert suspect and FOREIGN_MARKER not in w.engine.discovered
    assert FOREIGN not in w.engine.polled + w.engine.cancelled_operations
    assert w.engine.submitted == ["request-1", "request-2"]


# Secret-shaped text made ONLY of characters a printable-reason filter would admit.
LEGAL_SECRETS = [
    "ya29AbCdEf0123456789TokenValueXyz",
    "uncertain /home/alice/.config/earthengine/credentials",
    "projects 123456789012345678 operations 987654321098765432",
    "the refresh token is hunter2 and the key is swordfish",
]


@pytest.mark.parametrize("secret", LEGAL_SECRETS)
def test_unknown_runner_messages_are_never_echoed(secret):
    from geecomposer.runner import diagnosis

    for status in ("uncertain", "validation", "no_data", "auth"):
        assert diagnosis(RunnerError(status, secret), "bounds") == dict(phase="bounds", reason=status)
    w = World()
    w.engine.preparation_error = RunnerError("uncertain", secret)
    result = w.runner().run(w.recipe, "legal")
    state, _ = w.store.records["legal"]
    assert result["status"] == "uncertain" and state["error"] == dict(phase="prepare", reason="uncertain")
    assert secret not in str(result) + str(state)


def test_known_fixed_reasons_stay_useful():
    from geecomposer.runner import diagnosis

    for status, message in (("no_data", "no input scenes"), ("uncertain", "EE status reconciliation unavailable"),
                            ("uncertain", "local state write failed; inspect the state directory"),
                            ("validation", "projected pixel/calibration limit exceeded")):
        assert diagnosis(RunnerError(status, message), "poll")["reason"] == f"{status}: {message}"


UNKNOWN = dict(phase="unknown", reason="unrecognized diagnostic")
MALFORMED = [
    {"phase": "poll", "reason": LEGAL_SECRETS[0]},
    {"phase": "poll", "reason": "uncertain: " + LEGAL_SECRETS[3]},
    {"phase": "poll", "reason": "bogus: no input scenes"},
    {"phase": ["poll"], "reason": "uncertain"},
    {"phase": {"x": 1}, "reason": "uncertain"},
    {"phase": "poll", "reason": ["uncertain"]},
    {"phase": "poll", "reason": None},
    {"phase": "poll", "reason": "uncertain", "detail": "x"},
    {"phase": "poll"},
    {"phase": "poll", "reason": "x" * 5000},
    ["poll", "uncertain"],
    "uncertain",
    17,
]


@pytest.mark.parametrize("value", MALFORMED, ids=range(len(MALFORMED)))
def test_public_error_refuses_malformed_or_unknown_values_without_raising(value):
    from geecomposer.runner import public_error, public_status

    assert public_error(value) == dict(UNKNOWN, persisted=True)
    assert public_error(value, persisted=False) == dict(UNKNOWN, persisted=False)
    w = World()
    w.runner().run(w.recipe, "edited")
    state, _ = w.store.records["edited"]
    shown = public_status(dict(state, error=value), w.policy)
    assert shown["status"] == "succeeded" and shown["error"] == dict(UNKNOWN, persisted=True)
    assert LEGAL_SECRETS[0] not in str(shown) and LEGAL_SECRETS[3] not in str(shown)


def test_valid_persisted_reasons_are_shown_unchanged():
    from geecomposer.runner import public_error

    for reason in ("uncertain", "unexpected timeout", "no_data: no input scenes"):
        assert public_error(dict(phase="scenes", reason=reason)) == dict(phase="scenes", reason=reason,
                                                                        persisted=True)
