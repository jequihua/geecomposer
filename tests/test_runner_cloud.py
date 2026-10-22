from types import SimpleNamespace
from unittest.mock import Mock

import ee
import pytest
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.compute_engine.credentials import Credentials as ComputeCredentials

from geecomposer.recipe import RunnerError, canonical, validate_policy
from geecomposer.runner_cloud import (
    AttachedCredentials,
    EarthEngine,
    EETransport,
    GCS,
    Metadata,
    attached_identity,
    connect,
)
from runner_fakes import Clock, policy_value, recipe_value

pytestmark = pytest.mark.usefixtures("runner_offline")


class MetadataFake:
    def __init__(self, email="worker@test-project.iam.gserviceaccount.com"):
        self.email, self.calls = email, []

    def get(self, resource):
        self.calls.append(resource)
        if not resource:
            return {"email": self.email}
        return {
            "access_token": "PRIVATE_TOKEN",
            "expires_in": 3600,
            "token_type": "Bearer",
        }


@pytest.mark.parametrize(
    "principal",
    [None, "", "person@example.org", "other@test-project.iam.gserviceaccount.com"],
)
def test_wrong_identity_rejected_before_token_ee_or_storage(principal, monkeypatch):
    meta = MetadataFake(principal)
    monkeypatch.setattr(
        ee, "Initialize", lambda **k: pytest.fail("initialized with wrong principal")
    )
    monkeypatch.setattr(
        "geecomposer.runner_cloud.storage.Client",
        lambda **k: pytest.fail("constructed storage before identity"),
    )
    monkeypatch.setattr("geecomposer.runner_cloud.Metadata", lambda b: meta)
    with pytest.raises(RunnerError) as exc:
        connect(validate_policy(policy_value()), Clock().budget())
    assert exc.value.status == "auth"
    assert meta.calls == [""]


@pytest.mark.parametrize(
    "credential",
    ["persistent", None, object(), UserCredentials("TOKEN"), ComputeCredentials()],
)
def test_asserted_or_discovered_credentials_are_not_attached_proof(credential, monkeypatch):
    monkeypatch.setattr(
        ee, "Initialize", lambda **k: pytest.fail("initialized unverified identity")
    )
    with pytest.raises(RunnerError) as exc:
        EarthEngine(Clock().budget()).initialize(credential, "test-project")
    assert exc.value.status == "auth"


@pytest.mark.parametrize(
    "key",
    [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCE_METADATA_HOST",
        "GCE_METADATA_IP",
        "GCE_METADATA_ROOT",
        "STORAGE_EMULATOR_HOST",
    ],
)
def test_credential_files_keys_federation_and_metadata_overrides_rejected(key):
    meta = MetadataFake()
    with pytest.raises(RunnerError):
        attached_identity(
            validate_policy(policy_value()),
            Clock().budget(),
            metadata=meta,
            environ={key: "PRIVATE_KEY_OR_FEDERATION_CONFIG"},
        )
    assert meta.calls == []


def test_attached_refresh_verifies_actual_principal_each_time():
    meta = MetadataFake()
    credentials = attached_identity(
        validate_policy(policy_value()), Clock().budget(), metadata=meta, environ={}
    )
    assert type(credentials) is AttachedCredentials
    assert credentials.token == "PRIVATE_TOKEN"
    assert credentials.service_account_email == meta.email
    meta.email = "changed@test-project.iam.gserviceaccount.com"
    with pytest.raises(RunnerError):
        credentials.refresh(Mock(side_effect=AssertionError("generic auth transport used")))
    assert meta.calls == ["", "token", ""]


@pytest.mark.parametrize("status,headers", [(403, {"metadata-flavor": "Google"}), (200, {})])
def test_metadata_origin_header_is_required(status, headers, monkeypatch):
    read = Mock(
        return_value=(
            status,
            headers,
            canonical({"email": "worker@test-project.iam.gserviceaccount.com"}),
        )
    )
    monkeypatch.setattr("geecomposer.runner_cloud._read_url", read)
    with pytest.raises(RunnerError):
        Metadata(Clock().budget()).get("")
    assert read.call_args.args[0].startswith("http://169.254.169.254/computeMetadata/v1/")
    assert read.call_args.kwargs["headers"] == {"Metadata-Flavor": "Google"}


