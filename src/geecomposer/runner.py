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

from .recipe import RunnerError, digest, require, validate_recipe
from .runner_science import config_fingerprint, discovery_marker, request_config

LEASE_SECONDS = 120
TERMINAL_EE = {"COMPLETED", "FAILED", "CANCELLED"}
ACTIVE_EE = {"READY", "RUNNING", "CANCEL_REQUESTED"}


def run_id(value):
    require(
        type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value),
        "invalid run ID",
    )
    return value


def binding(recipe, policy, source):
    return digest(dict(recipe=recipe, policy=policy, source=source, package=version("geecomposer")))


def public_status(state, policy):
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
        recipe = validate_recipe(state["recipe"], self.policy)
        require(
            state["binding"] == binding(recipe, self.policy, state["source"]),
            "run content binding mismatch",
            "conflict",
        )
        require(
            state["prefix"].startswith(recipe["destination"]["prefix"] + "/" + identifier + "-")
            and re.fullmatch(r"[A-Za-z0-9_/-]+", state["prefix"]),
            "invalid reserved prefix",
            "conflict",
        )
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
        except RunnerError as exc:
            # A cancel requester only sets stop/status. Adopting that newer CAS
            # generation authorizes cleanup, never another export.
            if exc.status == "conflict":
                latest, generation = self.store.read(self.state["run_id"])
                if (
                    latest
                    and latest.get("owner") == self.owner
                    and latest.get("stop") == "cancelled"
                ):
                    self.state, self.generation = latest, generation
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

    def _status(self, task):
        if not task.get("operation"):
            # Discovery is outside the status retry loop. Even a late match is
            # unusable until its name has been saved with the current generation.
            operation = self.engine.discover(task["marker"])
            if operation is None:
                task["state"] = "UNKNOWN"
                return "UNKNOWN"
            task["operation"] = operation
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
                result = self.engine.status(task["request_id"], task.get("operation"))
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
        return public_status(state, self.policy)

    def run(self, recipe, identifier, source=None):
        recipe = validate_recipe(recipe, self.policy)
        identifier = run_id(identifier)
        source = source or {"kind": "local", "sha256": digest(recipe)}
        existing, _ = self.store.read(identifier)
        require(
            existing is None,
            "run ID already reserved; use status or explicit resume",
            "conflict",
        )
        buckets = {recipe["destination"]["bucket"], self.policy["control"]["bucket"]}
        preflight = [self.store.bucket_preflight(x) for x in sorted(buckets)]
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
            image_version=self.policy["image_version"],
            project=self.policy["project"],
            principal=self.policy["service_account"],
            created=now,
            updated=now,
            deadline=self.budget.started + recipe["limits"]["deadline_seconds"],
            owner=self.owner,
            lease_until=now + LEASE_SECONDS,
            status="running",
            prefix=f"{recipe['destination']['prefix']}/{identifier}-{uuid.uuid4().hex}",
            tasks=[],
            inventory=[],
            bucket_preflight=preflight,
            stop=None,
        )
        # Reservation, canonical recipe and execution binding precede EE work.
        self.generation = self.store.write(identifier, self.state, 0)
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
            return public_status(self.state, self.policy)
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
            return public_status(self.state, self.policy)
        self.state["stop"] = "cancelled"
        self.state["status"] = "cancelling"
        # A live worker observes this generation change at its next state write;
        # a paused submitter's persisted intent is included in cancellation.
        if self.state["owner"] and self.budget.wall() < self.state["lease_until"]:
            self.store.write(identifier, self.state, self.generation)
            return public_status(self.state, self.policy)
        self.budget.finalize()
        self._initialize()
        for task in self.state["tasks"]:
            if task["state"] != "prepared":
                self._status(task)
        self.state["owner"] = self.owner
        self.state["lease_until"] = self.budget.wall() + LEASE_SECONDS
        self.generation = self.store.write(identifier, self.state, self.generation)
        self.owned = True
        self.budget.heartbeat = self._heartbeat
        return self._finish_stop("cancelled")

    def _execute(self):
        try:
            self._initialize()
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
                        self._save()  # Durable ID + fingerprint + intent, before exportImage.
                        self.budget.checkpoint()
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
                    self.state["inventory"] = self.store.inventory(
                        self.state["recipe"]["destination"]["bucket"],
                        self.state["prefix"],
                    )
                    verify_inventory(self.state, self.state["inventory"])
                    self.state["status"] = "succeeded"
                    self._save(release=True)
                    return public_status(self.state, self.policy)
                self.budget.pause(self.policy["poll_seconds"])
        except RunnerError as exc:
            if not self.owned:
                raise
            if exc.status in ("deadline", "cancelled", "export"):
                return self._finish_stop(exc.status)
            self.state["status"] = exc.status
            self._save(release=True)
            return public_status(self.state, self.policy)
        except Exception:
            if not self.owned:
                raise RunnerError("uncertain", "state write outcome unknown") from None
            self.state["status"] = "uncertain"
            self._save(release=True)
            return public_status(self.state, self.policy)
        finally:
            self.budget.heartbeat = None

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
                        self.engine.cancel(task["request_id"], task.get("operation"))
                        self._status(task)
                    if task["state"] not in TERMINAL_EE:
                        self.state["remote_pending"] = True
                except Exception as exc:
                    if isinstance(exc, RunnerError) and exc.status == "auth":
                        self.state["status"] = "auth"
                        self._save(release=True)
                        return public_status(self.state, self.policy)
                    self.state["remote_pending"] = True
                self._save()
            try:
                self.state["inventory"] = self.store.inventory(
                    self.state["recipe"]["destination"]["bucket"], self.state["prefix"]
                )
            except Exception:
                self.state["inventory_unavailable"] = True
            self.state["remote_pending"] = any(
                t["state"] not in TERMINAL_EE | {"prepared"} for t in self.state["tasks"]
            )
            if reason == "cancelled":
                self.state["status"] = "uncertain" if self.state["remote_pending"] else "cancelled"
            self._save(release=True)
        except RunnerError:
            self.owned = False
            self.budget.heartbeat = None
            raise
        return public_status(self.state, self.policy)
