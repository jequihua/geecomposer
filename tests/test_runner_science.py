from datetime import datetime, timezone
from unittest.mock import Mock
from geecomposer.recipe import canonical

import ee
import pytest

from geecomposer.recipe import RunnerError, validate_recipe
from geecomposer.runner_cloud import EarthEngine
from geecomposer.runner_science import config_fingerprint, prepare
from runner_fakes import Clock, recipe_value

pytestmark = pytest.mark.usefixtures("runner_offline")


# The bounds response is in the grid's full affine (pixel) coordinates: recipe_value's
# AOI spans columns and rows 0..10 of its grid.
BOUNDS = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]


def evaluation(monkeypatch, values):
    pending = list(values)

    def compute(*args, **kwargs):
        assert pending, "unexpected remote evaluation"
        return pending.pop(0)

    monkeypatch.setattr(ee.data, "computeValue", compute)
    return pending


@pytest.mark.parametrize("workflow", ["composite", "yearly", "raw", "gn", "ratio_gamma"])
def test_public_science_paths_build_real_native_configs(real_ee, monkeypatch, workflow):
    value = recipe_value()
    answers = [BOUNDS]
    if workflow == "yearly":
        value.update(workflow="yearly", years=[2022, 2023])
        value.pop("start")
        value.pop("end")
        answers.extend([1, ["scene_a"], 1, ["scene_b"]])
    else:
        answers.extend([1, ["scene_a"]])
    if workflow in ("raw", "gn"):
        value.update(workflow="rao", rao={"normalization": workflow, "window_size": 1})
        if workflow == "gn":
            answers.extend(
                [
                    9,
                    {"ndvi_count": 9, "ndvi_mean": 0.5, "ndvi_min": 0, "ndvi_max": 1},
                    {"c_0_0": 2.25},
                ]
            )
    if workflow == "ratio_gamma":
        value.update(
            dataset="sentinel1_float",
            bands=["VV", "VH"],
            preprocess="gamma_map",
            transform="vh_vv_ratio",
        )
    pending = evaluation(monkeypatch, answers)
    monkeypatch.setattr(
        ee.batch.Task,
        "start",
        lambda *a: pytest.fail("task started during preparation"),
    )
    recipe = validate_recipe(value)
    tasks, provenance = prepare(recipe, "products/run", Clock().budget())
    assert pending == []
    assert len(tasks) == (2 if workflow == "yearly" else 1)
    assert provenance["pixels_upper_bound"] == 196
    assert provenance["dtype"] == "float64"
    for task in tasks:
        config = task["config"]
        assert config["fileExportOptions"]["cloudStorageDestination"]["bucket"] == "output-bucket"
        assert config["fileExportOptions"]["cloudStorageDestination"]["filenamePrefix"].startswith(
            "products/run/"
        )
        assert config["maxPixels"] == {"value": "1000000" if workflow == "gn" else "10000000"}
        assert config["workloadTag"] == ""
        assert task["fingerprint"] == config_fingerprint(config)
        serialized = config["expression"].serialize()
        assert "Image.toDouble" in serialized
        assert "explicit_snapshot" in serialized
        assert "scene_a" in serialized or "scene_b" in serialized
    if workflow == "gn":
        assert provenance["calibration_sha256"]
    if workflow == "ratio_gamma":
        assert "Image.expression" in serialized or "Image.parseExpression" in serialized


