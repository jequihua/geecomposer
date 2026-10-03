"""CAS-owned, bounded run orchestration. External boundaries are injectable.

Submission intent is irreversible: once persisted as submitting, an UNKNOWN
remote status never authorizes a new ID or a repeated export. Leases bound crash
recovery delays; CAS fences stale workers before every submission intent.
"""

from __future__ import annotations

import base64
from importlib.metadata import version
import re
import uuid

import ee

from .recipe import EXIT_CODES, RunnerError, digest, require, validate_recipe
from .runner_science import PHASES, config_fingerprint, discovery_marker, request_config

LEASE_SECONDS = 120
TERMINAL_EE = {"COMPLETED", "FAILED", "CANCELLED"}
ACTIVE_EE = {"READY", "RUNNING", "CANCEL_REQUESTED"}


# Record fields a concurrent stop request or normal progress may change. Every
# other field, and each task's submission identity, is fixed once written.
# "error" is diagnostic only: never part of the run or submission identity.
PROGRESS_FIELDS = frozenset(
    {"status", "stop", "owner", "lease_until", "updated", "remote_pending", "inventory",
     "inventory_unavailable", "tasks", "error"}
)
TASK_IDENTITY = ("request_id", "name", "marker", "fingerprint")
# Fixed categories for failures that are not the product's own RunnerError. Order
# matters: TimeoutError and ConnectionError are OSError subclasses.
UNEXPECTED = (
    (TimeoutError, "unexpected timeout"),
    (ee.EEException, "unexpected service error"),
    (OSError, "unexpected transport or system error"),
)
CATEGORIES = frozenset(r for _, r in UNEXPECTED) | {"unexpected internal error"}
# The ONLY RunnerError messages a diagnosis may carry: fixed execution-path product texts
# (initialization, preparation, state, submission, polling, outputs). Any other message,
# whatever its characters, is reduced to its fixed status name.
MESSAGES = frozenset(
    {
        # initialization and the EE/HTTP boundary
        "EE initialization response unavailable", "EE credential refresh refused",
        "verified attached credentials required", "unexpected EE endpoint", "EE permission denied",
        "unsupported EE response encoding", "remote response size limit exceeded",
        "remote redirect refused", "application deadline reached",
        # scientific preparation
        "no input scenes", "scene limit exceeded", "scene snapshot exceeds budget",
        "unusable source scene identities", "duplicate source identity",
        "unusable projected AOI bound", "cannot establish output pixel bound",
        "projected pixel/calibration limit exceeded", "total scene limit exceeded",
        "no usable seasonal observations", "insufficient calibration samples",
        "output task limit exceeded", "private provenance exceeds byte budget",
        "task budget exceeded", "duplicate prepared product",
        "prepared configuration changed on resume", "scientific provenance changed on resume",
        "request-bound configuration changed on resume",
        # state
        "run ownership lost", "run ownership expired", "conditional state conflict",
        "concurrent record changed run or submission identity; not adopted",
        "state write outcome unknown", "local state write failed; inspect the state directory",
        "local run state unreadable", "local run state is locked by another process",
        "local run state is corrupt or unrecognized; it was not modified",
        "local state size limit exceeded", "remote write outcome unknown",
        "remote response unavailable", "remote permission denied",
        "private manifest size limit exceeded", "state generation unavailable",
        # submission and polling
        "invalid EE request ID", "submission operation unavailable", "submission outcome unknown",
        "original submission remains unresolved", "unknown EE status response",
        "EE status reconciliation unavailable", "EE status permission denied", "EE export failed",
        "EE export cancelled", "cancellation requested",
        "operation discovery required before status",
        "saved operation not confirmed by its submission marker; not used",
        "saved operation differs from its submission marker; not used",
        "operation listing permission denied", "operation listing inaccessible or unavailable",
        "incomplete operation listing", "invalid operation listing page",
        "malformed or repeated operation record", "invalid or repeated operation page token",
        "ambiguous operation discovery",
        "operation discovery page cap reached; enumeration incomplete",
        "operation listing could not be verified",
        "operation listing unavailable; no automatic retry",
        # institutional outputs
        "missing or oversized output inventory", "required product missing",
        "unexpected object in run output inventory", "output listing page limit exceeded",
        "output inventory exceeds bound", "output outside exact run prefix",
    }
)
UNKNOWN_ERROR = {"phase": "unknown", "reason": "unrecognized diagnostic"}


