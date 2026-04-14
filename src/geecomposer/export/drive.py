"""Google Drive export helpers.

Creates Earth Engine export tasks for writing GeoTIFF images to Google Drive.
"""

from __future__ import annotations

from typing import Any

import ee

from ..aoi import to_ee_geometry
from ..exceptions import GeeComposerError


def export_to_drive(
    image: ee.Image,
    description: str,
    folder: str,
    region: Any,
    scale: int | float,
    file_name_prefix: str | None = None,
    max_pixels: float = 1e13,
) -> ee.batch.Export.image:
    """Create an Earth Engine export task for Google Drive.

    The task is created but **not started**. Call ``.start()`` on the
    returned task object to begin the export.

    Parameters
    ----------
    image:
        The ``ee.Image`` to export.
    description:
        Human-readable task description (also used as the default filename
        prefix if *file_name_prefix* is not provided).
    folder:
        Google Drive folder name to export into.
    region:
        Export region. Accepts any AOI form supported by
        ``to_ee_geometry()``: ``ee.Geometry``, ``ee.Feature``,
        ``ee.FeatureCollection``, a GeoJSON dict, or a local vector file
        path.
    scale:
        Pixel resolution in meters.
    file_name_prefix:
        Optional filename prefix. Defaults to *description* if not provided.
    max_pixels:
        Maximum number of pixels to export. Defaults to 1e13.

    Returns
    -------
    ee.batch.Export.image
        The Earth Engine export task (not yet started).

    Raises
    ------
    GeeComposerError
        If required parameters are missing or invalid.
    """
    if not isinstance(description, str) or not description.strip():
        raise GeeComposerError("description must be a non-empty string.")
    if not isinstance(folder, str) or not folder.strip():
        raise GeeComposerError("folder must be a non-empty string.")

    geometry = to_ee_geometry(region)

    task = ee.batch.Export.image.toDrive(
        image=image,
        description=description,
        folder=folder,
        region=geometry,
        scale=scale,
        fileNamePrefix=file_name_prefix or description,
        maxPixels=max_pixels,
    )

    return task
