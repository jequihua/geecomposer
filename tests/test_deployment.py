"""Operator tool tests never access cloud identity or run an external command."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from geecomposer import deployment as deploy
from geecomposer.recipe import RunnerError, canonical, digest
from runner_fakes import policy_value, recipe_value

pytestmark = pytest.mark.usefixtures("runner_offline")


@pytest.fixture
def policy():
    value = policy_value()
    value.update(
        mode="hosted",
        project="cohort-project",
        service_account="worker@cohort-project.iam.gserviceaccount.com",
        image_version="release-001",
    )
    return deploy.hosted_policy(value)


@pytest.fixture
def template():
    return {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {
            "name": "${JOB}",
            "namespace": "${PROJECT_NUMBER}",
            "labels": {"cloud.googleapis.com/location": "${REGION}"},
            "annotations": {"geecomposer-policy-sha256": "${POLICY_SHA256}"},
        },
        "spec": {
            "template": {
                "spec": {
                    "taskCount": 1,
                    "parallelism": 1,
                    "template": {
                        "spec": {
                            "serviceAccountName": "${RUNTIME_ACCOUNT}",
                            "maxRetries": 0,
                            "timeoutSeconds": "2100",
                            "containers": [
                                {
                                    "image": "${IMAGE}",
                                    "args": ["--help"],
                                    "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                                }
                            ],
                        }
                    },
                }
            }
        },
    }


IMAGE = "europe-west1-docker.pkg.dev/cohort-project/rasters/worker@sha256:" + "a" * 64
URI = "gs://input-bucket/recipes/immutable.json"


def prepared(policy, template, **kwargs):
    options = dict(
        image=IMAGE,
        project_number="123456789012",
        region="europe-west1",
        job="cohort-worker",
        action="run",
        identifier_value="run-001",
        recipe=recipe_value(),
        uri=URI,
    )
    options.update(kwargs)
    return deploy.plan(policy, template, **options)


def receipt(policy):
    from geecomposer.recipe import validate_recipe

    return dict(
        uri=URI,
        generation=123,
        recipe_sha256=digest(validate_recipe(recipe_value(), policy)),
        policy_sha256=digest(policy),
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("mode", "local"),
        ("project", "geecomposer-example"),
        ("service_account", "worker@geecomposer-example.iam.gserviceaccount.com"),
        ("inputs", [{"bucket": "geecomposer-example-input", "prefix": "recipes"}]),
        ("outputs", [{"bucket": "geecomposer-example-output", "prefix": "products"}]),
        ("control", {"bucket": "geecomposer-example-control", "prefix": "runs"}),
        ("image_version", "replace-with-reviewed-image-digest"),
        ("image_version", "__VERSION__"),
        ("image_version", "${VERSION}"),
        ("image_version", "<VERSION>"),
        ("image_version", "{{VERSION}}"),
        ("inputs", [{"bucket": "input-bucket", "prefix": "recipes/__NAME__"}]),
        ("project", "bad project"),
        ("service_account", "not-an-account"),
        ("image_version", None),
        ("outputs", [{"bucket": "bad_bucket", "prefix": "products"}]),
        ("outputs", [{"bucket": "output-bucket", "prefix": "../products"}]),
        ("outputs", [{"bucket": "output-bucket", "prefix": "*"}]),
    ],
)
def test_build_policy_rejects_placeholder_or_permissive_input(policy, key, value):
    policy[key] = value
    with pytest.raises(RunnerError):
        deploy.hosted_policy(policy)


def test_policy_preparation_explicit_output_and_no_overwrite(policy, tmp_path, capsys):
    source, output = tmp_path / "policy with spaces &;.json", tmp_path / "validated.json"
    source.write_bytes(canonical(policy))
    args = ["--mode", "policy", "--policy", str(source), "--output", str(output)]
    assert deploy.main(args) == 0
    assert json.loads(output.read_bytes()) == policy
    assert deploy.main(args) == 2
    assert json.loads(output.read_bytes()) == policy


def test_upload_is_generation_conditional_and_receipt_bound(policy):
    client = Mock()
    blob = client.return_value.bucket.return_value.blob.return_value
    blob.generation = 123
    result = deploy.upload(policy, recipe_value(), URI, client_factory=client)
    assert result == receipt(policy)
    assert blob.upload_from_string.call_args.kwargs == dict(
        content_type="application/json", if_generation_match=0, retry=None, timeout=10
    )
    with pytest.raises(RunnerError):
        deploy.upload(
            policy, recipe_value(), "gs://other-bucket/recipes/a.json", client_factory=client
        )
    assert blob.upload_from_string.call_count == 1


def test_lost_upload_response_never_retries_or_exposes_diagnostics(policy):
    client = Mock()
    blob = client.return_value.bucket.return_value.blob.return_value
    blob.upload_from_string.side_effect = TimeoutError("PRIVATE raw token")
    with pytest.raises(RunnerError) as exc:
        deploy.upload(policy, recipe_value(), URI, client_factory=client)
    assert exc.value.status == "uncertain"
    assert "PRIVATE" not in str(exc.value)
    assert blob.upload_from_string.call_count == 1


@pytest.mark.parametrize("mode", ["plan", "render"])
def test_plan_render_have_no_external_effects(
    policy, template, tmp_path, monkeypatch, capsys, mode
):
    p, r, t = [
        tmp_path / name for name in ("policy &;.json", "recipe with spaces.json", "job.yaml")
    ]
    for path, value in ((p, policy), (r, recipe_value()), (t, template)):
        path.write_bytes(canonical(value))
    monkeypatch.setattr(deploy, "upload", lambda *a: pytest.fail("default uploaded"))
    monkeypatch.setattr(deploy, "execute", lambda *a: pytest.fail("default executed"))
    assert (
        deploy.main(
            [
                "--policy",
                str(p),
                "--recipe",
                str(r),
                "--job-template",
                str(t),
                "--image",
                IMAGE,
                "--project-number",
                "123456789012",
                "--region",
                "europe-west1",
                "--job",
                "cohort-worker",
                "--run-id",
                "run-001",
                "--uri",
                URI,
            ]
            + (["--mode", "render"] if mode == "render" else [])
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    if mode == "render":
        assert result == prepared(policy, template)["job"]
        return
    assert result["mode"] == "plan"
    assert result["ready_to_execute"] is False
    assert "coordinates" not in canonical(result).decode()
    monkeypatch.undo()
    with pytest.raises(RunnerError):
        deploy.execute(result, command=lambda *a, **k: pytest.fail("unpinned execution"))


@pytest.mark.parametrize(
    "changes",
    [
        {"identifier_value": "run &whoami"},
        {"job": "worker;echo"},
        {"region": "europe west1"},
        {"image": "repo:latest"},
        {"uri": "gs://input-bucket/recipes/a#7"},
        {"uri": "gs://input-bucket/recipes/../a"},
    ],
)
def test_plan_rejects_untrusted_command_values(policy, template, changes):
    with pytest.raises(RunnerError):
        prepared(policy, template, **changes)


@pytest.mark.parametrize(
    "key,value",
    [
        ("generation", True),
        ("generation", 0),
        ("policy_sha256", "wrong"),
        ("recipe_sha256", "wrong"),
        ("uri", "gs://input-bucket/recipes/different.json"),
    ],
)
def test_receipt_cannot_rebind_input_or_policy(policy, template, key, value):
    value_receipt = receipt(policy)
    value_receipt[key] = value
    with pytest.raises(RunnerError):
        prepared(policy, template, receipt=value_receipt)


def test_execute_uses_json_flags_and_verifies_deployed_binding(policy, template):
    result = prepared(policy, template, receipt=receipt(policy))
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] <= 60
        flags = json.loads(Path(kwargs["cwd"], argv[-1].split("=", 1)[1]).read_bytes())
        assert flags["--project"] == policy["project"]
        if argv[3] == "describe":
            return SimpleNamespace(returncode=0, stdout=json.dumps(result["job"]))
        assert flags["--args"] == [
            "run",
            "--run-id",
            "run-001",
            "--recipe",
            URI,
            "--recipe-generation",
            "123",
        ]
        return SimpleNamespace(returncode=0, stdout="{}")

    assert deploy.execute(result, command=command)["status"] == "submitted"
    assert [a[3] for a in calls] == ["describe", "execute"]


@pytest.mark.parametrize(
    "failure", ["read-error", "write-error", "changed-image", "changed-policy", "extra-env"]
)
def test_execution_failures_stop_without_blind_retry(policy, template, failure):
    result = prepared(policy, template, receipt=receipt(policy))
    live = copy.deepcopy(result["job"])
    container = live["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    if failure == "changed-image":
        container["image"] = IMAGE[:-1] + "b"
    if failure == "changed-policy":
        live["metadata"]["annotations"]["geecomposer-policy-sha256"] = "different"
    if failure == "extra-env":
        container["env"] = [{"name": "PYTHONPATH", "value": "/work"}]
    command = Mock(
        side_effect=[
            SimpleNamespace(returncode=int(failure == "read-error"), stdout=json.dumps(live)),
            SimpleNamespace(returncode=1, stdout="PRIVATE"),
        ]
    )
    with pytest.raises(RunnerError):
        deploy.execute(result, command=command)
    assert command.call_count == (2 if failure == "write-error" else 1)


@pytest.mark.parametrize("action", ["status", "cancel", "resume"])
def test_recovery_commands_need_no_replacement_recipe(policy, template, action):
    result = prepared(policy, template, action=action, recipe=None, uri=None)
    assert result["ready_to_execute"] is True
    assert result["runner_args"] == [action, "--run-id", "run-001"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("project", "test-project"),
        ("project", "research-example-project"),
        ("service_account", "test-worker@test-project.iam.gserviceaccount.com"),
        ("inputs", [{"bucket": "research-test-input", "prefix": "test_recipes/example"}]),
        ("outputs", [{"bucket": "research-example-output", "prefix": "test_products"}]),
        ("control", {"bucket": "research-test-control", "prefix": "test_runs"}),
        ("image_version", "test_release-example-001"),
    ],
)
def test_legitimate_names_are_not_substring_placeholders(policy, key, value):
    policy[key] = value
    assert deploy.hosted_policy(policy)[key] == value


def test_shipped_template_conforms_to_helper_contract(policy, template):
    path = Path(__file__).resolve().parents[1] / "deploy/cloud_run/job.yaml"
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pytest.skip("repository-owned job template absent from standalone package")
    # Both pass the same job_spec validation used by package orchestration tests.
    assert prepared(policy, json.loads(contents)) == prepared(policy, template)
