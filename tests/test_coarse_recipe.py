"""S03: the optional coarse recipe section, its limits and credential-free CLI use.

Validation and dry-run are offline: no credentials, connector, metadata read or EE
evaluation. The three shipped example recipes run through the real installed CLI.
"""

import copy
import json
from pathlib import Path

import pytest

from geecomposer import cli, coarse
from geecomposer.recipe import CEILINGS, RunnerError, canonical, estimate, validate_policy, validate_recipe
from runner_fakes import policy_value
from test_coarse_metadata import recipe

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "recipes"
NAMES = ("coarse_s1_monthly_2025.json", "coarse_s2_monthly_2025.json", "coarse_rao_monthly_2025.json")

pytestmark = pytest.mark.usefixtures("runner_offline")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    for key in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "home"))


def offline(args, capsys):
    code = cli.main(args, connector=lambda *a, **k: pytest.fail("offline command connected"))
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("command", ["validate", "dry-run"])
def test_examples_validate_and_dry_run_credential_free(name, command, capsys):
    path = str(EXAMPLES / name)
    args = ["validate", path] if command == "validate" else ["run", "--recipe", path, "--dry-run"]
    code, result = offline(args, capsys)
    assert code == 0 and result["status"] == ("validated" if command == "validate" else "dry_run")
    assert result["workflow"] == "coarse" and result["order"] == "spatial_mean_then_temporal"
    assert result["grid"] == dict(crs="MX-AEA", resolution_m=1000, transform=[1000.0, 0.0, 0.0, 0.0, -1000.0, 0.0])
    assert [s["max_pixels"] for s in result["stages"]] == [225, 144]
    assert all(s["best_effort"] is False for s in result["stages"])
    assert result["task_upper_bound"] == 12 and result["task_limit"] == 12 and result["active_exports"] == 2
    assert result["tasks"] is None and result["scene_count"] is None
    assert set(result["unknown"]) >= {"scene_count", "eecu_seconds", "runtime", "empty_periods"}
    assert result["output_pixels_are_not_compute_cost"] is True
    assert result["periods"] == [f"2025-{m:02d}" for m in range(1, 13)]
    assert result["task_names"][0] == "p2025_01"
    if "rao" in name:
        assert result["rao_support_m"] == 3000 and len(result["final_bands"]) == 8
        assert any("reference median fit" in p for p in result["prerequisites"])
    else:
        assert result["rao_support_m"] is None
    assert result["metadata_calls_upper_bound"] == (2 + 1 if "s1" in name else 3 + 1 + (3 if "rao" in name else 0))


def test_example_bytes_are_strict_canonical_json():
    for name in NAMES:
        value = json.loads((EXAMPLES / name).read_bytes())
        validate_recipe(copy.deepcopy(value))
        assert len(canonical(value)) < 10_000