def known_reason(value):
    """A fixed status name, a fixed category, or "<status>: <allowlisted message>"."""
    if type(value) is not str or len(value) > 200:
        return False
    if value in EXIT_CODES or value in CATEGORIES:
        return True
    status, separator, message = value.partition(": ")
    return bool(separator) and status in EXIT_CODES and message in MESSAGES


def diagnosis(exc, phase):
    """Allowlisted, bounded failure evidence: never exception text, URLs, response bodies,
    paths or class names. A RunnerError keeps its status and, only when it is one of the
    fixed MESSAGES, that message; anything else is a fixed category."""
    tagged = getattr(exc, "__dict__", {}).get("phase")
    phase = tagged if type(tagged) is str and tagged in PHASES else phase
    if isinstance(exc, RunnerError):
        message = exc.args[0] if len(exc.args) == 1 else None
        reason = f"{exc.status}: {message}" if type(message) is str and message in MESSAGES else exc.status
    else:
        reason = next((r for t, r in UNEXPECTED if isinstance(exc, t)), "unexpected internal error")
    return dict(phase=phase, reason=reason if known_reason(reason) else "unexpected internal error")


def public_error(value, persisted=True):
    """Public view of a diagnosis (persisted, raised or edited on disk); persisted is False
    when no store write confirmed it. Never raises; anything not exactly well formed and
    known is shown as a fixed unknown."""
    if value is None:
        return None
    try:
        valid = (
            type(value) is dict
            and set(value) == {"phase", "reason"}
            and type(value["phase"]) is str
            and value["phase"] in PHASES
            and known_reason(value["reason"])
        )
    except Exception:
        valid = False
    shown = {"phase": value["phase"], "reason": value["reason"]} if valid else UNKNOWN_ERROR
    return dict(shown, persisted=persisted)


def identity(state):
    """Digest of immutable run bindings plus ordered task submission identities."""
    try:
        return digest(
            [
                {k: v for k, v in state.items() if k not in PROGRESS_FIELDS},
                [[task[k] for k in TASK_IDENTITY] for task in state["tasks"]],
            ]
        )
    except (TypeError, KeyError, AttributeError, RunnerError):
        return None


def run_id(value):
    require(
        type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value),
        "invalid run ID",
    )
    return value


def binding(recipe, policy, source):
    return digest(dict(recipe=recipe, policy=policy, source=source, package=version("geecomposer")))


def public_status(state, policy, persisted=True):
    """Small allowlisted shared-log view; detailed provenance stays in private GCS."""
    loc = policy["control"]
    return dict(
        run_id=state["run_id"],
        status=state["status"],
        tasks=[dict(name=t["name"], state=t["state"]) for t in state["tasks"]],
        destination=f"gs://{state['recipe']['destination']['bucket']}/{state['prefix']}/",
        manifest=f"gs://{loc['bucket']}/{loc['prefix']}/{state['run_id']}.json",
        outputs=(
            [
                f"gs://{state['recipe']['destination']['bucket']}/{x['name']}"
                for x in state.get("inventory", [])
            ]
            if state["status"] == "succeeded"
            else []
        ),
        remote_pending=state.get("remote_pending", False),
        error=public_error(state.get("error"), persisted),
    )


def verify_inventory(state, inventory):
    """Exact product directories, including EE shard names; reject empty evidence."""
    require(
        inventory and len(inventory) <= 512,
        "missing or oversized output inventory",
        "artifact",
    )
    names, seen = set(), set()
    for item in inventory:
        name = item.get("name")
        require(
            type(name) is str and name not in names,
            "duplicate or invalid output name",
            "artifact",
        )
        names.add(name)
        matched = [
            t["name"]
            for t in state["tasks"]
            if re.fullmatch(
                re.escape(state["prefix"] + "/" + t["name"] + "/image") + r"(?:-\d+-\d+)?\.tif",
                name,
            )
        ]
        require(len(matched) == 1, "unexpected object in run output inventory", "artifact")
        seen.add(matched[0])
        require(
            type(item.get("size")) is int and item["size"] > 0,
            "zero-sized output artifact",
            "artifact",
        )
        generation = item.get("generation")
        require(
            type(generation) is str
            and generation.isascii()
            and generation.isdigit()
            and 0 < int(generation) < 2**64,
            "missing artifact generation",
            "artifact",
        )
        checksum = False
        for key, length in (("crc32c", 4), ("md5", 16)):
            value = item.get(key)
            if value is not None:
                try:
                    valid = (
                        type(value) is str and len(base64.b64decode(value, validate=True)) == length
                    )
                except (ValueError, TypeError):
                    valid = False
                require(valid, "invalid artifact checksum metadata", "artifact")
                checksum = True
        require(checksum, "artifact checksum metadata unavailable", "artifact")
    require(
        seen == {t["name"] for t in state["tasks"]},
        "required product missing",
        "artifact",
    )


