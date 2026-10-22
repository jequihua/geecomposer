"""Offline native GCS task construction and early option validation."""

from unittest.mock import Mock
import json
from pathlib import Path
import runpy
import socket

import ee
import pytest

from geecomposer import export_to_gcs
from geecomposer.export import gcs
from geecomposer.exceptions import GeeComposerError


@pytest.fixture
def forbidden(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("unexpected authentication, discovery, evaluation or task start")

    monkeypatch.setattr(ee, "Authenticate", fail)
    monkeypatch.setattr(ee.data, "get_persistent_credentials", fail)
    monkeypatch.setattr(ee.data, "computeValue", fail)
    monkeypatch.setattr(ee.batch.Task, "start", fail)
    import google.auth

    monkeypatch.setattr(google.auth, "default", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)


@pytest.mark.parametrize(
    "grid",
    [
        {"scale": 12.5},
        {"dimensions": 512},
        {"dimensions": "512x256", "crs": "EPSG:32632"},
        {
            "crs": "EPSG:32632",
            "crs_transform": (20, 0.125, 500000.25, 0.25, -20, 5300000.75),
        },
    ],
)
def test_exact_forwarding(monkeypatch, forbidden, grid):
    geometry, image, task = object(), object(), Mock()
    normalize = Mock(return_value=geometry)
    construct = Mock(return_value=task)
    monkeypatch.setattr(gcs, "to_ee_geometry", normalize)
    monkeypatch.setattr(ee.batch.Export.image, "toCloudStorage", construct)
    result = export_to_gcs(
        image,
        "result",
        "example-bucket",
        "aoi.geojson",
        file_name_prefix="folder/run/product",
        max_pixels=2500,
        shard_size=128,
        file_dimensions=(256, 512),
        cloud_optimized=True,
        no_data=-9999,
        skip_empty_tiles=True,
        **grid,
    )
    assert result is task
    normalize.assert_called_once_with("aoi.geojson")
    expected = dict(
        image=image,
        description="result",
        bucket="example-bucket",
        region=geometry,
        fileNamePrefix="folder/run/product",
        maxPixels=2500,
        shardSize=128,
        fileDimensions=(256, 512),
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True, "noData": -9999},
        skipEmptyTiles=True,
    )
    expected.update(
        {
            "crsTransform" if k == "crs_transform" else k: (
                list(v) if k == "crs_transform" else v
            )
            for k, v in grid.items()
        }
    )
    construct.assert_called_once_with(**expected)
    task.start.assert_not_called()


BAD_OPTIONS = [
    {"max_pixels": 0.5},
    {"max_pixels": 1.5},
    {"max_pixels": 2**63},
    {"scale": None},
    {"dimensions": 100},
    {"crs_transform": [1, 0, 0, 0, -1, 0], "crs": "EPSG:4326"},
    *(
        {"bucket": v}
        for v in (
            None,
            "",
            "ab",
            "gs://bucket",
            "UPPER",
            "bucket/path",
            "../bucket",
            "bucket\\path",
            "127.0.0.1",
            "bad..bucket",
            "goog-test",
        )
    ),
    *(
        {"file_name_prefix": v}
        for v in (
            "",
            "/absolute",
            "gs://bucket/p",
            "a\\b",
            "a/../b",
            "./a",
            "a//b",
            " p",
            "p\n",
        )
    ),
    *({"description": v} for v in (None, "", "bad name")),
    *(
        {key: v}
        for key in ("scale", "max_pixels")
        for v in (True, 0, -1, float("nan"), float("inf"), "10", {}, 10**400)
    ),
    *(
        {"scale": None, "dimensions": v}
        for v in (True, 0, 1.5, "0x5", "5X5", "2x-1", [1, 2])
    ),
    {"scale": None, "dimensions": 20, "crs_transform": [1, 0, 0, 0, -1, 0]},
    *(
        {"scale": None, "crs_transform": v, "crs": "EPSG:4326"}
        for v in (
            [True, 0, 0, 0, -1, 0],
            ["1", 0, 0, 0, -1, 0],
            [1, 0, 0, 0, 0, 0],
            [1, 2],
            "123456",
            [1, 0, 0, 0, float("inf"), 0],
        )
    ),
    {"scale": None, "crs_transform": [1, 0, 0, 0, -1, 0]},
    {"crs": ""},
    *({"shard_size": v} for v in (True, 0, 1.5, "256")),
    *(
        {"file_dimensions": v}
        for v in (True, 0, 257, [256], [256, 512, 768], [256, False], "256", 256.0)
    ),
    {"file_format": "TFRecord"},
    {"cloud_optimized": 1},
    {"skip_empty_tiles": "true"},
    *({"no_data": v} for v in (True, "-9999", float("nan"), float("inf"))),
]