@pytest.mark.parametrize("resolution", coarse.PRESETS)
def test_all_six_presets_resolve_to_the_python_api_grid(resolution):
    result = estimate(validate_recipe(recipe(resolution=resolution, periods=None)))
    grid = coarse.grid(resolution)
    assert result["grid"]["transform"] == list(grid.transform)
    chain = coarse.CHAINS[resolution]
    first = 225 if chain[0][1] == 100 else 81
    assert [s["max_pixels"] for s in result["stages"]] == [first] + [(b // a + 2) ** 2 for a, b in chain[1:]]
    assert [s["to_m"] for s in result["stages"]] == [b for _, b in chain]
    assert result["periods"] == ["all"] and result["task_names"] == ["pall"]


def test_local_utm_alternative_and_explicit_matching_grid():
    local = recipe(crs="EPSG:32612", resolution=500, periods=None)
    local["aoi"] = dict(type="Polygon", coordinates=[[[-110.3, 27.2], [-110.1, 27.2], [-110.1, 27.4], [-110.3, 27.4],
                                                      [-110.3, 27.2]]])
    result = estimate(validate_recipe(local))
    assert result["grid"]["crs"] == "EPSG:32612" and result["stages"][0]["max_pixels"] == 169
    same = copy.deepcopy(local)
    same["grid"] = dict(crs="EPSG:32612", transform=[500.0, 0.0, 0.0, 0.0, -500.0, 0.0])
    validate_recipe(same)


def drop(value, *path):
    target = value
    for key in path[:-1]:
        target = target[key]
    target.pop(path[-1])
    return value


def put(value, path, item):
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = item
    return value


@pytest.mark.parametrize(
    "change",
    [
        lambda v: put(v, ("coarse", "unknown"), 1),
        lambda v: put(v, ("coarse", "resolution"), 2000),
        lambda v: put(v, ("coarse", "resolution"), 1000.0),
        lambda v: put(v, ("coarse", "crs"), "EPSG:4326"),
        lambda v: put(v, ("coarse", "spatial"), "median"),
        lambda v: put(v, ("coarse", "coverage_min"), 0),
        lambda v: put(v, ("coarse", "coverage_min"), 1.5),
        lambda v: put(v, ("coarse", "coverage_min"), True),
        lambda v: put(v, ("coarse", "temporal"), ["median", "median"]),
        lambda v: put(v, ("coarse", "temporal"), ["p90"]),
        lambda v: put(v, ("coarse", "db"), True),
        lambda v: put(v, ("coarse", "index"), "nirv"),
        lambda v: put(v, ("coarse", "periods"), "weekly"),
        lambda v: put(v, ("coarse", "periods"), [dict(label="a", start="2024-12-01", end="2025-01-01")]),
        lambda v: put(v, ("coarse", "periods"), [dict(label="a", start="2025-01-01", end="2025-03-01"),
                                                 dict(label="b", start="2025-02-01", end="2025-04-01")]),
        lambda v: put(v, ("coarse", "periods"), [dict(label="A", start="2025-01-01", end="2025-02-01"),
                                                 dict(label="a", start="2025-02-01", end="2025-03-01")]),
        lambda v: put(v, ("coarse", "tiles"), dict(columns=0)),
        lambda v: put(v, ("coarse", "tiles"), dict(columns=2, cores=[[0, 2]])),
        lambda v: put(v, ("coarse", "tiles"), dict(columns=2, cores=[[0, 1], [0, 1]])),
        lambda v: put(v, ("coarse", "product"), "rao"),  # Rao needs model or fit
        lambda v: put(v, ("coarse", "rao"), {"fit": {"start": "2025-01-01", "end": "2026-01-01"}}),
        lambda v: drop(v, "mask"),
        lambda v: put(v, ("filters",), {"orbitPass": "ASCENDING"}),
        lambda v: put(v, ("reducer",), "mean"),
        lambda v: put(v, ("transform",), "ndvi"),
        lambda v: put(v, ("grid",), dict(crs="EPSG:4326", transform=[0.01, 0, -120, 0, -0.01, 35])),
        lambda v: put(v, ("grid",), dict(crs="MX-AEA", transform=[5000.0, 0.0, 0.0, 0.0, -5000.0, 0.0])),
        lambda v: put(v, ("bands",), ["B2", "B5"]),
        lambda v: put(v, ("aoi",), dict(type="Polygon", coordinates=[[[-87.0, 33.9], [-86.9, 33.9], [-86.9, 34.1],
                                                                     [-87.0, 34.1], [-87.0, 33.9]]])),
        lambda v: put(v, ("start",), "2025-02-01"),  # monthly needs a calendar-year run
        lambda v: put(v, ("workflow",), "composite"),  # coarse section without its workflow
    ],
)
def test_invalid_coarse_recipes_fail_before_remote_access(change):
    with pytest.raises(RunnerError) as err:
        validate_recipe(change(recipe()))
    assert err.value.status == "validation"


def test_sentinel1_requires_its_accepted_configuration():
    value = recipe("sentinel1_float", db=True, temporal=["mean", "median", "sd_pop"])
    result = estimate(validate_recipe(value))
    assert "VV__s_mean__t_median__db" in result["final_bands"] and "VV__s_mean__t_sd_pop__db" not in result["final_bands"]
    for change in (lambda v: put(v, ("filters",), {"polarizations": ["VV", "VH"]}),
                   lambda v: put(v, ("filters", "instrumentMode"), "EW"),
                   lambda v: put(v, ("preprocess",), "gamma_map"),
                   lambda v: put(v, ("mask",), "s2_cloud_score_plus"),
                   lambda v: put(v, ("coarse", "index"), "ndvi")):
        with pytest.raises(RunnerError):
            validate_recipe(change(recipe("sentinel1_float")))


def test_final_band_count_is_bounded_separately_from_input_bands():
    full = estimate(validate_recipe(recipe(temporal=list(coarse.TEMPORAL), index="ndvi")))
    assert len(full["final_bands"]) == 4 * 6 + 2 + 2 <= 32
    assert validate_recipe(recipe())["limits"]["bands"] == CEILINGS["bands"] == 12


@pytest.mark.parametrize("outputs, admitted", [(11, False), (12, True), (13, None)])
def test_twelve_output_ceiling_with_monthly_periods(outputs, admitted):
    value = recipe()
    value["limits"] = {"outputs": outputs}
    if admitted is None:  # above the ceiling itself
        with pytest.raises(RunnerError):
            validate_recipe(value)
    elif admitted:
        assert validate_recipe(value)["limits"]["outputs"] == 12
    else:  # twelve declared months cannot fit eleven outputs, whatever turns out empty
        with pytest.raises(RunnerError) as err:
            validate_recipe(value)
        assert str(err.value) == "declared periods times tile cores exceed the output ceiling"
    assert CEILINGS["outputs"] == 12 and CEILINGS["active_exports"] == 2


def test_lower_policy_limits_bind_and_tiles_multiply_periods():
    policy = policy_value()
    policy["limits"] = {"outputs": 6}
    value = recipe()
    value["destination"] = dict(bucket="output-bucket", prefix="products/coarse")
    with pytest.raises(RunnerError):
        validate_recipe(value, validate_policy(policy))
    six = [dict(label=f"m{m}", start=f"2025-{m:02d}-01", end=f"2025-{m + 1:02d}-01") for m in range(1, 7)]
    validate_recipe(put(copy.deepcopy(value), ("coarse", "periods"), six), validate_policy(policy))
    tiled = put(copy.deepcopy(value), ("coarse", "periods"), six[:3])
    put(tiled, ("coarse", "tiles"), dict(columns=2, rows=1))
    assert estimate(validate_recipe(tiled, validate_policy(policy)))["task_upper_bound"] == 6
    put(tiled, ("coarse", "tiles"), dict(columns=2, rows=2))
    with pytest.raises(RunnerError):
        validate_recipe(tiled, validate_policy(policy))
    put(tiled, ("coarse", "tiles"), dict(columns=2, rows=2, cores=[[0, 0], [1, 1]]))
    assert estimate(validate_recipe(tiled, validate_policy(policy)))["task_names"][:2] == ["pm1_r00c00", "pm1_r01c01"]


def test_national_reference_fit_respects_the_calibration_ceiling_but_a_model_does_not_need_one():
    fit = recipe(product="rao", rao={"fit": {"start": "2025-01-01", "end": "2026-01-01"}})
    with pytest.raises(RunnerError) as err:
        validate_recipe(fit)
    assert str(err.value) == "coarse reference fit exceeds the calibration ceiling"
    model = dict(bands=["B8__s_mean__t_median", "B3__s_mean__t_median", "B2__s_mean__t_median", "B4__s_mean__t_median"],
                 means=[0.3, 0.07, 0.05, 0.06], scales=[0.1, 0.02, 0.02, 0.03],
                 loadings=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], retained_fraction=1.0,
                 n_sigmas=6.0, sample_count=10, decimal_places=None, calibration="synthetic-test-model")
    supplied = recipe(product="rao", rao={"model": model})
    supplied["bands"] = ["B8", "B3", "B2", "B4"]
    result = estimate(validate_recipe(supplied))
    assert result["final_bands"][4:] == [b for b in model["bands"]] and "supplied frozen" in result["prerequisites"][1]
    supplied["bands"] = ["B2", "B3", "B4", "B8"]  # select must follow the model's order
    with pytest.raises(RunnerError):
        validate_recipe(supplied)
    one = copy.deepcopy(model)
    one.update(bands=["B2__s_mean__t_median"], means=[0.05], scales=[0.02], loadings=[[1]])
    with pytest.raises(RunnerError):
        validate_recipe(put(recipe(product="rao", rao={"model": one}), ("bands",), ["B2"]))


def test_existing_recipes_keep_their_shape():
    from runner_fakes import recipe_value

    value = recipe_value()
    validated = validate_recipe(value)
    assert "coarse" not in validated and validated["grid"] == value["grid"]
    with pytest.raises(RunnerError):
        validate_recipe(drop(copy.deepcopy(value), "grid"))


# ------------------------------------------------------------------ coarse Rao window ceiling (S03-F2)

SUPPLIED_MODEL = dict(
    bands=["B8__s_mean__t_median", "B3__s_mean__t_median", "B2__s_mean__t_median", "B4__s_mean__t_median"],
    means=[0.3, 0.07, 0.05, 0.06], scales=[0.1, 0.02, 0.02, 0.03],
    loadings=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], retained_fraction=1.0,
    n_sigmas=6.0, sample_count=10, decimal_places=None, calibration="synthetic-test-model",
)


