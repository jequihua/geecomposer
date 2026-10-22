import copy
import json
import socket

import ee
import google.auth
import pytest

from geecomposer import cli
from geecomposer.recipe import (
    CEILINGS,
    RunnerError,
    canonical,
    estimate,
    parse_json,
    validate_policy,
    validate_recipe,
)
from runner_fakes import World, policy_value, recipe_value

pytestmark = pytest.mark.usefixtures("runner_offline")


@pytest.fixture(autouse=True)
def deny_offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("offline command attempted credentials, EE initialization or network")

    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(google.auth, "default", deny)
    monkeypatch.setattr(ee, "Initialize", deny)
    monkeypatch.setattr(ee, "Authenticate", deny)
    monkeypatch.setattr(ee.data, "get_persistent_credentials", deny)


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1e999}',
        b"\xff",
        b'{"a":}',
        b"[" * 2000,
        b" " * (CEILINGS["recipe_bytes"] + 1),
    ],
    ids=[
        "duplicate",
        "nan",
        "infinity",
        "overflow",
        "utf8",
        "syntax",
        "depth",
        "bytes",
    ],
)
def test_strict_json_rejections(data):
    with pytest.raises(RunnerError) as exc:
        parse_json(data)
    assert exc.value.status == "validation"


@pytest.mark.parametrize(
    "path,value",
    [
        (("version",), True),
        (("version",), 2),
        (("project",), "evil"),
        (("python",), "eval(SECRET)"),
        (("expression",), "vh/vv"),
        (("asset",), "users/private"),
        (("dataset",), "https://example.com"),
        (("workflow",), []),
        (("start",), "2024-02-30"),
        (("start",), "2024-1-01"),
        (("end",), "2023-01-01"),
        (("bands",), ["B.*"]),
        (("bands",), ["B4", "B4"]),
        (("bands",), [True]),
        (("aoi",), "secret.geojson"),
        (("aoi",), {"type": "Point", "coordinates": [0, 0]}),
        (("aoi", "url"), "https://example.com"),
        (("aoi", "coordinates"), [[[0, 0], [1, 0], [0, 1], [1, 1], [0, 0]]]),
        (("aoi", "coordinates"), [[[True, 0], [1, 0], [0, 1], [True, 0]]]),
        (("aoi", "coordinates"), [[[0, 0], [1, 0], [0, 1], [0, 2]]]),
        (("aoi", "coordinates"), [[[179, 0], [-179, 0], [-179, 1], [179, 0]]]),
        (("grid", "crs"), "AUTO:123"),
        (("grid", "transform"), [1, 0, 0, 0, 0, 0]),
        (("grid", "transform"), [True, 0, 0, 0, -1, 0]),
        (("grid", "transform"), [float("inf"), 0, 0, 0, -1, 0]),
        (("grid", "url"), "https://example.com"),
        (("destination", "bucket"), "gs://output-bucket"),
        (("destination", "prefix"), "products/../private"),
        (("limits",), {"scenes": 1001}),
        (("limits",), {"pixels": True}),
        (("limits",), {"window": 8}),
        (("limits",), {"active_exports": 3}),
        (("limits",), {"outputs": 13}),  # D126/A17: the ceiling is 12
        (("limits",), {"calibration_pixels": 1000001}),
        (("limits",), {"unknown": 1}),
        (("preprocess",), "gamma_map"),
        (("transform",), "vh_vv_ratio"),
        (("years",), [2020]),
        (("filters",), {"orbitPass": "ASCENDING"}),
    ],
)
def test_recipe_field_trust_boundary(path, value):
    recipe = recipe_value()
    target = recipe
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(RunnerError):
        validate_recipe(recipe)


def test_budget_lowering_and_estimates():
    policy = validate_policy(policy_value())
    value = recipe_value()
    value["limits"] = {"scenes": 10, "pixels": 1000}
    recipe = validate_recipe(value, policy)
    result = estimate(recipe)
    assert result["tasks"] == 1
    assert result["pixel_upper_bound"] == 196
    assert result["scene_count"] is None
    assert result["scene_limit"] == 10
    assert result["online_preflight_required"] is True
    assert result["limits"]["active_exports"] == 2
    value["grid"]["crs"] = "EPSG:32631"
    result = estimate(validate_recipe(value, policy))
    assert result["pixel_upper_bound"] is None
    assert result["online_preflight_required"] is True


