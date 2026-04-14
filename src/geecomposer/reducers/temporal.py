"""Temporal reducer mapping.

Maps string reducer names to Earth Engine ``ImageCollection`` reduction methods.
"""

from __future__ import annotations

from typing import Callable

import ee

from ..validation import validate_reducer

# Mapping from reducer name to a callable that reduces an ee.ImageCollection.
_REDUCER_MAP: dict[str, Callable[[ee.ImageCollection], ee.Image]] = {
    "median": lambda col: col.median(),
    "mean": lambda col: col.mean(),
    "min": lambda col: col.min(),
    "max": lambda col: col.max(),
    "mosaic": lambda col: col.mosaic(),
}


def apply_reducer(collection: ee.ImageCollection, reducer_name: str) -> ee.Image:
    """Apply a named temporal reducer to an image collection.

    Parameters
    ----------
    collection:
        The ``ee.ImageCollection`` to reduce.
    reducer_name:
        One of ``"median"``, ``"mean"``, ``"min"``, ``"max"``, ``"mosaic"``.

    Returns
    -------
    ee.Image
        The reduced composite image.

    Raises
    ------
    InvalidReducerError
        If *reducer_name* is not a supported reducer.
    """
    validated = validate_reducer(reducer_name)
    reducer_fn = _REDUCER_MAP[validated]
    return reducer_fn(collection)
