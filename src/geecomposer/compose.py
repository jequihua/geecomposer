"""Main composition orchestration entry point.

Implements the 12-step pipeline specified in the architecture contract:

1.  Resolve dataset or collection
2.  Normalize AOI
3.  Load image collection
4.  Filter by AOI (done inside dataset loader)
5.  Filter by date (done inside dataset loader)
6.  Apply dataset-specific filters and preprocessing/masking
7.  Apply optional band selection
8.  Apply optional custom preprocessing callable
9.  Apply optional transform callable
10. Apply temporal reducer
11. Attach metadata
12. Return ``ee.Image``
"""

from __future__ import annotations

from typing import Any, Callable

import ee

from .aoi import to_ee_geometry
from .datasets import sentinel1, sentinel1_float, sentinel2
from .exceptions import DatasetNotSupportedError, GeeComposerError
from .reducers.temporal import apply_reducer
from .utils.metadata import build_metadata_payload
from .validation import validate_dataset

# Maps validated dataset names to their loader modules.
_DATASET_MODULES: dict[str, Any] = {
    "sentinel1": sentinel1,
    "sentinel1_float": sentinel1_float,
    "sentinel2": sentinel2,
}


def compose(
    collection: str | None = None,
    dataset: str | None = None,
    aoi=None,
    start: str | None = None,
    end: str | None = None,
    reducer: str = "median",
    transform: Callable[[ee.Image], ee.Image] | None = None,
    select: str | list[str] | None = None,
    mask: str | None = None,
    preprocess: Callable[[ee.Image], ee.Image] | None = None,
    filters: dict | None = None,
    metadata: dict | None = None,
) -> ee.Image:
    """Compose an Earth Engine image from a dataset or collection.

    Parameters
    ----------
    collection:
        Raw Earth Engine collection ID. Mutually exclusive with *dataset*
        in v0.1 — when *dataset* is provided, the collection is resolved
        automatically.
    dataset:
        Friendly preset name: ``"sentinel2"``, ``"sentinel1"`` (dB), or
        ``"sentinel1_float"`` (linear units). Resolves the collection ID
        and enables dataset-specific loading and masking.
    aoi:
        Area of interest. Accepts ``ee.Geometry``, ``ee.Feature``,
        ``ee.FeatureCollection``, a GeoJSON dict, or a path to a local
        vector file.
    start, end:
        ISO date strings for temporal filtering.
    reducer:
        Temporal reducer name (``"median"``, ``"mean"``, ``"min"``,
        ``"max"``, ``"mosaic"``, ``"count"``).
    transform:
        Optional per-image transform callable ``ee.Image -> ee.Image``.
    select:
        Optional band name or list of band names to select before transform.
    mask:
        Optional dataset-specific masking preset (e.g.
        ``"s2_cloud_score_plus"`` for Sentinel-2).
    preprocess:
        Optional custom preprocessing callable ``ee.Image -> ee.Image``
        applied before *transform*.
    filters:
        Optional dataset-specific extra filters.
    metadata:
        Optional user metadata dict to attach to the output image.

    Returns
    -------
    ee.Image
        The composed and reduced Earth Engine image.
    """
    # --- Step 1: resolve dataset or collection --------------------------
    dataset_name, collection_id, ds_module = _resolve_dataset(dataset, collection)

    # --- Step 2: normalize AOI ------------------------------------------
    if aoi is None:
        raise GeeComposerError("aoi is required.")
    geometry = to_ee_geometry(aoi)

    # --- Step 3–5: load image collection (AOI + date filtering inside) --
    if start is None or end is None:
        raise GeeComposerError("start and end dates are required.")
    col = ds_module.load_collection(geometry, start, end, filters=filters)

    # --- Step 6: dataset-specific preprocessing / masking ---------------
    if mask is not None:
        col = ds_module.apply_mask(col, mask=mask)

    # --- Step 7: optional band selection --------------------------------
    if select is not None:
        bands = [select] if isinstance(select, str) else list(select)
        col = col.select(bands)

    # --- Step 8: optional custom preprocessing callable -----------------
    if preprocess is not None:
        col = col.map(preprocess)

    # --- Step 9: optional transform callable ----------------------------
    if transform is not None:
        col = col.map(transform)

    # --- Step 10: temporal reducer --------------------------------------
    # Validation happens inside apply_reducer().
    image = apply_reducer(col, reducer)

    # --- Step 11: attach metadata ---------------------------------------
    transform_name = getattr(transform, "__name__", None) if transform else None
    props = build_metadata_payload(
        dataset=dataset_name,
        collection=collection_id,
        start=start,
        end=end,
        reducer=reducer,
        transform_name=transform_name,
        metadata=metadata,
    )
    image = image.set(props)

    # --- Step 12: return ee.Image ---------------------------------------
    return image


def _resolve_dataset(
    dataset: str | None,
    collection: str | None,
) -> tuple[str | None, str, Any]:
    """Resolve the dataset preset or raw collection into a loader module.

    Returns ``(dataset_name, collection_id, module)``.
    """
    if dataset is not None and collection is not None:
        raise GeeComposerError(
            "Provide either 'dataset' or 'collection', not both."
        )

    if dataset is not None:
        name = validate_dataset(dataset)
        if name not in _DATASET_MODULES:
            raise DatasetNotSupportedError(
                f"Dataset '{name}' is validated but has no loader module yet."
            )
        mod = _DATASET_MODULES[name]
        return name, mod.get_collection_id(), mod

    if collection is not None:
        # Raw collection path — no dataset-specific module.
        # Use a thin generic loader.
        return None, collection, _GenericLoader(collection)

    raise GeeComposerError(
        "Either 'dataset' or 'collection' must be provided."
    )


class _GenericLoader:
    """Minimal loader for raw collection IDs without a dataset module.

    Provides the same interface as dataset modules (``load_collection``,
    ``apply_mask``, ``get_collection_id``) so ``compose()`` can treat all
    collections uniformly.
    """

    def __init__(self, collection_id: str) -> None:
        self._collection_id = collection_id

    def get_collection_id(self) -> str:
        return self._collection_id

    def load_collection(
        self,
        aoi: ee.Geometry,
        start: str,
        end: str,
        filters: dict | None = None,
    ) -> ee.ImageCollection:
        return (
            ee.ImageCollection(self._collection_id)
            .filterBounds(aoi)
            .filterDate(start, end)
        )

    def apply_mask(self, collection: ee.ImageCollection, mask: str) -> ee.ImageCollection:
        raise GeeComposerError(
            f"Masking is not supported for raw collection '{self._collection_id}'. "
            "Use a dataset preset or apply masking manually before calling compose()."
        )
