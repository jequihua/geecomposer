"""Tests for AOI normalization."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from geecomposer.exceptions import InvalidAOIError

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_GEOJSON_PATH = FIXTURES / "sample_aoi.geojson"
MULTI_FEATURE_PATH = FIXTURES / "multi_feature_aoi.geojson"

POLYGON_GEOJSON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-3.80, 40.40],
            [-3.80, 40.55],
            [-3.55, 40.55],
            [-3.55, 40.40],
            [-3.80, 40.40],
        ]
    ],
}

POLYGON_WEST = {
    "type": "Polygon",
    "coordinates": [
        [
            [-3.80, 40.40],
            [-3.80, 40.55],
            [-3.70, 40.55],
            [-3.70, 40.40],
            [-3.80, 40.40],
        ]
    ],
}

POLYGON_EAST = {
    "type": "Polygon",
    "coordinates": [
        [
            [-3.65, 40.40],
            [-3.65, 40.55],
            [-3.55, 40.55],
            [-3.55, 40.40],
            [-3.65, 40.40],
        ]
    ],
}

FEATURE_GEOJSON = {
    "type": "Feature",
    "properties": {},
    "geometry": POLYGON_GEOJSON,
}

FEATURE_COLLECTION_GEOJSON = {
    "type": "FeatureCollection",
    "features": [FEATURE_GEOJSON],
}

MULTI_FEATURE_COLLECTION_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {}, "geometry": POLYGON_WEST},
        {"type": "Feature", "properties": {}, "geometry": POLYGON_EAST},
    ],
}


# ---------------------------------------------------------------------------
# _dissolve_feature_collection
# ---------------------------------------------------------------------------


class TestDissolveFeatureCollection:
    def test_single_feature_returns_geometry(self) -> None:
        from geecomposer.aoi import _dissolve_feature_collection

        features = [{"type": "Feature", "properties": {}, "geometry": POLYGON_GEOJSON}]
        result = _dissolve_feature_collection(features)
        assert isinstance(result, dict)
        assert "type" in result
        assert "coordinates" in result

    def test_multi_feature_dissolves(self) -> None:
        from geecomposer.aoi import _dissolve_feature_collection

        features = MULTI_FEATURE_COLLECTION_GEOJSON["features"]
        result = _dissolve_feature_collection(features)
        assert isinstance(result, dict)
        assert result["type"] in ("Polygon", "MultiPolygon")

    def test_multi_feature_bounds_cover_both(self) -> None:
        """Dissolved geometry should span the extent of both input polygons."""
        from shapely.geometry import shape

        from geecomposer.aoi import _dissolve_feature_collection

        features = MULTI_FEATURE_COLLECTION_GEOJSON["features"]
        result = _dissolve_feature_collection(features)
        dissolved = shape(result)
        # West polygon goes to -3.80, east polygon goes to -3.55
        minx, _, maxx, _ = dissolved.bounds
        assert minx == pytest.approx(-3.80, abs=0.01)
        assert maxx == pytest.approx(-3.55, abs=0.01)

    def test_no_valid_geometries_raises(self) -> None:
        from geecomposer.aoi import _dissolve_feature_collection

        features = [{"type": "Feature", "properties": {}}]
        with pytest.raises(InvalidAOIError, match="no valid geometries"):
            _dissolve_feature_collection(features)

    def test_malformed_geometry_missing_coordinates_raises(self) -> None:
        """A geometry dict without coordinates raises InvalidAOIError."""
        from geecomposer.aoi import _dissolve_feature_collection

        features = [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon"}},
        ]
        with pytest.raises(InvalidAOIError, match="Malformed geometry"):
            _dissolve_feature_collection(features)

    def test_malformed_geometry_unknown_type_raises(self) -> None:
        """A geometry dict with an unknown type raises InvalidAOIError."""
        from geecomposer.aoi import _dissolve_feature_collection

        features = [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Hexagon", "coordinates": []},
            },
        ]
        with pytest.raises(InvalidAOIError, match="Malformed geometry"):
            _dissolve_feature_collection(features)

    def test_mixed_valid_and_malformed_raises(self) -> None:
        """A mix of valid and malformed geometries raises on the malformed one."""
        from geecomposer.aoi import _dissolve_feature_collection

        features = [
            {"type": "Feature", "properties": {}, "geometry": POLYGON_WEST},
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon"}},
        ]
        with pytest.raises(InvalidAOIError, match="Malformed geometry.*feature 1"):
            _dissolve_feature_collection(features)


# ---------------------------------------------------------------------------
# geojson_to_ee_geometry
# ---------------------------------------------------------------------------


class TestGeojsonToEeGeometry:
    @patch("geecomposer.aoi.ee")
    def test_polygon_dict(self, mock_ee: MagicMock) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        sentinel = MagicMock()
        mock_ee.Geometry.return_value = sentinel

        result = geojson_to_ee_geometry(POLYGON_GEOJSON)
        mock_ee.Geometry.assert_called_once_with(POLYGON_GEOJSON)
        assert result is sentinel

    @patch("geecomposer.aoi.ee")
    def test_feature_dict(self, mock_ee: MagicMock) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        sentinel = MagicMock()
        mock_ee.Geometry.return_value = sentinel

        result = geojson_to_ee_geometry(FEATURE_GEOJSON)
        mock_ee.Geometry.assert_called_once_with(POLYGON_GEOJSON)
        assert result is sentinel

    @patch("geecomposer.aoi.ee")
    def test_single_feature_collection_dissolves(self, mock_ee: MagicMock) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        sentinel = MagicMock()
        mock_ee.Geometry.return_value = sentinel

        result = geojson_to_ee_geometry(FEATURE_COLLECTION_GEOJSON)
        mock_ee.Geometry.assert_called_once()
        # The dissolved geometry should be a dict passed to ee.Geometry
        call_args = mock_ee.Geometry.call_args[0][0]
        assert isinstance(call_args, dict)
        assert "type" in call_args
        assert result is sentinel

    @patch("geecomposer.aoi.ee")
    def test_multi_feature_collection_dissolves_all(self, mock_ee: MagicMock) -> None:
        """Multi-feature FeatureCollections dissolve all geometries, not just the first."""
        from shapely.geometry import shape

        from geecomposer.aoi import geojson_to_ee_geometry

        sentinel = MagicMock()
        mock_ee.Geometry.return_value = sentinel

        result = geojson_to_ee_geometry(MULTI_FEATURE_COLLECTION_GEOJSON)
        mock_ee.Geometry.assert_called_once()
        # The dissolved geometry passed to ee.Geometry should cover both polygons
        call_args = mock_ee.Geometry.call_args[0][0]
        dissolved = shape(call_args)
        minx, _, maxx, _ = dissolved.bounds
        assert minx == pytest.approx(-3.80, abs=0.01)
        assert maxx == pytest.approx(-3.55, abs=0.01)

    def test_non_dict_raises(self) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        with pytest.raises(InvalidAOIError, match="GeoJSON-like dict"):
            geojson_to_ee_geometry("not a dict")

    def test_unknown_type_raises(self) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        with pytest.raises(InvalidAOIError, match="Unrecognized GeoJSON type"):
            geojson_to_ee_geometry({"type": "Bogus"})

    def test_feature_without_geometry_raises(self) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        with pytest.raises(InvalidAOIError, match="has no geometry"):
            geojson_to_ee_geometry({"type": "Feature", "properties": {}})

    def test_empty_feature_collection_raises(self) -> None:
        from geecomposer.aoi import geojson_to_ee_geometry

        with pytest.raises(InvalidAOIError, match="no features"):
            geojson_to_ee_geometry({"type": "FeatureCollection", "features": []})


# ---------------------------------------------------------------------------
# read_vector_file
# ---------------------------------------------------------------------------


class TestReadVectorFile:
    def test_missing_file_raises(self) -> None:
        from geecomposer.aoi import read_vector_file

        with pytest.raises(InvalidAOIError, match="not found"):
            read_vector_file("/nonexistent/path/aoi.geojson")

    def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        from geecomposer.aoi import read_vector_file

        bad_file = tmp_path / "aoi.txt"
        bad_file.write_text("not a vector file")
        with pytest.raises(InvalidAOIError, match="Unsupported vector file extension"):
            read_vector_file(str(bad_file))

    def test_reads_geojson_file(self) -> None:
        from geecomposer.aoi import read_vector_file

        result = read_vector_file(SAMPLE_GEOJSON_PATH)
        assert isinstance(result, dict)
        assert "type" in result
        assert "coordinates" in result

    def test_reads_multi_feature_file(self) -> None:
        """Multi-feature files are dissolved into a single geometry."""
        from shapely.geometry import shape

        from geecomposer.aoi import read_vector_file

        result = read_vector_file(MULTI_FEATURE_PATH)
        assert isinstance(result, dict)
        dissolved = shape(result)
        minx, _, maxx, _ = dissolved.bounds
        # Should cover both west (-3.80) and east (-3.55) polygons
        assert minx == pytest.approx(-3.80, abs=0.01)
        assert maxx == pytest.approx(-3.55, abs=0.01)

    def test_reads_string_path(self) -> None:
        """Accepts plain string paths (not just pathlib.Path)."""
        from geecomposer.aoi import read_vector_file

        result = read_vector_file(str(MULTI_FEATURE_PATH))
        assert isinstance(result, dict)
        assert result["type"] in ("Polygon", "MultiPolygon")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        """A GeoJSON file with an empty FeatureCollection should fail."""
        from geecomposer.aoi import read_vector_file

        empty_fc = {"type": "FeatureCollection", "features": []}
        empty_file = tmp_path / "empty.geojson"
        empty_file.write_text(json.dumps(empty_fc))
        with pytest.raises(InvalidAOIError):
            read_vector_file(str(empty_file))


# ---------------------------------------------------------------------------
# to_ee_geometry
# ---------------------------------------------------------------------------


class TestToEeGeometry:
    def test_none_raises(self) -> None:
        from geecomposer.aoi import to_ee_geometry

        with pytest.raises(InvalidAOIError, match="must not be None"):
            to_ee_geometry(None)

    @patch("geecomposer.aoi.ee")
    def test_ee_geometry_passthrough(self, mock_ee: MagicMock) -> None:
        from geecomposer.aoi import to_ee_geometry

        mock_ee.Geometry = type("Geometry", (), {})
        geom_instance = mock_ee.Geometry()

        with patch("geecomposer.aoi.ee.Geometry", new=type(geom_instance)):
            result = to_ee_geometry(geom_instance)
            assert result is geom_instance

    @patch("geecomposer.aoi.geojson_to_ee_geometry")
    def test_dict_delegates_to_geojson_converter(self, mock_convert: MagicMock) -> None:
        from geecomposer.aoi import to_ee_geometry

        sentinel = MagicMock()
        mock_convert.return_value = sentinel

        result = to_ee_geometry(POLYGON_GEOJSON)
        mock_convert.assert_called_once_with(POLYGON_GEOJSON)
        assert result is sentinel

    def test_string_path_delegates_to_file_reader(self) -> None:
        from geecomposer.aoi import to_ee_geometry

        mock_read = MagicMock(return_value=POLYGON_GEOJSON)

        class _FakeGeometry:
            def __init__(self, *args, **kwargs):
                pass

        class _FakeFeature:
            pass

        class _FakeFeatureCollection:
            pass

        with patch("geecomposer.aoi.read_vector_file", mock_read), \
             patch("geecomposer.aoi.ee.Geometry", _FakeGeometry), \
             patch("geecomposer.aoi.ee.Feature", _FakeFeature), \
             patch("geecomposer.aoi.ee.FeatureCollection", _FakeFeatureCollection):
            result = to_ee_geometry("/some/path/aoi.geojson")

        mock_read.assert_called_once()
        assert isinstance(result, _FakeGeometry)

    def test_pathlib_path_delegates_to_file_reader(self) -> None:
        """pathlib.Path inputs are handled the same as string paths."""
        from geecomposer.aoi import to_ee_geometry

        mock_read = MagicMock(return_value=POLYGON_GEOJSON)

        class _FakeGeometry:
            def __init__(self, *args, **kwargs):
                pass

        class _FakeFeature:
            pass

        class _FakeFeatureCollection:
            pass

        with patch("geecomposer.aoi.read_vector_file", mock_read), \
             patch("geecomposer.aoi.ee.Geometry", _FakeGeometry), \
             patch("geecomposer.aoi.ee.Feature", _FakeFeature), \
             patch("geecomposer.aoi.ee.FeatureCollection", _FakeFeatureCollection):
            result = to_ee_geometry(Path("/some/path/aoi.geojson"))

        mock_read.assert_called_once()
        assert isinstance(result, _FakeGeometry)

    def test_unsupported_type_raises(self) -> None:
        from geecomposer.aoi import to_ee_geometry

        with pytest.raises(InvalidAOIError, match="Unsupported AOI type"):
            to_ee_geometry(12345)
