"""Google Cloud Storage export helpers."""

from __future__ import annotations


def export_to_gcs(
    image,
    description: str,
    bucket: str,
    region,
    scale: int,
    file_name_prefix: str | None = None,
    max_pixels: float = 1e13,
):
    """Create an Earth Engine export task for Google Cloud Storage."""
    raise NotImplementedError("Implement export_to_gcs() in a later milestone.")
