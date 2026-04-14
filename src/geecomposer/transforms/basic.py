"""Basic transform factories.

Each factory returns a callable with signature ``ee.Image -> ee.Image``,
suitable for use as the ``transform`` argument to ``compose()``.
"""

from __future__ import annotations

from typing import Callable

import ee


def select_band(band: str, name: str | None = None) -> Callable[[ee.Image], ee.Image]:
    """Build a transform that selects a single band.

    Parameters
    ----------
    band:
        Band name to select from the input image.
    name:
        Optional rename for the selected band.
    """
    if not isinstance(band, str) or not band:
        raise ValueError("band must be a non-empty string.")

    def _fn(img: ee.Image) -> ee.Image:
        selected = img.select(band)
        if name is not None:
            selected = selected.rename(name)
        return selected

    _fn.__name__ = f"select_band({band!r})"
    return _fn


def normalized_difference(band1: str, band2: str, name: str) -> Callable[[ee.Image], ee.Image]:
    """Build a normalized-difference transform: ``(band1 - band2) / (band1 + band2)``.

    Parameters
    ----------
    band1:
        Name of the first band (numerator positive term).
    band2:
        Name of the second band (numerator negative term).
    name:
        Name for the output band.
    """
    if not isinstance(band1, str) or not band1:
        raise ValueError("band1 must be a non-empty string.")
    if not isinstance(band2, str) or not band2:
        raise ValueError("band2 must be a non-empty string.")
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string.")

    def _fn(img: ee.Image) -> ee.Image:
        return img.normalizedDifference([band1, band2]).rename(name)

    _fn.__name__ = f"normalized_difference({band1!r}, {band2!r})"
    return _fn
