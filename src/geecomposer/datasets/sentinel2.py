"""Sentinel-2 dataset helpers.

Handles collection loading, AOI/date filtering, and cloud masking for
Sentinel-2 Surface Reflectance Harmonized imagery.
"""

from __future__ import annotations

import ee

COLLECTION_ID = "COPERNICUS/S2_SR_HARMONIZED"

SUPPORTED_MASKS: tuple[str, ...] = ("s2_cloud_score_plus",)

# Cloud Score+ collection used for s2_cloud_score_plus masking.
_CS_PLUS_COLLECTION = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
_CS_PLUS_BAND = "cs_cdf"
_CS_PLUS_THRESHOLD = 0.6


def get_collection_id() -> str:
    """Return the default Sentinel-2 collection identifier."""
    return COLLECTION_ID


def load_collection(
    aoi: ee.Geometry,
    start: str,
    end: str,
    filters: dict | None = None,
) -> ee.ImageCollection:
    """Load a Sentinel-2 SR Harmonized collection filtered by AOI and date.

    Parameters
    ----------
    aoi:
        An ``ee.Geometry`` for spatial filtering.
    start, end:
        ISO date strings for temporal filtering.
    filters:
        Optional extra metadata filters (currently unused for Sentinel-2 but
        accepted for interface consistency with other dataset modules).

    Returns
    -------
    ee.ImageCollection
        The filtered Sentinel-2 collection.
    """
    col = (
        ee.ImageCollection(COLLECTION_ID)
        .filterBounds(aoi)
        .filterDate(start, end)
    )
    return col


def apply_mask(
    collection: ee.ImageCollection,
    mask: str = "s2_cloud_score_plus",
) -> ee.ImageCollection:
    """Apply the selected Sentinel-2 masking preset.

    Parameters
    ----------
    collection:
        A Sentinel-2 ``ee.ImageCollection``.
    mask:
        Masking preset name. Only ``"s2_cloud_score_plus"`` is supported in
        v0.1.

    Returns
    -------
    ee.ImageCollection
        The collection with cloud-masked imagery.

    Raises
    ------
    GeeComposerError
        If *mask* is not a supported preset.
    """
    from ..exceptions import GeeComposerError

    if mask not in SUPPORTED_MASKS:
        supported = ", ".join(SUPPORTED_MASKS)
        raise GeeComposerError(
            f"Unsupported Sentinel-2 mask '{mask}'. "
            f"Supported masks: {supported}."
        )

    if mask == "s2_cloud_score_plus":
        return _apply_cloud_score_plus(collection)

    # Unreachable given the check above, but defensive.
    raise GeeComposerError(f"No implementation for mask '{mask}'.")


def _apply_cloud_score_plus(
    collection: ee.ImageCollection,
) -> ee.ImageCollection:
    """Apply Cloud Score+ masking to a Sentinel-2 collection.

    Joins the Cloud Score+ ``cs_cdf`` band to each image and masks pixels
    where the score is below the threshold.
    """
    cs_plus = ee.ImageCollection(_CS_PLUS_COLLECTION).filterBounds(
        collection.geometry()
    )

    # Join Cloud Score+ to each S2 image by system:index.
    linked = collection.linkCollection(cs_plus, [_CS_PLUS_BAND])

    def _mask_fn(img: ee.Image) -> ee.Image:
        score = img.select(_CS_PLUS_BAND)
        return img.updateMask(score.gte(_CS_PLUS_THRESHOLD))

    return linked.map(_mask_fn)