def test_seasonal_adapter_uses_real_public_workflow_and_preserves_private_summaries(
    real_ee, monkeypatch
):
    value = recipe_value()
    value.update(workflow="seasonal_nirv", seasonal={"window_size": 1})
    value.pop("transform")
    times = [int(datetime(2024, 1, day, tzinfo=timezone.utc).timestamp() * 1000) for day in (1, 2)]
    metadata = [dict(id=f"scene_{i}", time=times[i], spacecraft="S2A", orbit=1) for i in range(2)]
    answers = [BOUNDS, 2, ["scene_0", "scene_1"], 2, metadata, {"total": 9}]
    for ndvi in (0.1, 0.9):
        answers.extend(
            [
                {"ndvi_count": 9, "ndvi_median": ndvi},
                {"nirv_count": 9, "nirv_mean": 0.5, "nirv_min": 0, "nirv_max": 1},
                {"c_0_0": 2.25},
                {"rao_gn_count": 9, "rao_gn_median": 0.01},
            ]
        )
    pending = evaluation(monkeypatch, answers)
    tasks, provenance = prepare(validate_recipe(value), "products/run", Clock().budget())
    assert pending == []
    assert [task["name"] for task in tasks] == [
        "seasonal_mean",
        "observation_count",
        "scene_0000",
    ]
    assert provenance["seasonal"]["world_cover_dataset"] == "ESA/WorldCover/v200"
    assert provenance["seasonal"]["comparison"] == "gt"
    assert provenance["seasonal"]["quantile"] == 0.9
    assert provenance["seasonal"]["decimal_places"] == 2
    assert provenance["seasonal"]["selected_observations"] == 1
    assert len(provenance["scene_summaries"]) == 2
    assert provenance["scene_summaries"][1]["calibration_sha256"]


@pytest.mark.parametrize(
    "count,ids,status",
    [
        (0, None, "no_data"),
        (1001, None, "validation"),
        (1, ["scene"] * 1001, "validation"),
    ],
)
def test_empty_or_growing_collection_never_truncates(real_ee, monkeypatch, count, ids, status):
    answers = [BOUNDS, count] + ([ids] if ids else [])
    evaluation(monkeypatch, answers)
    with pytest.raises(RunnerError) as exc:
        prepare(validate_recipe(recipe_value()), "products/run", Clock().budget())
    assert exc.value.status == status


def test_resume_uses_frozen_sources_without_catalogue_discovery(real_ee, monkeypatch):
    recipe = validate_recipe(recipe_value())
    evaluation(monkeypatch, [BOUNDS, 1, ["scene_a"]])
    original, provenance = prepare(recipe, "products/run", Clock().budget())
    pending = evaluation(monkeypatch, [BOUNDS])
    resumed, resumed_provenance = prepare(recipe, "products/run", Clock().budget(), provenance)
    assert pending == []
    assert resumed_provenance == provenance
    assert resumed[0]["fingerprint"] == original[0]["fingerprint"]


def test_online_projected_budget_rejects_uncertain_or_excessive_region(real_ee, monkeypatch):
    value = recipe_value()
    value["grid"] = dict(crs="EPSG:32631", transform=[20, 0, 0, 0, -20, 0])
    pending = evaluation(monkeypatch, [[[[0, 0], [1e8, 0], [1e8, 1e8], [0, 1e8], [0, 0]]]])
    with pytest.raises(RunnerError, match="pixel/calibration"):
        prepare(validate_recipe(value), "products/run", Clock().budget())
    assert pending == []


def test_initialized_transport_is_deadline_bound_and_uses_only_verified_credentials(
    monkeypatch,
):
    from geecomposer.runner_cloud import attached_identity, EETransport
    from runner_fakes import policy_value
    from geecomposer.recipe import validate_policy
    from test_runner_cloud import MetadataFake

    budget = Clock().budget()
    creds = attached_identity(
        validate_policy(policy_value()), budget, metadata=MetadataFake(), environ={}
    )
    calls = []
    monkeypatch.setattr(ee, "Reset", lambda: calls.append("reset"))
    monkeypatch.setattr(ee.data, "setMaxRetries", lambda n: calls.append(n))
    monkeypatch.setattr(ee, "Initialize", lambda **kwargs: calls.append(kwargs))
    EarthEngine(budget).initialize(creds, "test-project")
    assert calls[:2] == ["reset", 0]
    assert calls[2]["credentials"] is creds
    assert calls[2]["project"] == "test-project"
    assert isinstance(calls[2]["http_transport"], EETransport)