def test_native_submission_transport_never_uses_task_start(real_ee, monkeypatch):
    calls = []
    monkeypatch.setattr(ee.data, "newTaskId", lambda: ["request-id"])
    monkeypatch.setattr(
        ee.data,
        "exportImage",
        lambda *args: calls.append(args) or {"name": "projects/test-project/operations/op-id"},
    )
    monkeypatch.setattr(ee.batch.Task, "start", lambda *args: pytest.fail("Task.start used"))
    from geecomposer.export.gcs import export_to_gcs

    config = export_to_gcs(
        ee.Image.constant(1),
        "test",
        "output-bucket",
        ee.Geometry.Rectangle([0, 0, 1, 1]),
        scale=10,
    ).config
    engine = EarthEngine(Clock().budget())
    request_id = engine.new_id()
    result = engine.submit(request_id, config)
    assert calls == [("request-id", config)]
    assert calls[0][1] is config
    assert result["name"].endswith("op-id")
    status = Mock(return_value=[{"state": "COMPLETED"}])
    monkeypatch.setattr(ee.data, "getTaskStatus", status)
    engine.status(request_id, result["name"])
    status.assert_called_once_with("op-id")


def test_ee_transport_checks_budget_inside_scientific_calls(monkeypatch):
    clock = Clock()
    budget = clock.budget(120)
    transport = EETransport(budget)
    read = Mock(return_value=(200, {}, b"{}"))
    monkeypatch.setattr("geecomposer.runner_cloud._read_url", read)
    clock.sleep(61)
    with pytest.raises(RunnerError) as exc:
        transport.request(
            "https://earthengine.googleapis.com/v1/projects/p/value:compute",
            method="POST",
        )
    assert exc.value.status == "deadline"
    read.assert_not_called()


class RemoteFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("PRIVATE raw exception")


def storage_fake():
    blob = Mock(generation=7, size=42, crc32c="AAAAAA==", md5_hash=None)
    client = Mock()
    client.bucket.return_value.blob.return_value = blob
    return GCS(client, validate_policy(policy_value()), Clock().budget()), client, blob


@pytest.mark.parametrize("code,message", [(404, "absent"), (403, "inaccessible"), (500, "unknown")])
def test_bucket_absence_permission_unknown_distinction(code, message):
    gcs, client, _ = storage_fake()
    client.get_bucket.side_effect = RemoteFailure(code)
    with pytest.raises(RunnerError) as exc:
        gcs.bucket_preflight("output-bucket")
    assert message in str(exc.value)
    assert "PRIVATE" not in str(exc.value)
    assert client.get_bucket.call_args.kwargs == {"timeout": 10.0, "retry": None}
    client.create_bucket.assert_not_called()


def test_state_cas_has_no_unconditional_or_blind_retry():
    gcs, _, blob = storage_fake()
    assert gcs.write("run", {"schema": 1}, 0) == 7
    assert blob.upload_from_string.call_args.kwargs["if_generation_match"] == 0
    assert blob.upload_from_string.call_args.kwargs["retry"] is None
    blob.upload_from_string.side_effect = RemoteFailure(412)
    with pytest.raises(RunnerError) as exc:
        gcs.write("run", {"schema": 1}, 7)
    assert exc.value.status == "conflict"
    assert blob.upload_from_string.call_count == 2
    blob.delete.assert_not_called()


