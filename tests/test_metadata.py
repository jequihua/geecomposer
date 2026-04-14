"""Tests for metadata payload helpers."""

from __future__ import annotations

from geecomposer.utils.metadata import build_metadata_payload


class TestBuildMetadataPayload:
    def test_basic_payload(self) -> None:
        props = build_metadata_payload(
            dataset="sentinel2",
            collection="COPERNICUS/S2_SR_HARMONIZED",
            start="2024-01-01",
            end="2024-12-31",
            reducer="median",
            transform_name="ndvi",
        )
        assert props["geecomposer:dataset"] == "sentinel2"
        assert props["geecomposer:collection"] == "COPERNICUS/S2_SR_HARMONIZED"
        assert props["geecomposer:start"] == "2024-01-01"
        assert props["geecomposer:end"] == "2024-12-31"
        assert props["geecomposer:reducer"] == "median"
        assert props["geecomposer:transform"] == "ndvi"

    def test_none_values_become_empty_strings(self) -> None:
        props = build_metadata_payload(
            dataset=None,
            collection=None,
            start=None,
            end=None,
            reducer="median",
            transform_name=None,
        )
        assert props["geecomposer:dataset"] == ""
        assert props["geecomposer:collection"] == ""
        assert props["geecomposer:transform"] == ""

    def test_user_metadata_prefixed(self) -> None:
        props = build_metadata_payload(
            dataset="sentinel2",
            collection=None,
            start="2024-01-01",
            end="2024-12-31",
            reducer="max",
            transform_name=None,
            metadata={"project": "my_project", "version": "1"},
        )
        assert props["geecomposer:user:project"] == "my_project"
        assert props["geecomposer:user:version"] == "1"

    def test_no_user_metadata(self) -> None:
        props = build_metadata_payload(
            dataset="sentinel2",
            collection=None,
            start="2024-01-01",
            end="2024-12-31",
            reducer="median",
            transform_name=None,
            metadata=None,
        )
        assert not any(k.startswith("geecomposer:user:") for k in props)