@pytest.mark.parametrize("lost_response", [False, True])
def test_runner_with_native_science_and_ee_transport_boundaries(
    real_ee, monkeypatch, lost_response
):
    from geecomposer.runner import Runner
    from runner_fakes import World

    w = World()
    budget = w.clock.budget()
    engine = EarthEngine(budget)
    # real_ee already installed the real algorithm registry without auth/network.
    monkeypatch.setattr(engine, "initialize", lambda *args: None)
    engine.project = "test-project"
    engine.credentials = Mock()

    def listing(*args, **kwargs):
        task = w.store.records["native"][0]["tasks"][0]
        return (
            200,
            {},
            canonical(
                {
                    "operations": [
                        {
                            "name": "projects/test-project/operations/native-operation",
                            "metadata": {"description": task["marker"], "state": "SUCCEEDED"},
                        }
                    ]
                }
            ),
        )

    monkeypatch.setattr("geecomposer.runner_cloud._read_url", listing)
    pending = evaluation(monkeypatch, [BOUNDS, 1, ["scene_a"]])
    monkeypatch.setattr(ee.data, "newTaskId", lambda: ["native-request"])
    submitted = []

    def export(request_id, config):
        state, generation = w.store.records["native"]
        assert generation > 1
        assert state["recipe"] == w.recipe
        (task,) = state["tasks"]
        assert task["request_id"] == request_id == "native-request"
        assert task["state"] == "submitting"
        assert task["fingerprint"] == config_fingerprint(config)
        assert isinstance(config["expression"], ee.Image)
        submitted.append(request_id)
        if lost_response:
            raise TimeoutError("lost submit response")
        return {"name": "projects/test-project/operations/native-operation"}

    monkeypatch.setattr(ee.data, "exportImage", export)
    statuses = []

    def status(identifier):
        statuses.append(identifier)
        assert w.store.records["native"][0]["tasks"][0]["operation"].endswith(identifier)
        assert identifier != "native-request"
        return [{"state": "COMPLETED"}]

    monkeypatch.setattr(ee.data, "getTaskStatus", status)
    runner = Runner(w.policy, w.store, engine, object(), budget)
    result = runner.run(w.recipe, "native")
    assert result["status"] == "succeeded"
    assert submitted == ["native-request"]
    assert statuses == ["native-operation"]
    assert pending == []


def call_at(graph, node, name):
    call = graph.resolve(node)["functionInvocationValue"]
    assert call["functionName"] == name
    return call["arguments"]


def mapped_body(graph, node):
    args = call_at(graph, node, "Collection.map")
    fn = graph.resolve(args["baseAlgorithm"])["functionDefinitionValue"]
    return graph.values[fn["body"]], args["collection"]


def test_runner_s2_mask_link_is_an_ancestor_of_transform_and_reduction(real_ee, monkeypatch):
    from ee_graph import Graph

    value = recipe_value()
    value["mask"] = "s2_cloud_score_plus"
    evaluation(monkeypatch, [BOUNDS, 1, ["scene_a"]])
    tasks, _ = prepare(validate_recipe(value), "products/run", Clock().budget())
    graph = Graph(tasks[0]["config"]["expression"])
    (reduced,) = graph.calls("reduce.median")
    transform, selected = mapped_body(graph, reduced["collection"])
    renamed = call_at(graph, transform, "Image.rename")
    call_at(graph, renamed["input"], "Image.normalizedDifference")
    selection, masked = mapped_body(graph, selected)
    assert graph.constant(call_at(graph, selection, "Image.select")["bandSelectors"]) == [
        "B4",
        "B8",
    ]
    # compose's raw snapshot path filters AOI/date after the linked mask.
    masked = call_at(graph, masked, "Collection.filter")["collection"]
    masked = call_at(graph, masked, "Collection.filter")["collection"]
    mask, linked = mapped_body(graph, masked)
    update = call_at(graph, mask, "Image.updateMask")
    threshold = call_at(graph, update["mask"], "Image.gte")
    score = call_at(graph, threshold["image1"], "Image.select")
    assert graph.resolve(score["input"]) == graph.resolve(update["image"])
    assert graph.constant(score["bandSelectors"]) == ["cs_cdf"]
    assert graph.constant(call_at(graph, threshold["image2"], "Image.constant")["value"]) == 0.6
    link, snapshot = mapped_body(graph, linked)
    args = call_at(graph, link, "Image.linkCollection")
    assert graph.constant(args["linkedBands"]) == ["cs_cdf"]
    (collection,) = graph.calls("ImageCollection.load", args["imageCollection"])
    assert graph.constant(collection["id"]) == "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
    (image,) = graph.calls("Image.load", snapshot)
    assert graph.constant(image["id"]) == "COPERNICUS/S2_SR_HARMONIZED/scene_a"
    # Omitted matchPropertyName uses the installed API's system:index default.
    assert "matchPropertyName" not in args
    assert (
        next(
            a
            for a in ee.ApiFunction.lookup("Image.linkCollection").getSignature()["args"]
            if a["name"] == "matchPropertyName"
        )["optional"]
        is True
    )


