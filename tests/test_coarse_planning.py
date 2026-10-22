"""S03: aligned tile cores, periods x cores task planning and export windows.

Pure partition properties plus the real preparation over a counted evaluation fake
(``real_ee``): science is built once per period on the full cell union, then each
selected core is exported with its exact grid-pixel rectangle.
"""

import pytest

from geecomposer import runner_coarse
from geecomposer.recipe import RunnerError, validate_recipe
from geecomposer.runner_science import prepare
from ee_graph import Graph
from runner_fakes import Clock
from test_coarse_metadata import BOUNDS, C1, C2, CS, Answers, collection, recipe

pytestmark = pytest.mark.usefixtures("runner_offline")
READS = [BOUNDS, collection(C1), collection(C2), collection(CS)]
WINDOW = dict(col_off=900, row_off=-2400, width=3100, height=2100)


@pytest.mark.parametrize("columns, rows", [(1, 1), (2, 1), (3, 2), (12, 12), (7, 5)])
def test_cores_partition_the_window_exactly_with_global_offsets(columns, rows):
    cores = runner_coarse.partition(WINDOW, columns, rows)
    assert len(cores) == columns * rows
    assert sum(c["width"] * c["height"] for c in cores.values()) == WINDOW["width"] * WINDOW["height"]
    for (r, c), core in cores.items():
        assert core["width"] > 0 and core["height"] > 0
        if c + 1 < columns:  # contiguous, non-overlapping columns
            assert cores[(r, c + 1)]["col_off"] == core["col_off"] + core["width"]
        if r + 1 < rows:
            assert cores[(r + 1, c)]["row_off"] == core["row_off"] + core["height"]
    assert cores[(0, 0)]["col_off"] == WINDOW["col_off"] and cores[(0, 0)]["row_off"] == WINDOW["row_off"]
    last = cores[(rows - 1, columns - 1)]
    assert last["col_off"] + last["width"] == WINDOW["col_off"] + WINDOW["width"]
    assert last["row_off"] + last["height"] == WINDOW["row_off"] + WINDOW["height"]


def prepared(monkeypatch, value, reads=READS):
    answers = Answers(monkeypatch, reads)
    tasks, provenance = prepare(validate_recipe(value), "products/run", Clock().budget())
    return tasks, provenance, answers


def rectangle(task):
    graph = Graph(task["config"]["expression"])
    geometry = graph.resolve(graph.root["functionInvocationValue"]["arguments"]["geometry"])
    return graph.constant(geometry["functionInvocationValue"]["arguments"]["coordinates"])


def test_periods_times_selected_cores_with_exact_core_rectangles(real_ee, monkeypatch):
    periods = [dict(label="jun", start="2025-06-01", end="2025-07-01"),
               dict(label="aug", start="2025-08-01", end="2025-09-01"),
               dict(label="dec", start="2025-12-01", end="2026-01-01")]
    value = recipe(periods=periods, tiles=dict(columns=2, rows=2, cores=[[0, 0], [1, 1]]))
    tasks, provenance, answers = prepared(monkeypatch, value)
    assert answers.calls == 4  # one read set, not one per period or core
    assert [t["name"] for t in tasks] == ["pjun_r00c00", "pjun_r01c01", "pdec_r00c00", "pdec_r01c01"]
    outcomes = {p["label"]: p["outcome"] for p in provenance["coarse"]["periods"]}
    assert outcomes == {"jun": "populated", "aug": "empty", "dec": "populated"}  # empty: no task
    cores = {(c["row"], c["column"]): c for c in provenance["coarse"]["cores"]}
    full = runner_coarse.partition(provenance["export_window"], 2, 2)
    for task in tasks:
        r, c = int(task["name"][-5:-3]), int(task["name"][-2:])
        window = full[(r, c)]
        assert cores[(r, c)]["col_off"] == window["col_off"]
        assert rectangle(task) == [window["col_off"], window["row_off"], window["col_off"] + window["width"],
                                   window["row_off"] + window["height"]]
    science = {t["name"][:4]: t["config"]["expression"].serialize() for t in tasks}
    jun = [t for t in tasks if t["name"].startswith("pjun")]
    graphs = [Graph(t["config"]["expression"]) for t in jun]
    inputs = [g.resolve(g.root["functionInvocationValue"]["arguments"]["input"]) for g in graphs]
    assert len(science) == 2 and len({str(i) for i in inputs}) == 1  # one science graph shared by both cores


def test_no_raw_aoi_polygon_truncates_coarse_cells(real_ee, monkeypatch):
    tasks, _, _ = prepared(monkeypatch, recipe())
    graph = Graph(tasks[0]["config"]["expression"])
    kinds = set()
    for call in graph.calls("Image.clip"):
        node = graph.resolve(call["geometry"]).get("functionInvocationValue", {})
        kinds.add(node.get("functionName"))
    assert "GeometryConstructors.MultiPolygon" not in kinds and "GeometryConstructors.Polygon" not in kinds
    assert kinds <= {"GeometryConstructors.Rectangle", "Collection.geometry", "FeatureCollection.geometry"}
    assert list(graph.calls("Geometry.coveringGrid"))  # membership is the full coarse-cell union


def test_each_core_must_fit_the_output_pixel_limit_before_metadata_reads(real_ee, monkeypatch):
    value = recipe(periods=None)
    value["limits"] = {"pixels": 3_000_000}
    with pytest.raises(RunnerError) as err:
        prepared(monkeypatch, value, reads=[BOUNDS])
    assert str(err.value) == "coarse core exceeds the output pixel limit"
    tiled = recipe(periods=None, tiles=dict(columns=2, rows=2))
    tiled["limits"] = {"pixels": 3_000_000}
    tasks, provenance, _ = prepared(monkeypatch, tiled)
    assert len(tasks) == 4 and provenance["pixels_upper_bound"] <= 3_000_000


def test_one_period_one_core_names_and_window_are_recorded(real_ee, monkeypatch):
    tasks, provenance, _ = prepared(monkeypatch, recipe(periods=None))
    assert [t["name"] for t in tasks] == ["pall"]
    assert provenance["export_window"] == WINDOW and provenance["nodata"] == -9999
    assert provenance["grid"]["crs"] == "MX-AEA" and provenance["coarse"]["final_bands"][-2:] == ["obs__t_count", "cov__t_mean"]