@pytest.mark.parametrize("options", BAD_OPTIONS)
def test_invalid_before_aoi_or_task(monkeypatch, forbidden, options):
    def fail(*args, **kwargs):
        pytest.fail("invalid options reached AOI or task construction")

    monkeypatch.setattr(gcs, "to_ee_geometry", fail)
    monkeypatch.setattr(ee.batch.Export.image, "toCloudStorage", fail)
    kwargs = dict(
        image=object(),
        description="result",
        bucket="example-bucket",
        region="missing.geojson",
        scale=20,
    )
    kwargs.update(options)
    with pytest.raises(GeeComposerError):
        export_to_gcs(**kwargs)


@pytest.mark.parametrize(
    "grid",
    [
        {"scale": 10},
        {"dimensions": "10x20"},
        {
            "crs": "EPSG:32632",
            "crs_transform": [20, 0.125, 500000.25, 0.25, -20, 5300000.75],
        },
    ],
)
def test_native_unstarted_task(real_ee, forbidden, grid):
    region = ee.Geometry.Rectangle(
        [0.123456789, 0.234567891, 1.345678912, 1.456789123], geodesic=False
    )
    task = export_to_gcs(
        ee.Image.constant(1),
        "native",
        "example-bucket",
        region,
        file_name_prefix="run/product",
        max_pixels=2500,
        cloud_optimized=True,
        no_data=-9999,
        file_dimensions=512,
        skip_empty_tiles=True,
        **grid,
    )
    assert isinstance(task, ee.batch.Task)
    assert task.state == ee.batch.Task.State.UNSUBMITTED
    assert task.id is None
    config = task.config
    assert config["description"] == "native"
    assert config["maxPixels"] == {"value": "2500"}
    opts = config["fileExportOptions"]
    assert opts["fileFormat"] == "GEO_TIFF"
    assert opts["cloudStorageDestination"] == {
        "bucket": "example-bucket",
        "filenamePrefix": "run/product",
    }
    assert opts["geoTiffOptions"] == {
        "cloudOptimized": True,
        "noData": {"floatValue": -9999},
        "tileDimensions": {"width": 512, "height": 512},
        "skipEmptyFiles": True,
        "tileSize": {"value": 256},
    }
    serialized = config["expression"].serialize()
    assert "Image.unmask" not in serialized
    assert "Image.clipToBoundsAndScale" in serialized or "Image.clip" in serialized
    args = config["expression"].args
    assert args["geometry"].serialize() == region.serialize()
    if "crs_transform" in grid:
        projection = args["input"].args
        assert projection["crs"].serialize() == ee.Projection(grid["crs"]).serialize()
        assert json.loads(projection["crsTransform"].serialize()) == json.loads(
            ee.List(grid["crs_transform"]).serialize()
        )
    elif "scale" in grid:
        assert args["scale"].serialize() == ee.Number(grid["scale"]).serialize()
    else:
        assert args["width"].serialize() == ee.Number(10).serialize()
        assert args["height"].serialize() == ee.Number(20).serialize()


def test_positional_defaults_and_private_error(monkeypatch, forbidden):
    monkeypatch.setattr(gcs, "to_ee_geometry", lambda r: r)
    native = Mock(return_value=object())
    monkeypatch.setattr(ee.batch.Export.image, "toCloudStorage", native)
    export_to_gcs(object(), "result", "example-bucket", object(), 10, "run/", 1000)
    assert native.call_args.kwargs["fileNamePrefix"] == "run/"
    export_to_gcs(object(), "result", "example-bucket", object(), 10)
    assert native.call_args.kwargs["fileNamePrefix"] == "result"
    native.side_effect = RuntimeError("PRIVATE_CREDENTIAL")
    with pytest.raises(GeeComposerError) as error:
        export_to_gcs(object(), "result", "example-bucket", object(), 10)
    import traceback

    assert "PRIVATE_CREDENTIAL" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("kind", ["EEException", "ValueError", "TypeError", "unknown"])