def test_generation_pinned_input_size_check_before_allocation():
    gcs, client, blob = storage_fake()
    blob.size = 1_048_577
    with pytest.raises(RunnerError):
        gcs.read_recipe("gs://input-bucket/recipes/test.json", 7)
    blob.download_as_bytes.assert_not_called()
    blob.size = 42
    blob.download_as_bytes.return_value = canonical(recipe_value())
    value, source = gcs.read_recipe("gs://input-bucket/recipes/test.json", 7)
    assert value == recipe_value()
    assert source["generation"] == 7
    client.bucket.return_value.blob.assert_called_with("recipes/test.json", generation=7)
    kwargs = blob.download_as_bytes.call_args.kwargs
    assert kwargs["if_generation_match"] == 7
    assert kwargs["start"] == 0
    assert kwargs["end"] == 1_048_576
    assert kwargs["raw_download"] is True
    assert kwargs["retry"] is None


@pytest.mark.parametrize(
    "uri,generation",
    [
        ("https://input-bucket/recipes/a", 7),
        ("gs://input-bucket/recipes/a", None),
        ("gs://other-bucket/recipes/a", 7),
        ("gs://input-bucket/recipes-evil/a", 7),
        ("gs://input-bucket/recipes/../a", 7),
        ("gs://input-bucket/recipes/a?key=SECRET", 7),
        ("gs://input-bucket/recipes/a", True),
    ],
)
def test_hosted_input_rejection_before_storage_request(uri, generation):
    gcs, client, blob = storage_fake()
    with pytest.raises(RunnerError):
        gcs.read_recipe(uri, generation)
    client.bucket.assert_not_called()
    blob.reload.assert_not_called()


def test_inventory_lists_only_exact_run_and_preserves_metadata():
    gcs, client, _ = storage_fake()
    obj = SimpleNamespace(
        name="products/run-a/product/image.tif",
        generation=9,
        size=40,
        crc32c="AAAAAA==",
        md5_hash=None,
    )
    client.list_blobs.return_value = SimpleNamespace(pages=iter([[obj]]), next_page_token=None)
    result = gcs.inventory("output-bucket", "products/run-a")
    assert result == [dict(name=obj.name, generation="9", size=40, crc32c="AAAAAA==", md5=None)]
    assert client.list_blobs.call_args.kwargs["prefix"] == "products/run-a/"
    assert client.list_blobs.call_args.kwargs["max_results"] == 513


def test_inventory_consumes_all_pages_and_rejects_excess():
    gcs, client, _ = storage_fake()

    def obj(i):
        return SimpleNamespace(
            name=f"products/run-a/product/image-{i}-0.tif",
            generation=i + 1,
            size=40,
            crc32c="AAAAAA==",
            md5_hash=None,
        )

    client.list_blobs.side_effect = [
        SimpleNamespace(pages=iter([[obj(0)]]), next_page_token="second"),
        SimpleNamespace(pages=iter([[obj(1)]]), next_page_token=None),
    ]
    assert len(gcs.inventory("output-bucket", "products/run-a")) == 2
    assert [call.kwargs["page_token"] for call in client.list_blobs.call_args_list] == [
        None,
        "second",
    ]
    client.list_blobs.side_effect = None
    client.list_blobs.return_value = SimpleNamespace(
        pages=iter([[obj(i) for i in range(513)]]), next_page_token=None
    )
    with pytest.raises(RunnerError) as exc:
        gcs.inventory("output-bucket", "products/run-a")
    assert exc.value.status == "artifact"


def test_storage_transport_disables_redirects_proxies_and_rechecks_deadline(
    monkeypatch,
):
    from geecomposer.runner_cloud import StorageSession
    from google.auth.transport.requests import AuthorizedSession

    clock = Clock()
    budget = clock.budget(120)
    creds = attached_identity(
        validate_policy(policy_value()), budget, metadata=MetadataFake(), environ={}
    )
    session = StorageSession(creds, budget)
    assert session.trust_env is False
    request = Mock(return_value=SimpleNamespace(status_code=302))
    monkeypatch.setattr(AuthorizedSession, "request", request)
    with pytest.raises(RunnerError, match="redirect"):
        session.request("GET", "https://storage.googleapis.com/storage/v1/b/bucket")
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.kwargs["timeout"] == 10
    clock.sleep(61)
    with pytest.raises(RunnerError) as exc:
        session.request("GET", "https://storage.googleapis.com/storage/v1/b/bucket")
    assert exc.value.status == "deadline"
    assert request.call_count == 1


