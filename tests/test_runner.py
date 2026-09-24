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