def test_task_error_categories(monkeypatch, forbidden, kind):
    import traceback

    error_type = {
        "EEException": ee.EEException,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "unknown": type("PRIVATE_CLASS", (Exception,), {}),
    }[kind]
    monkeypatch.setattr(gcs, "to_ee_geometry", lambda r: r)
    monkeypatch.setattr(
        ee.batch.Export.image,
        "toCloudStorage",
        Mock(side_effect=error_type("PRIVATE_CREDENTIAL")),
    )
    with pytest.raises(GeeComposerError) as error:
        export_to_gcs(object(), "result", "example-bucket", object(), 10)
    assert str(error.value) == (
        f"GCS task construction failed ({kind}); check image, region and export options"
    )
    visible = "".join(traceback.format_exception(error.value))
    assert "PRIVATE_CREDENTIAL" not in visible
    assert "PRIVATE_CLASS" not in visible
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None


@pytest.mark.parametrize("source", ["helper", "shipped_example"])
def test_double_rao_export_graph(real_ee, forbidden, source):
    from geecomposer import RaoGrid, rao_q

    grid = RaoGrid("EPSG:32632", (20, 0, 500000, 0, -20, 5300000))
    region = ee.Geometry.Rectangle([9, 47, 9.01, 47.01], geodesic=False)
    image = ee.Image.constant(1).rename("signal")
    product = rao_q(
        image,
        ["signal"],
        grid=grid,
        region=region,
        window_size=3,
    )
    if source == "shipped_example":
        example = Path(__file__).resolve().parents[1] / "examples/rao_gcs_unstarted.py"
        build_task = _load_example(example)["build_task"]
        task = build_task(
            image=image,
            bands=["signal"],
            region=region,
            grid=grid,
            bucket="example-bucket",
            prefix="run/product",
        )
    else:
        task = export_to_gcs(
            product.toDouble(),
            "rao_product",
            "example-bucket",
            region,
            crs=grid.crs,
            crs_transform=grid.transform,
            no_data=-9999,
        )
    # Follow the actual native task expression through region/grid wrappers.
    clipped = task.config["expression"]
    assert clipped.func.getSignature()["name"] == "Image.clip"
    projected = clipped.args["input"]
    assert projected.func.getSignature()["name"] == "Image.reproject"
    expression = projected.args["image"]
    assert expression.func.getSignature()["name"] == "Image.toDouble"
    assert projected.args["crs"].serialize() == ee.Projection(grid.crs).serialize()
    assert json.loads(projected.args["crsTransform"].serialize()) == json.loads(
        ee.List(grid.transform).serialize()
    )
    assert clipped.args["geometry"].serialize() == region.serialize()
    assert expression.args["value"].serialize() == product.serialize()
    assert task.config["fileExportOptions"]["geoTiffOptions"]["noData"] == {
        "floatValue": -9999
    }
    assert task.state == ee.batch.Task.State.UNSUBMITTED


EXAMPLE_ABSENT = "shipped GCS example absent from standalone source/tests copy"


def _load_example(path):
    if not path.exists():
        pytest.skip(EXAMPLE_ABSENT)
    return runpy.run_path(str(path))


def test_example_loader_absent(tmp_path):
    with pytest.raises(pytest.skip.Exception) as error:
        _load_example(tmp_path / "absent.py")
    assert str(error.value) == EXAMPLE_ABSENT


def test_example_loader_executes_present_file(tmp_path):
    example = tmp_path / "present.py"
    example.write_text("def build_task():\n    return 42\n", encoding="utf-8")
    assert _load_example(example)["build_task"]() == 42


@pytest.mark.parametrize(
    "body, error_type",
    [
        ("raise ImportError('fixture error')", ImportError),
        ("def invalid syntax", SyntaxError),
        ("raise AssertionError('fixture error')", AssertionError),
        ("raise FileNotFoundError('fixture error')", FileNotFoundError),
    ],
)
def test_example_loader_propagates_present_file_errors(tmp_path, body, error_type):
    example = tmp_path / "broken.py"
    example.write_text(body, encoding="utf-8")
    with pytest.raises(error_type):
        _load_example(example)