@pytest.mark.parametrize("workflow", ["yearly", "rao", "seasonal_nirv"])
def test_workflow_options(workflow):
    recipe = recipe_value()
    recipe["workflow"] = workflow
    if workflow == "yearly":
        recipe.pop("start")
        recipe.pop("end")
        recipe["years"] = [2020, 2021]
    elif workflow == "rao":
        recipe["rao"] = dict(normalization="gn", window_size=7, decimal_places=None)
    else:
        recipe.pop("transform")
        recipe["seasonal"] = dict(reference_start="2020-01-01", reference_end="2024-02-01")
    valid = validate_recipe(recipe)
    assert valid["workflow"] == workflow
    if workflow == "rao":
        assert estimate(valid)["pairs_per_pixel"] == 1176
    invalid = copy.deepcopy(recipe)
    if workflow == "yearly":
        invalid["years"] = [True]
    elif workflow == "rao":
        invalid["rao"]["window_size"] = 4
    else:
        invalid["seasonal"]["scene_outputs"] = 1
    with pytest.raises(RunnerError):
        validate_recipe(invalid)


def test_policy_denies_overrides_and_neighbor_prefix():
    policy = validate_policy(policy_value())
    for dest in (
        dict(bucket="other-bucket", prefix="products"),
        dict(bucket="output-bucket", prefix="products-evil"),
    ):
        with pytest.raises(RunnerError) as exc:
            validate_recipe(dict(recipe_value(), destination=dest), policy)
        assert exc.value.status == "auth"
    for key, value in (
        ("limits", {"pixels": 10000001}),
        ("service_account", "person@example.org"),
        ("credential_path", "key.json"),
        ("poll_seconds", 1),
    ):
        with pytest.raises(RunnerError):
            validate_policy(dict(policy_value(), **{key: value}))


@pytest.mark.parametrize("command", ["validate", "dry-run"])
def test_real_cli_offline_forbids_credentials_and_sockets(tmp_path, capsys, command):
    path = tmp_path / "recipe.json"
    path.write_bytes(canonical(recipe_value()))
    args = (
        ["validate", str(path)]
        if command == "validate"
        else ["run", "--recipe", str(path), "--dry-run"]
    )

    def connector(*args):
        pytest.fail("offline command connected")

    assert cli.main(args, connector=connector) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == ("validated" if command == "validate" else "dry_run")
    assert "coordinates" not in output