@pytest.mark.parametrize("dataset", ["sentinel1", "sentinel1_float"])
@pytest.mark.parametrize(
    "filters",
    [None, {"instrumentMode": "EW", "orbitPass": "DESCENDING", "polarizations": ["VV", "VH"]}],
)
def test_runner_s1_snapshot_queries_depend_on_filters(real_ee, monkeypatch, dataset, filters):
    from ee_graph import Graph

    value = recipe_value()
    value.update(dataset=dataset, bands=["VV", "VH"], transform="identity")
    if filters is not None:
        value["filters"] = filters
    queries, answers = [], [BOUNDS, 1, ["scene_a"]]

    def compute(obj):
        queries.append(Graph(obj))
        return answers.pop(0)

    monkeypatch.setattr(ee.data, "computeValue", compute)
    prepare(validate_recipe(value), "products/run", Clock().budget())
    for graph in queries[1:]:
        equalities = {
            graph.constant(a["leftField"]): graph.constant(a["rightValue"])
            for a in graph.calls("Filter.equals")
        }
        assert equalities["instrumentMode"] == ("IW" if filters is None else "EW")
        if filters is None:
            assert "orbitProperties_pass" not in equalities
        else:
            assert equalities["orbitProperties_pass"] == "DESCENDING"
            pols = list(graph.calls("Filter.listContains"))
            assert {graph.constant(a["rightValue"]) for a in pols} == {"VV", "VH"}
            assert all(
                graph.constant(a["leftField"]) == "transmitterReceiverPolarisation" for a in pols
            )
        assert len(list(graph.calls("Filter.dateRangeContains"))) == 1
        assert len(list(graph.calls("Filter.intersects"))) == 1


def test_runner_gamma_precedes_ratio_precedes_reduction(real_ee, monkeypatch):
    from ee_graph import Graph

    value = recipe_value()
    value.update(
        dataset="sentinel1_float",
        bands=["VV", "VH"],
        preprocess="gamma_map",
        transform="vh_vv_ratio",
    )
    evaluation(monkeypatch, [BOUNDS, 1, ["scene_a"]])
    tasks, _ = prepare(validate_recipe(value), "products/run", Clock().budget())
    graph = Graph(tasks[0]["config"]["expression"])
    (reduced,) = graph.calls("reduce.median")
    ratio_body, preprocessed = mapped_body(graph, reduced["collection"])
    ratio = graph.resolve(call_at(graph, ratio_body, "Image.rename")["input"])[
        "functionInvocationValue"
    ]
    expression = call_at(graph, graph.values[ratio["functionReference"]], "Image.parseExpression")
    assert graph.constant(expression["expression"]) == "vh / vv"
    for variable, band in [("vh", "VH"), ("vv", "VV")]:
        selected = call_at(graph, ratio["arguments"][variable], "Image.select")
        assert graph.constant(selected["bandSelectors"]) == [band]
        assert graph.resolve(selected["input"]) == graph.resolve(
            ratio["arguments"]["DEFAULT_EXPRESSION_IMAGE"]
        )
    gamma_body, selected = mapped_body(graph, preprocessed)
    gamma = call_at(graph, gamma_body, "Image.addBands")
    assert graph.constant(gamma["overwrite"]) is True
    filtered = call_at(graph, gamma["srcImg"], "Image.updateMask")
    # Neighborhood estimates and conditional Gamma MAP output feed the ratio's input map.
    assert list(graph.calls("Image.reduceNeighborhood", filtered["image"]))
    assert list(graph.calls("Image.where", filtered["image"]))
    selection, _ = mapped_body(graph, selected)
    assert graph.constant(call_at(graph, selection, "Image.select")["bandSelectors"]) == [
        "VV",
        "VH",
    ]
