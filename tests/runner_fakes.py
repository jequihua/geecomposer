"""Small deterministic external boundaries; no repository helpers or network."""

import copy

from geecomposer.recipe import RunnerError, digest, validate_policy, validate_recipe
from geecomposer.runner import Runner
from geecomposer.runner_cloud import Budget
from geecomposer.runner_science import config_fingerprint


def policy_value():
    return dict(
        version=1,
        mode="local",
        project="test-project",
        service_account="worker@test-project.iam.gserviceaccount.com",
        inputs=[dict(bucket="input-bucket", prefix="recipes")],
        outputs=[dict(bucket="output-bucket", prefix="products")],
        control=dict(bucket="control-bucket", prefix="runs"),
        limits={},
        poll_seconds=15,
        image_version="offline-test",
    )


def recipe_value():
    return dict(
        version=1,
        workflow="composite",
        dataset="sentinel2",
        bands=["B4", "B8"],
        start="2024-01-01",
        end="2024-02-01",
        transform="ndvi",
        aoi=dict(
            type="Polygon",
            coordinates=[[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]],
        ),
        grid=dict(crs="EPSG:4326", transform=[0.0001, 0, 0, 0, -0.0001, 0.001]),
        destination=dict(bucket="output-bucket", prefix="products/test"),
    )


class Crash(BaseException):
    pass


class Clock:
    def __init__(self):
        self.now = 0

    def wall(self):
        return 1_000_000 + self.now

    def sleep(self, seconds):
        self.now += seconds

    def budget(self, seconds=1800):
        return Budget(seconds, monotonic=lambda: self.now, wall=self.wall, sleep=self.sleep)


class Boundary:
    def __init__(self):
        self.trace = []
        self.fault = None
        self.hook = None

    def event(self, name):
        self.trace.append(name)
        if self.hook:
            self.hook(name)
        if len(self.trace) == self.fault:
            self.fault = None
            raise Crash(name)


class Store:
    def __init__(self, boundary):
        self.boundary = boundary
        self.records = {}
        self.writes = []
        self.artifacts = None

    def read(self, identifier):
        self.boundary.event("read.before")
        result = copy.deepcopy(self.records.get(identifier, (None, 0)))
        self.boundary.event("read.after")
        return result

    def write(self, identifier, state, generation):
        self.boundary.event("write.before")
        _, current = self.records.get(identifier, (None, 0))
        if generation != current:
            raise RunnerError("conflict", "conditional state conflict")
        self.records[identifier] = copy.deepcopy(state), current + 1
        self.writes.append((generation, copy.deepcopy(state)))
        self.boundary.event("write.after")
        return current + 1

    def bucket_preflight(self, bucket):
        self.boundary.event("bucket.before")
        result = dict(bucket=bucket, location="EU", access="accessible")
        self.boundary.event("bucket.after")
        return result

    def inventory(self, bucket, prefix):
        self.boundary.event("inventory.before")
        if self.artifacts is not None:
            result = self.artifacts
        else:
            state = next(s for s, _ in self.records.values() if s["prefix"] == prefix)
            result = [
                dict(
                    name=prefix + "/" + task["name"] + "/image.tif",
                    size=42,
                    generation="1",
                    crc32c="AAAAAA==",
                    md5=None,
                )
                for task in state["tasks"]
                if task["state"] == "COMPLETED"
            ]
        self.boundary.event("inventory.after")
        return copy.deepcopy(result)


class Engine:
    def __init__(self, boundary, store):
        self.boundary, self.store = boundary, store
        self.allocated = []
        self.submitted = []
        self.remote = {}
        self.operations = {}
        self.markers = {}
        self.states = {}
        self.products = 3
        self.loss = False
        self.preparation_error = None
        self.cancel_calls = []
        self.fail_status = 0

    def initialize(self, credentials, project):
        self.boundary.event("initialize.before")
        self.boundary.event("initialize.after")

    def prepare(self, recipe, prefix, previous=None):
        self.boundary.event("prepare.before")
        if self.preparation_error:
            raise self.preparation_error
        configs = [
            dict(
                name=f"product_{i}",
                config={"expression": f"image_{i}", "prefix": prefix},
                fingerprint=digest([i, prefix]),
            )
            for i in range(self.products)
        ]
        result = configs, dict(sources={"all": ["scene1"]}, pixels_upper_bound=196)
        self.boundary.event("prepare.after")
        return result

    def new_id(self):
        self.boundary.event("new_id.before")
        value = f"request-{len(self.allocated) + 1}"
        self.allocated.append(value)
        self.boundary.event("new_id.after")
        return value

    def submit(self, request_id, config):
        self.boundary.event("submit.before")
        state = next(s for s, _ in self.store.records.values() if s["prefix"] == config["prefix"])
        task = next(t for t in state["tasks"] if t["request_id"] == request_id)
        assert task["state"] == "submitting"
        assert task["fingerprint"] == config_fingerprint(config)
        assert task["marker"] == config["description"]
        assert state["recipe"]
        assert state["binding"]
        assert state["owner"]
        assert sum(t["state"] in ("READY", "RUNNING", "submitting") for t in state["tasks"]) <= 2
        assert request_id not in self.submitted
        self.submitted.append(request_id)
        self.remote[request_id] = "RUNNING"
        self.operations[request_id] = (
            f"projects/test-project/operations/server-{len(self.submitted)}"
        )
        self.markers[config["description"]] = self.operations[request_id]
        self.boundary.event("submit.after")
        if self.loss:
            raise TimeoutError("SECRET lost response")
        return {"name": self.operations[request_id]}

    def discover(self, marker):
        self.boundary.event("discover.before")
        operation = self.markers.get(marker)
        self.boundary.event("discover.after")
        return operation

    def persisted(self, request_id, operation):
        assert any(
            t["request_id"] == request_id and t.get("operation") == operation
            for s, _ in self.store.records.values()
            for t in s["tasks"]
        )

    def status(self, request_id, operation=None):
        self.boundary.event("status.before")
        if self.fail_status:
            self.fail_status -= 1
            raise TimeoutError("SECRET token")
        if operation is None or operation != self.operations.get(request_id):
            state = "UNKNOWN"
        else:
            self.persisted(request_id, operation)
            sequence = self.states.get(request_id, ["COMPLETED"])
            state = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            self.remote[request_id] = state
        self.boundary.event("status.after")
        return {"state": state, "error_message": "SECRET never log"}

    def cancel(self, request_id, operation=None):
        self.boundary.event("cancel.before")
        assert operation == self.operations[request_id]
        self.persisted(request_id, operation)
        self.cancel_calls.append(request_id)
        self.states[request_id] = ["CANCELLED"]
        self.remote[request_id] = "CANCELLED"
        self.boundary.event("cancel.after")


class World:
    def __init__(self):
        self.clock, self.boundary = Clock(), Boundary()
        self.store = Store(self.boundary)
        self.engine = Engine(self.boundary, self.store)
        self.policy = validate_policy(policy_value())
        self.recipe = validate_recipe(recipe_value(), self.policy)

    def runner(self):
        return Runner(self.policy, self.store, self.engine, object(), self.clock.budget())