def test_cli_real_runner_orchestration(tmp_path, capsys):
    w = World()
    recipe_path, policy_path = tmp_path / "recipe.json", tmp_path / "policy.json"
    recipe_path.write_bytes(canonical(recipe_value()))
    policy_path.write_bytes(canonical(policy_value()))
    args = [
        "--policy",
        str(policy_path),
        "run",
        "--recipe",
        str(recipe_path),
        "--run-id",
        "cli",
    ]

    def connector(p, b):
        return w.store, w.engine, object()

    assert cli.main(args, connector=connector, budget_factory=w.clock.budget) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded"
    assert len(result["outputs"]) == 3
    assert cli.main(args, connector=connector, budget_factory=w.clock.budget) == 5
    assert json.loads(capsys.readouterr().out)["status"] == "conflict"
    for cmd in ("status", "resume", "cancel"):
        assert (
            cli.main(
                ["--policy", str(policy_path), cmd, "--run-id", "cli"],
                connector=connector,
                budget_factory=w.clock.budget,
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


@pytest.mark.parametrize(
    "args",
    [
        ["--policy", "SECRET.json", "status", "--run-id", "x"],
        ["run", "--recipe", "SECRET.json", "--run-id", "x"],
        ["run", "--recipe", "gs://input-bucket/recipes/a", "--run-id", "x"],
        [
            "run",
            "--recipe",
            "gs://input-bucket/recipes/a",
            "--recipe-generation",
            "1",
            "--project",
            "SECRET",
        ],
        ["validate", "SECRET.json"],
    ],
)
def test_bundled_hosted_policy_cannot_be_downgraded(tmp_path, monkeypatch, capsys, args):
    path = tmp_path / "runtime_policy.json"
    path.write_bytes(canonical(dict(policy_value(), mode="hosted")))
    monkeypatch.setattr(cli, "BUNDLED_POLICY", path)

    def connector(*args):
        pytest.fail("invalid hosted input reached remote boundary")

    assert cli.main(args, connector=connector) == 2
    assert "SECRET" not in capsys.readouterr().out


def test_cli_errors_are_redacted(tmp_path, capsys):
    path = tmp_path / "recipe.json"
    path.write_text('{"SECRET": "token"}')
    assert cli.main(["validate", str(path)]) == 2
    assert "SECRET" not in capsys.readouterr().out


def test_hosted_cli_persists_pinned_source_and_uses_bundled_settings(tmp_path, monkeypatch, capsys):
    w = World()
    w.policy["mode"] = "hosted"
    path = tmp_path / "runtime_policy.json"
    path.write_bytes(canonical(w.policy))
    monkeypatch.setattr(cli, "BUNDLED_POLICY", path)
    source = {"uri": "gs://input-bucket/recipes/test.json", "generation": 7}

    def read_recipe(uri, generation):
        assert uri == source["uri"]
        assert generation == 7
        return recipe_value(), source

    w.store.read_recipe = read_recipe

    def connector(policy, budget):
        assert policy == w.policy
        return w.store, w.engine, object()

    result = cli.main(
        [
            "run",
            "--recipe",
            source["uri"],
            "--recipe-generation",
            "7",
            "--run-id",
            "pinned",
        ],
        connector=connector,
        budget_factory=w.clock.budget,
    )
    assert result == 0
    state, _ = w.store.records["pinned"]
    assert state["source"] == source
    assert state["principal"] == w.policy["service_account"]
    assert state["project"] == w.policy["project"]
    assert "coordinates" not in capsys.readouterr().out
    assert cli.main(["--credential", "SECRET"]) == 2
    assert "SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("ceiling", [10, 4, 2])
@pytest.mark.parametrize("command", ["validate", "dry-run"])
def test_seasonal_scene_warning_uses_effective_ceiling(enabled, ceiling, command, tmp_path, capsys):
    value = recipe_value()
    value.update(workflow="seasonal_nirv", seasonal={})
    value.pop("transform")
    if enabled is not None:
        value["seasonal"]["scene_outputs"] = enabled
    policy = policy_value()
    policy["limits"] = {"outputs": ceiling}
    recipe_path, policy_path = tmp_path / "recipe.json", tmp_path / "policy.json"
    recipe_path.write_bytes(canonical(value))
    policy_path.write_bytes(canonical(policy))
    args = ["--policy", str(policy_path)] + (
        ["validate", str(recipe_path)]
        if command == "validate"
        else ["run", "--recipe", str(recipe_path), "--dry-run"]
    )
    assert cli.main(args, connector=lambda *a: pytest.fail("offline connected")) == 0
    result = json.loads(capsys.readouterr().out)
    info = result
    assert info["tasks"] == (2 if enabled is False else None)
    if enabled is False:
        assert info["warnings"] == []
    else:
        (warning,) = info["warnings"]
        assert warning["code"] == "seasonal_scene_output_limit"
        assert warning["fixed_outputs"] == 2
        assert warning["allowed_scene_outputs"] == ceiling - 2
        assert "expensive" in warning["message"]
        assert "scene_outputs=false" in warning["message"]
    policy["limits"]["outputs"] = 1
    with pytest.raises(RunnerError):
        validate_recipe(value, validate_policy(policy))


@pytest.mark.parametrize(
    "failure,code", [("refresh", 3), ("permission", 3), ("submit-auth", 3), ("unavailable", 6)]
)
def test_native_discovery_auth_reaches_cli_without_reclassification(
    tmp_path, monkeypatch, capsys, failure, code
):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock
    from geecomposer.runner_cloud import EarthEngine, attached_identity
    from test_runner_cloud import MetadataFake

    w = World()
    w.engine.products = 1
    budget = w.clock.budget()
    meta = MetadataFake()
    credentials = attached_identity(w.policy, budget, metadata=meta, environ={})
    engine = EarthEngine(budget)
    engine.project, engine.credentials = w.policy["project"], credentials
    monkeypatch.setattr(engine, "initialize", lambda *a: None)
    monkeypatch.setattr(engine, "prepare", w.engine.prepare)
    monkeypatch.setattr(ee.data, "newTaskId", lambda: ["original-request"])

    def submit(*args):
        if failure == "submit-auth":
            raise RunnerError("auth", "attached identity rejected")
        if failure == "refresh":
            meta.email = "PRIVATE_CHANGED_PRINCIPAL"
            credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=1
            )
        raise TimeoutError("PRIVATE lost acknowledgement")

    export = Mock(side_effect=submit)
    monkeypatch.setattr(ee.data, "exportImage", export)
    if failure == "permission":
        transport = Mock(return_value=(403, {}, b"PRIVATE"))
    else:
        transport = Mock(return_value=(503, {}, b"PRIVATE"))
    monkeypatch.setattr("geecomposer.runner_cloud._read_url", transport)
    recipe_path, policy_path = tmp_path / "recipe.json", tmp_path / "policy.json"
    recipe_path.write_bytes(canonical(recipe_value()))
    policy_path.write_bytes(canonical(w.policy))
    result = cli.main(
        [
            "--policy",
            str(policy_path),
            "run",
            "--recipe",
            str(recipe_path),
            "--run-id",
            "auth-discovery",
        ],
        connector=lambda *a: (w.store, engine, credentials),
        budget_factory=lambda *a: budget,
    )
    assert result == code
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    state, _ = w.store.records["auth-discovery"]
    assert state["status"] == ("auth" if code == 3 else "uncertain")
    assert state["remote_pending"] is True
    assert state["owner"] is None
    assert state["tasks"][0]["request_id"] == "original-request"
    assert export.call_count == 1
    if failure in ("refresh", "submit-auth"):
        transport.assert_not_called()