def coarse_rao(path):
    """The shipped regional Rao example (reference fit), or it with a supplied model."""
    value = json.loads((EXAMPLES / "coarse_rao_monthly_2025.json").read_bytes())
    if path == "model":
        value["coarse"]["rao"] = {"model": copy.deepcopy(SUPPLIED_MODEL)}
        value["bands"] = ["B8", "B3", "B2", "B4"]
    return value


def ceiling_files(tmp_path, value, ceiling, window):
    """Recipe file and CLI options with the window ceiling imposed by the recipe or a policy."""
    options = []
    if ceiling == "recipe":
        value["limits"] = {"window": window}
    else:
        policy = policy_value()
        policy["limits"] = {"window": window}
        (tmp_path / "policy.json").write_bytes(canonical(policy))
        options = ["--policy", str(tmp_path / "policy.json")]
        value["destination"] = dict(bucket="output-bucket", prefix="products/coarse")
    (tmp_path / "recipe.json").write_bytes(canonical(value))
    return options, str(tmp_path / "recipe.json")


@pytest.mark.parametrize("command", ["validate", "dry-run"])
@pytest.mark.parametrize("ceiling", ["recipe", "policy"])
@pytest.mark.parametrize("path", ["fit", "model"])
@pytest.mark.parametrize("window", [1, 3])
def test_coarse_rao_needs_a_window_ceiling_admitting_its_3x3_support(tmp_path, capsys, window, path, ceiling, command):
    options, recipe_path = ceiling_files(tmp_path, coarse_rao(path), ceiling, window)
    args = ["validate", recipe_path] if command == "validate" else ["run", "--recipe", recipe_path, "--dry-run"]
    code, result = offline(options + args, capsys)
    if window < 3:
        assert (code, result) == (2, {"message": "coarse Rao 3x3 support exceeds the window ceiling",
                                      "status": "validation"})
    else:
        assert code == 0 and result["limits"]["window"] == 3 and result["rao_support_m"] == 3000


@pytest.mark.parametrize("ceiling", ["recipe", "policy"])
def test_window_ceiling_one_still_admits_coarse_composites(tmp_path, capsys, ceiling):
    options, recipe_path = ceiling_files(tmp_path, recipe(), ceiling, 1)
    code, result = offline(options + ["validate", recipe_path], capsys)
    assert code == 0 and result["limits"]["window"] == 1 and result["rao_support_m"] is None
    assert validate_recipe(coarse_rao("model"))["limits"]["window"] == CEILINGS["window"] == 7