def test_downloaded_key_and_impersonation_objects_are_rejected_before_ee(monkeypatch):
    from google.oauth2 import service_account
    from google.auth import impersonated_credentials

    key = service_account.Credentials(
        Mock(),
        service_account_email="worker@test-project.iam.gserviceaccount.com",
        token_uri="https://oauth2.googleapis.com/token",
    )
    impersonated = impersonated_credentials.Credentials(
        UserCredentials("TOKEN"),
        "worker@test-project.iam.gserviceaccount.com",
        ["https://www.googleapis.com/auth/cloud-platform"],
    )
    monkeypatch.setattr(
        ee, "Initialize", lambda **k: pytest.fail("unverified source chain reached EE")
    )
    for credential in (key, impersonated):
        with pytest.raises(RunnerError) as exc:
            EarthEngine(Clock().budget()).initialize(credential, "test-project")
        assert exc.value.status == "auth"


def operation(marker, identifier="server-id"):
    return {
        "name": f"projects/test-project/operations/{identifier}",
        "metadata": {"description": marker, "state": "RUNNING"},
    }


def discovery_engine(monkeypatch, pages):
    engine = EarthEngine(Clock().budget())
    engine.project, engine.credentials = "test-project", Mock()
    read = Mock(side_effect=[(200, {}, canonical(page)) for page in pages])
    monkeypatch.setattr("geecomposer.runner_cloud._read_url", read)
    return engine, read


def test_discovery_consumes_later_pages_and_cancels_actual_operation(monkeypatch):
    marker = "gc-" + "a" * 64
    engine, read = discovery_engine(
        monkeypatch,
        [
            {"operations": [operation(marker + "x", "other")], "nextPageToken": "next+/"},
            {"operations": [operation(marker)]},
        ],
    )
    name = engine.discover(marker)
    assert name == operation(marker)["name"]
    assert read.call_count == 2
    assert (
        read.call_args_list[0].args[0]
        == "https://earthengine.googleapis.com/v1/projects/test-project/operations?pageSize=100"
    )
    assert "pageToken=next%2B%2F" in read.call_args_list[1].args[0]
    assert all(c.kwargs["limit"] == 1_048_576 for c in read.call_args_list)
    cancel = Mock()
    monkeypatch.setattr(ee.data, "cancelOperation", cancel)
    engine.cancel("request-uuid", name)
    cancel.assert_called_once_with(name)
    for action in (engine.status, engine.cancel):
        with pytest.raises(RunnerError, match="discovery required"):
            action("request-uuid")


@pytest.mark.parametrize(
    "pages",
    [
        [{"operations": [operation("gc-" + "a" * 64), operation("gc-" + "a" * 64, "two")]}],
        [{"operations": [None]}],
        [{"operations": [{"name": "bad", "metadata": {}}]}],
        [{"operations": "bad"}],
        [{"operations": [operation("other", str(i)) for i in range(101)]}],
        [{"nextPageToken": "same"}, {"nextPageToken": "same"}],
        [{"nextPageToken": str(i)} for i in range(5)],
        [{"unreachable": ["region"]}],
        [{"nextPageToken": 1}],
        [
            {"operations": [operation("other")], "nextPageToken": "next"},
            {"operations": [operation("other")]},
        ],
        [],
    ],
)
def test_discovery_fails_closed_without_retry(monkeypatch, pages):
    engine, read = discovery_engine(monkeypatch, pages)
    with pytest.raises(RunnerError) as exc:
        engine.discover("gc-" + "a" * 64)
    assert exc.value.status == "uncertain"
    assert read.call_count <= 5
    assert read.call_count <= max(1, len(pages))


