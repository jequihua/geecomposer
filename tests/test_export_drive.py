"""Tests for Google Drive export helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from geecomposer.exceptions import GeeComposerError, InvalidAOIError


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


class TestExportToDrive:
    @patch("geecomposer.export.drive.to_ee_geometry")
    @patch("geecomposer.export.drive.ee")
    def test_creates_drive_task(
        self, mock_ee: MagicMock, mock_to_ee: MagicMock
    ) -> None:
        from geecomposer.export.drive import export_to_drive

        geometry = MagicMock()
        mock_to_ee.return_value = geometry

        task = MagicMock()
        mock_ee.batch.Export.image.toDrive.return_value = task

        image = MagicMock()
        result = export_to_drive(
            image=image,
            description="test_export",
            folder="gee_exports",
            region=POLYGON_GEOJSON,
            scale=10,
        )

        mock_to_ee.assert_called_once_with(POLYGON_GEOJSON)
        mock_ee.batch.Export.image.toDrive.assert_called_once_with(
            image=image,
            description="test_export",
            folder="gee_exports",
            region=geometry,
            scale=10,
            fileNamePrefix="test_export",
            maxPixels=1e13,
        )
        assert result is task

    @patch("geecomposer.export.drive.to_ee_geometry")
    @patch("geecomposer.export.drive.ee")
    def test_custom_file_name_prefix(
        self, mock_ee: MagicMock, mock_to_ee: MagicMock
    ) -> None:
        from geecomposer.export.drive import export_to_drive

        mock_to_ee.return_value = MagicMock()
        mock_ee.batch.Export.image.toDrive.return_value = MagicMock()

        export_to_drive(
            image=MagicMock(),
            description="test",
            folder="exports",
            region=POLYGON_GEOJSON,
            scale=10,
            file_name_prefix="custom_prefix",
        )

        call_kwargs = mock_ee.batch.Export.image.toDrive.call_args[1]
        assert call_kwargs["fileNamePrefix"] == "custom_prefix"

    @patch("geecomposer.export.drive.to_ee_geometry")
    @patch("geecomposer.export.drive.ee")
    def test_custom_max_pixels(
        self, mock_ee: MagicMock, mock_to_ee: MagicMock
    ) -> None:
        from geecomposer.export.drive import export_to_drive

        mock_to_ee.return_value = MagicMock()
        mock_ee.batch.Export.image.toDrive.return_value = MagicMock()

        export_to_drive(
            image=MagicMock(),
            description="test",
            folder="exports",
            region=POLYGON_GEOJSON,
            scale=10,
            max_pixels=5e9,
        )

        call_kwargs = mock_ee.batch.Export.image.toDrive.call_args[1]
        assert call_kwargs["maxPixels"] == 5e9

    @patch("geecomposer.export.drive.to_ee_geometry")
    @patch("geecomposer.export.drive.ee")
    def test_region_normalization(
        self, mock_ee: MagicMock, mock_to_ee: MagicMock
    ) -> None:
        """Region is normalized via to_ee_geometry."""
        from geecomposer.export.drive import export_to_drive

        geometry = MagicMock()
        mock_to_ee.return_value = geometry
        mock_ee.batch.Export.image.toDrive.return_value = MagicMock()

        export_to_drive(
            image=MagicMock(),
            description="test",
            folder="exports",
            region="/some/path/aoi.geojson",
            scale=10,
        )

        mock_to_ee.assert_called_once_with("/some/path/aoi.geojson")

    @patch("geecomposer.export.drive.to_ee_geometry")
    @patch("geecomposer.export.drive.ee")
    def test_default_file_name_prefix_from_description(
        self, mock_ee: MagicMock, mock_to_ee: MagicMock
    ) -> None:
        """When file_name_prefix is None, description is used as the prefix."""
        from geecomposer.export.drive import export_to_drive

        mock_to_ee.return_value = MagicMock()
        mock_ee.batch.Export.image.toDrive.return_value = MagicMock()

        export_to_drive(
            image=MagicMock(),
            description="my_composite",
            folder="exports",
            region=POLYGON_GEOJSON,
            scale=10,
            file_name_prefix=None,
        )

        call_kwargs = mock_ee.batch.Export.image.toDrive.call_args[1]
        assert call_kwargs["fileNamePrefix"] == "my_composite"

    def test_empty_description_raises(self) -> None:
        from geecomposer.export.drive import export_to_drive

        with pytest.raises(GeeComposerError, match="description must be a non-empty string"):
            export_to_drive(
                image=MagicMock(),
                description="",
                folder="exports",
                region=POLYGON_GEOJSON,
                scale=10,
            )

    def test_empty_folder_raises(self) -> None:
        from geecomposer.export.drive import export_to_drive

        with pytest.raises(GeeComposerError, match="folder must be a non-empty string"):
            export_to_drive(
                image=MagicMock(),
                description="test",
                folder="",
                region=POLYGON_GEOJSON,
                scale=10,
            )
