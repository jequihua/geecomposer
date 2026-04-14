"""Index transform factories.

Convenience wrappers around ``normalized_difference`` for common spectral
indices.  Each returns a callable ``ee.Image -> ee.Image``.
"""

from __future__ import annotations

from typing import Callable

import ee

from .basic import normalized_difference


def ndvi(
    nir: str = "B8", red: str = "B4", name: str = "ndvi"
) -> Callable[[ee.Image], ee.Image]:
    """Build an NDVI transform: ``(NIR - Red) / (NIR + Red)``.

    Default band names match Sentinel-2 Surface Reflectance conventions.
    """
    fn = normalized_difference(nir, red, name)
    fn.__name__ = "ndvi"
    return fn