class Runner:
    def __init__(self, policy, store, engine, credentials, budget):
        self.policy, self.store, self.engine = policy, store, engine
        self.credentials, self.budget = credentials, budget
        self.owner = uuid.uuid4().hex
        self.state = None
        self.generation = 0
        self.owned = False
        self.cancel_requested = False
        self.initialized = False
        # identity() of this worker's last successful write; a newer record
        # carrying a stop request must match it before the stop is honored.
        self.written = None
        # request_id -> operation name established by remote evidence in this
        # process (exportImage response or exact-marker discovery). A name read
        # from persisted state is only a claim until it appears here.
        self.verified = {}
        # Step for diagnostics; science preparation tags its own sub-steps.
        self.phase = "initialize"
        # The diagnosis last confirmed by a store read or write (None if none).
        self.saved_error = None

    # Destination-specific steps. These GCS defaults are the institutional
    # contract; runner_personal overrides them for Drive without GCS inventory.

    def _validate(self, recipe):
        return validate_recipe(recipe, self.policy)

    def _reservation(self, recipe, identifier):
        buckets = {recipe["destination"]["bucket"], self.policy["control"]["bucket"]}
        preflight = [self.store.bucket_preflight(x) for x in sorted(buckets)]
        return dict(
            image_version=self.policy["image_version"],
            principal=self.policy["service_account"],
            prefix=f"{recipe['destination']['prefix']}/{identifier}-{uuid.uuid4().hex}",
            inventory=[],
            bucket_preflight=preflight,
        )

    def _valid_prefix(self, state, recipe, identifier):
        return state["prefix"].startswith(
            recipe["destination"]["prefix"] + "/" + identifier + "-"
        ) and re.fullmatch(r"[A-Za-z0-9_/-]+", state["prefix"])

    def _verify_outputs(self):
        self.state["inventory"] = self.store.inventory(
            self.state["recipe"]["destination"]["bucket"],
            self.state["prefix"],
        )
        verify_inventory(self.state, self.state["inventory"])

    def _stop_outputs(self):
        try:
            self.state["inventory"] = self.store.inventory(
                self.state["recipe"]["destination"]["bucket"], self.state["prefix"]
            )
        except Exception:
            self.state["inventory_unavailable"] = True

    def _persisted(self, state):
        return state.get("error") in (None, self.saved_error)

    def _public(self, state):
        return public_status(state, self.policy, self._persisted(state))

    def _initialize(self):
        if not self.initialized:
            self.engine.initialize(self.credentials, self.policy["project"])
            self.initialized = True

    def _load(self, identifier):
        state, generation = self.store.read(run_id(identifier))
        require(state is not None, "run reservation absent", "conflict")
        require(
            state.get("schema") == 1
            and state.get("run_id") == identifier
            and state.get("policy_sha256") == digest(self.policy),
            "run policy binding mismatch",
            "conflict",
        )
        recipe = self._validate(state["recipe"])
        require(
            state["binding"] == binding(recipe, self.policy, state["source"]),
            "run content binding mismatch",
            "conflict",
        )
        require(self._valid_prefix(state, recipe, identifier), "invalid reserved prefix", "conflict")
        require(
            type(state["tasks"]) is list and len(state["tasks"]) <= recipe["limits"]["outputs"],
            "invalid task manifest",
            "conflict",
        )
        for task in state["tasks"]:
            require(
                re.fullmatch(r"[A-Za-z0-9_-]{1,60}", task["request_id"])
                and re.fullmatch(r"[a-z0-9_]{1,60}", task["name"]),
                "invalid task identity",
                "conflict",
            )
            require(
                task.get("marker")
                == discovery_marker(task["request_id"], state["binding"], task["name"]),
                "missing or invalid discovery marker; inspect private manifest and obtain operator evidence; do not resubmit",
                "uncertain",
            )
            require(
                task["state"] in TERMINAL_EE | ACTIVE_EE | {"prepared", "submitting", "UNKNOWN"},
                "invalid persisted task state",
                "conflict",
            )
        self.saved_error = state.get("error")
        return state, generation

    def _save(self, *, release=False):
        require(
            self.owned and self.state["owner"] == self.owner,
            "run ownership lost",
            "conflict",
        )
        require(
            self.budget.wall() < self.state["lease_until"],
            "run ownership expired",
            "conflict",
        )
        self.state["lease_until"] = self.budget.wall() + LEASE_SECONDS
        self.state["updated"] = self.budget.wall()
        self.state["remote_pending"] = any(
            t["state"] not in TERMINAL_EE | {"prepared"} for t in self.state["tasks"]
        )
        if release:
            self.state["owner"] = None
            self.state["lease_until"] = 0
        try:
            self.generation = self.store.write(self.state["run_id"], self.state, self.generation)
            self.written = identity(self.state)
            self.saved_error = self.state.get("error")
        except RunnerError as exc:
            # A cancel requester only sets stop/status on a copy of our last
            # write. Honor that request by merging it into this worker's trusted
            # state, never by adopting the newer record's tasks or bindings.
            if exc.status == "conflict":
                latest, generation = self.store.read(self.state["run_id"])
                if (
                    latest
                    and latest.get("owner") == self.owner
                    and latest.get("stop") == "cancelled"
                ):
                    if self.written is None or identity(latest) != self.written:
                        self.owned = False
                        raise RunnerError(
                            "conflict",
                            "concurrent record changed run or submission identity; not adopted",
                        ) from None
                    lease = latest.get("lease_until")
                    self.state.update(
                        stop="cancelled",
                        status="cancelling",
                        owner=self.owner,
                        lease_until=lease if type(lease) in (int, float) else 0,
                    )
                    self.generation = generation
                    raise RunnerError("cancelled", "cancellation requested") from None
            self.owned = False
            raise
        if release:
            self.owned = False
            self.budget.heartbeat = None

    def _heartbeat(self):
        if self.cancel_requested and not self.budget.finalizing:
            raise RunnerError("cancelled", "cancellation requested")
        if self.owned and self.budget.wall() >= self.state["lease_until"] - 90:
            self._save()

    def _operation(self, task):
        """Return the saved operation only after remote evidence binds it to the marker.

        Called at every point that polls or cancels, so a reloaded or adopted
        state cannot substitute another operation. Discovery runs once per
        unconfirmed name; absence or mismatch fails closed without using it.
        """
        operation = task.get("operation")
        if operation is not None and self.verified.get(task["request_id"]) != operation:
            found = self.engine.discover(task["marker"])
            require(
                found is not None,
                "saved operation not confirmed by its submission marker; not used",
                "uncertain",
            )
            require(
                found == operation,
                "saved operation differs from its submission marker; not used",
                "conflict",
            )
            self.verified[task["request_id"]] = operation
        return operation

    def _status(self, task):
        if task.get("operation"):
            self._operation(task)
        else:
            # Discovery is outside the status retry loop. Even a late match is
            # unusable until its name has been saved with the current generation.
            operation = self.engine.discover(task["marker"])
            if operation is None:
                task["state"] = "UNKNOWN"
                return "UNKNOWN"
            task["operation"] = operation
            self.verified[task["request_id"]] = operation
            if self.owned:
                self._save()
            else:
                # Recovery evidence only, before takeover; retain the old owner.
                self.generation = self.store.write(
                    self.state["run_id"], self.state, self.generation
                )
        retries = (
            self.state["recipe"]["limits"]["status_retries"]
            if self.state
            else self.policy["limits"]["status_retries"]
        )
        for attempt in range(retries + 1):
            self.budget.checkpoint()
            try:
                result = self.engine.status(task["request_id"], self._operation(task))
                status = result.get("state")
                require(
                    status in ACTIVE_EE | TERMINAL_EE | {"UNKNOWN"},
                    "unknown EE status response",
                    "uncertain",
                )
                task["state"] = status
                return status
            except RunnerError:
                raise
            except Exception as exc:
                # Retry transport/429/5xx failures only; do not expose raw text.
                code = getattr(exc, "code", None)
                transient = isinstance(exc, (TimeoutError, ConnectionError, OSError)) or code in (
                    429,
                    500,
                    502,
                    503,
                    504,
                )
                if code in (401, 403):
                    raise RunnerError("auth", "EE status permission denied") from None
                if not transient or attempt == retries:
                    raise RunnerError("uncertain", "EE status reconciliation unavailable") from None
                # Bounded exponential backoff with deterministic per-ID jitter.
                jitter = int(digest(task["request_id"])[0], 16) % 4
                self.budget.pause(min(60, 15 * 2**attempt + jitter))

    def status(self, identifier):
        state, _ = self._load(identifier)
        return self._public(state)

    def run(self, recipe, identifier, source=None):
        recipe = self._validate(recipe)
        identifier = run_id(identifier)
        source = source or {"kind": "local", "sha256": digest(recipe)}
        existing, _ = self.store.read(identifier)
        require(
            existing is None,
            "run ID already reserved; use status or explicit resume",
            "conflict",
        )
        reservation = self._reservation(recipe, identifier)
        self.budget.constrain(self.budget.started + recipe["limits"]["deadline_seconds"])
        now = self.budget.wall()
        self.state = dict(
            schema=1,
            run_id=identifier,
            recipe=recipe,
            source=source,
            recipe_sha256=digest(recipe),
            binding=binding(recipe, self.policy, source),
            policy_sha256=digest(self.policy),
            package_version=version("geecomposer"),
            project=self.policy["project"],
            created=now,
            updated=now,
            deadline=self.budget.started + recipe["limits"]["deadline_seconds"],
            owner=self.owner,
            lease_until=now + LEASE_SECONDS,
            status="running",
            tasks=[],
            stop=None,
            **reservation,
        )
        # Reservation, canonical recipe and execution binding precede EE work.
        self.generation = self.store.write(identifier, self.state, 0)
        self.written = identity(self.state)
        self.owned = True
        self.budget.heartbeat = self._heartbeat
        return self._execute()

    def resume(self, identifier):
        self.state, self.generation = self._load(identifier)
        require(
            not self.state["owner"] or self.budget.wall() >= self.state["lease_until"],
            "another worker owns the run",
            "conflict",
        )
        if self.state["status"] == "succeeded":
            return self._public(self.state)
        if self.state["stop"] or self.budget.wall() >= self.state["deadline"] - 60:
            self.budget.finalize()
        else:
            self.budget.constrain(self.state["deadline"])
        self._initialize()
        # Reconcile every original submission before CAS takeover. UNKNOWN
        # remains an unresolved intent, including a crash just before export.
        for task in self.state["tasks"]:
            if task["state"] != "prepared":
                self._status(task)
        self.state["owner"] = self.owner
        self.state["lease_until"] = self.budget.wall() + LEASE_SECONDS
        self.generation = self.store.write(identifier, self.state, self.generation)
        self.written = identity(self.state)
        self.owned = True
        self.budget.heartbeat = self._heartbeat
        if self.state["stop"]:
            return self._finish_stop(self.state["stop"])
        if self.budget.wall() >= self.state["deadline"] - 60:
            return self._finish_stop("deadline")
        self.budget.constrain(self.state["deadline"])
        return self._execute()

    def cancel(self, identifier):
        self.state, self.generation = self._load(identifier)
        if self.state["status"] == "succeeded":
            return self._public(self.state)
        self.state["stop"] = "cancelled"
        self.state["status"] = "cancelling"
        # A live worker observes this generation change at its next state write;
        # a paused submitter's persisted intent is included in cancellation.
        if self.state["owner"] and self.budget.wall() < self.state["lease_until"]:
            self.store.write(identifier, self.state, self.generation)
            return self._public(self.state)
        self.budget.finalize()
        self._initialize()
        for task in self.state["tasks"]:
            if task["state"] != "prepared":
                self._status(task)
        self.state["owner"] = self.owner
        self.state["lease_until"] = self.budget.wall() + LEASE_SECONDS
        self.generation = self.store.write(identifier, self.state, self.generation)
        self.written = identity(self.state)
        self.owned = True
        self.budget.heartbeat = self._heartbeat
        return self._finish_stop("cancelled")

    def _execute(self):
        # A stale diagnosis is replaced only by this invocation's own outcome.
        self.state.pop("error", None)
        try:
            self.phase = "initialize"
            self._initialize()
            self.phase = "prepare"
            require(
                not any(t["state"] == "UNKNOWN" for t in self.state["tasks"]),
                "original submission remains unresolved",
                "uncertain",
            )
            if any(t["state"] == "FAILED" for t in self.state["tasks"]):
                raise RunnerError("export", "EE export failed")
            if any(t["state"] == "CANCELLED" for t in self.state["tasks"]):
                raise RunnerError("cancelled", "EE export cancelled")
            configs = {}
            if not self.state["tasks"] or any(
                t["state"] == "prepared" for t in self.state["tasks"]
            ):
                prepared, provenance = self.engine.prepare(
                    self.state["recipe"],
                    self.state["prefix"],
                    self.state.get("provenance"),
                )
                require(
                    0 < len(prepared) <= self.state["recipe"]["limits"]["outputs"],
                    "task budget exceeded",
                )
                configs = {t["name"]: t["config"] for t in prepared}
                require(
                    len(configs) == len(prepared),
                    "duplicate prepared product",
                    "conflict",
                )
                self.phase = "persist"
                plan = [{"name": t["name"], "fingerprint": t["fingerprint"]} for t in prepared]
                if "plan" in self.state:
                    require(
                        self.state["plan"] == plan,
                        "prepared configuration changed on resume",
                        "conflict",
                    )
                    require(
                        digest(self.state["provenance"]) == digest(provenance),
                        "scientific provenance changed on resume",
                        "conflict",
                    )
                self.state["provenance"] = provenance
                self.state["plan"] = plan
                self._save()
                for product in prepared:
                    if product["name"] not in {t["name"] for t in self.state["tasks"]}:
                        self.phase = "allocate"
                        request_id = self.engine.new_id()
                        require(
                            type(request_id) is str
                            and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", request_id),
                            "invalid EE request ID",
                            "uncertain",
                        )
                        marker = discovery_marker(
                            request_id, self.state["binding"], product["name"]
                        )
                        self.state["tasks"].append(
                            dict(
                                name=product["name"],
                                marker=marker,
                                fingerprint=config_fingerprint(
                                    request_config(product["config"], marker)
                                ),
                                request_id=request_id,
                                state="prepared",
                            )
                        )
                        self.phase = "persist"
                        self._save()
                for task in self.state["tasks"]:
                    config = request_config(configs[task["name"]], task["marker"])
                    require(
                        config_fingerprint(config) == task["fingerprint"],
                        "request-bound configuration changed on resume",
                        "conflict",
                    )
                    configs[task["name"]] = config
            self.state["status"] = "running"
            while True:
                self.budget.checkpoint()
                for task in self.state["tasks"]:
                    if task["state"] in ACTIVE_EE | {"submitting"}:
                        self.phase = "poll"
                        status = self._status(task)
                        self._save()
                        require(
                            status != "UNKNOWN",
                            "original submission remains unresolved",
                            "uncertain",
                        )
                        require(status != "FAILED", "EE export failed", "export")
                        require(status != "CANCELLED", "EE export cancelled", "cancelled")
                active = sum(t["state"] in ACTIVE_EE for t in self.state["tasks"])
                for task in self.state["tasks"]:
                    if active >= self.state["recipe"]["limits"]["active_exports"]:
                        break
                    if task["state"] == "prepared":
                        self.budget.checkpoint()
                        task["state"] = "submitting"
                        self.phase = "persist"
                        self._save()  # Durable ID + fingerprint + intent, before exportImage.
                        self.budget.checkpoint()
                        self.phase = "submit"
                        try:
                            response = self.engine.submit(task["request_id"], configs[task["name"]])
                            operation = response.get("name")
                            require(
                                type(operation) is str
                                and re.fullmatch(
                                    r"projects/[a-zA-Z0-9_-]+/operations/[a-zA-Z0-9_-]+",
                                    operation,
                                ),
                                "submission operation unavailable",
                                "uncertain",
                            )
                            task.update(operation=operation, state="READY")
                            self.verified[task["request_id"]] = operation
                        except Exception as exc:
                            if isinstance(exc, RunnerError) and exc.status == "auth":
                                raise
                            if not self.owned:
                                raise
                            # Do not send another export, even with the same ID.
                            status = self._status(task)
                            require(
                                status != "UNKNOWN",
                                "submission outcome unknown",
                                "uncertain",
                            )
                            require(status != "FAILED", "EE export failed", "export")
                            require(
                                status != "CANCELLED",
                                "EE export cancelled",
                                "cancelled",
                            )
                        self._save()
                        active += task["state"] in ACTIVE_EE
                if all(t["state"] == "COMPLETED" for t in self.state["tasks"]):
                    self.phase = "outputs"
                    self._verify_outputs()
                    self.state["status"] = "succeeded"
                    self._save(release=True)
                    return self._public(self.state)
                self.phase = "poll"
                self.budget.pause(self.policy["poll_seconds"])
        except RunnerError as exc:
            if not self.owned:
                # Not persisted by this worker: the CLI reports it as unconfirmed.
                exc.diagnosis = diagnosis(exc, self.phase)
                raise
            self.state["error"] = diagnosis(exc, self.phase)
            if exc.status in ("deadline", "cancelled", "export"):
                return self._finish_stop(exc.status)
            return self._release_with(exc.status)
        except Exception as exc:
            if not self.owned:
                error = RunnerError("uncertain", "state write outcome unknown")
                error.diagnosis = diagnosis(exc, self.phase)
                raise error from None
            self.state["error"] = diagnosis(exc, self.phase)
            return self._release_with("uncertain")
        finally:
            self.budget.heartbeat = None

    def _release_with(self, status):
        """Final error-path write. If it adopts a newer stop request, that request
        is not remote evidence: run the ordinary bounded cleanup exactly once."""
        self.state["status"] = status
        try:
            self._save(release=True)
        except RunnerError as exc:
            if exc.status != "cancelled" or not self.owned:
                # The original cause survives in output, never as a persisted claim.
                exc.diagnosis = self.state.get("error")
                raise
            return self._finish_stop("cancelled")
        return self._public(self.state)

    def _finish_stop(self, reason):
        # A separate recovery invocation can finalize an old run, but cannot
        # restart its expired work budget. Cleanup itself is capped at 60 s.
        self.budget.finalize()
        self.state["stop"] = reason
        self.state["status"] = "cancelling" if reason == "cancelled" else reason
        self.state["remote_pending"] = any(
            t["state"] not in TERMINAL_EE | {"prepared"} for t in self.state["tasks"]
        )
        try:
            self._save()
            for task in self.state["tasks"]:
                if task["state"] == "prepared":
                    continue
                try:
                    status = self._status(task)
                    if status in ACTIVE_EE:
                        self.engine.cancel(task["request_id"], self._operation(task))
                        self._status(task)
                    if task["state"] not in TERMINAL_EE:
                        self.state["remote_pending"] = True
                except Exception as exc:
                    if isinstance(exc, RunnerError) and not self.owned:
                        raise  # e.g. a rejected record: keep its meaning
                    if isinstance(exc, RunnerError) and exc.status == "auth":
                        self.state["status"] = "auth"
                        self._save(release=True)
                        return self._public(self.state)
                    self.state["remote_pending"] = True
                self._save()
            self._stop_outputs()
            self.state["remote_pending"] = any(
                t["state"] not in TERMINAL_EE | {"prepared"} for t in self.state["tasks"]
            )
            if reason == "cancelled":
                self.state["status"] = "uncertain" if self.state["remote_pending"] else "cancelled"
            self._save(release=True)
        except RunnerError as exc:
            self.owned = False
            self.budget.heartbeat = None
            if exc.status != "cancelled":
                raise
            # A further stop request won a write during cleanup (only _save
            # adoption raises "cancelled" once finalizing). Its durable record
            # keeps the stop for a later cancel/resume; do not claim cancellation.
            result = self._public(self.state)
            result.update(status="uncertain", remote_pending=True)
            return result
        return self._public(self.state)
