"""Index transform factories.

Convenience wrappers around ``normalized_difference`` for common spectral
indices.  Each returns a callable ``ee.Image -> ee.Image``.
"""

from __future__ import annotations

from typing import Callable

import ee
import math

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


def nirv(nir="B8", red="B4", name="nirv", reflectance_scale=1.0):
    """NIR reflectance times NDVI; use scale=.0001 for unscaled S2 DN.

    Unlike normalizedDifference, explicit arithmetic preserves negative inputs.
    A zero NIR+red denominator is masked. For the scientific S2 profile, aggregate
    reflectance to the analysis grid BEFORE applying this transform.
    """
    if any(not isinstance(x, str) or not x.strip() for x in (nir, red, name)):
        raise ValueError("nir, red and name must be nonempty band names")
    if not math.isfinite(reflectance_scale) or reflectance_scale <= 0:
        raise ValueError("reflectance_scale must be positive and finite")

    def transform(image):
        n = image.select(nir).toDouble().multiply(reflectance_scale)
        r = image.select(red).toDouble().multiply(reflectance_scale)
        denominator = n.add(r)
        return (
            n.multiply(n.subtract(r).divide(denominator))
            .updateMask(denominator.neq(0))
            .rename(name)
        )

    transform.__name__ = "nirv"
    return transform