@pytest.mark.parametrize(
    "response", [(403, {}, b"{}"), (200, {}, b"not json"), (200, {}, b" " * 1_048_577)]
)
def test_discovery_inaccessible_malformed_or_oversized_body(monkeypatch, response):
    engine, read = discovery_engine(monkeypatch, [])
    read.side_effect = None
    read.return_value = response
    with pytest.raises(RunnerError) as exc:
        engine.discover("gc-" + "a" * 64)
    assert exc.value.status == ("auth" if response[0] == 403 else "uncertain")
    assert read.call_count == 1


def test_discovery_absence_and_deadline(monkeypatch):
    engine, read = discovery_engine(monkeypatch, [{}])
    assert engine.discover("gc-" + "a" * 64) is None
    engine.budget.end = engine.budget.work_end = 0
    with pytest.raises(RunnerError) as exc:
        engine.discover("gc-" + "a" * 64)
    assert exc.value.status == "deadline"
    assert read.call_count == 1


def test_listing_stream_caps_body_and_each_request_timeout(monkeypatch):
    from io import BytesIO

    stream = BytesIO(b"x" * 1_048_577)
    response = Mock(code=200, headers={})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read1 = stream.read
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("geecomposer.runner_cloud.build_opener", lambda *args: opener)
    engine = EarthEngine(Clock().budget())
    engine.project, engine.credentials = "test-project", Mock()
    with pytest.raises(RunnerError, match="size limit"):
        engine.discover("gc-" + "a" * 64)
    assert opener.open.call_args.kwargs["timeout"] == 10
    assert stream.tell() == 1_048_577


@pytest.mark.parametrize("accepted", [False, True])
def test_native_adapter_crash_recovery_and_discovered_cancel(monkeypatch, accepted):
    from geecomposer.runner import Runner
    from runner_fakes import World, Crash

    w = World()
    w.engine.products = 1
    engine = EarthEngine(w.clock.budget())
    engine.project, engine.credentials = "test-project", Mock()
    monkeypatch.setattr(engine, "initialize", lambda *a: None)
    monkeypatch.setattr(engine, "prepare", w.engine.prepare)
    allocate = Mock(return_value=["immutable-request"])
    monkeypatch.setattr(ee.data, "newTaskId", allocate)
    remote = []

    def submit(request, config):
        task = w.store.records["native-crash"][0]["tasks"][0]
        assert task["state"] == "submitting"
        assert task["marker"] == config["description"]
        if accepted:
            remote.append(operation(config["description"]))
        raise Crash()

    export = Mock(side_effect=submit)
    monkeypatch.setattr(ee.data, "exportImage", export)
    monkeypatch.setattr(
        "geecomposer.runner_cloud._read_url",
        lambda *a, **k: (200, {}, canonical({"operations": remote})),
    )

    def status(identifier):
        assert identifier == "server-id"
        assert w.store.records["native-crash"][0]["tasks"][0]["operation"] == operation("")["name"]
        return [{"state": "CANCELLED" if cancel.called else "RUNNING"}]

    cancel = Mock()
    monkeypatch.setattr(ee.data, "cancelOperation", cancel)
    monkeypatch.setattr(ee.data, "getTaskStatus", status)
    with pytest.raises(Crash):
        Runner(w.policy, w.store, engine, object(), engine.budget).run(w.recipe, "native-crash")
    w.clock.sleep(121)
    engine.budget = w.clock.budget()
    result = Runner(w.policy, w.store, engine, object(), engine.budget).cancel("native-crash")
    assert result["status"] == ("cancelled" if accepted else "uncertain")
    assert cancel.call_count == int(accepted)
    if accepted:
        cancel.assert_called_once_with(operation("")["name"])
    assert export.call_count == 1
    assert allocate.call_count == 1


def test_discovery_checks_deadline_between_pages(monkeypatch):
    engine, read = discovery_engine(monkeypatch, [])

    def slow(*args, **kwargs):
        engine.budget.sleep(1741)
        return 200, {}, canonical({"nextPageToken": "more"})

    read.side_effect = slow
    with pytest.raises(RunnerError) as exc:
        engine.discover("gc-" + "a" * 64)
    assert exc.value.status == "deadline"
    assert read.call_count == 1
