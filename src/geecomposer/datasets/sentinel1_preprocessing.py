"""Sentinel-1 mono-temporal speckle filtering helpers.

This module provides one opt-in mono-temporal Gamma MAP speckle filter for
linear-unit Sentinel-1 imagery (the ``sentinel1_float`` preset).

The helper is wired through the existing ``compose(..., preprocess=...)``
stage. Filtering is **not** auto-applied: callers must opt in by passing
the helper through the preprocess slot.

Scope is intentionally narrow:

- mono-temporal, per-image filtering only
- Gamma MAP only — no filter menu, no Refined Lee, no Lee Sigma, no Boxcar
- linear-unit input only — applying this to dB-scaled imagery produces
  misleading values
- no terrain flattening, no border-noise correction, no tide filtering,
  no SAR texture features

This is not a Sentinel-1 ARD framework. See ``docs/baseline_repairs.md``.
"""

from __future__ import annotations

import math
from typing import Callable

import ee

from ..exceptions import GeeComposerError

# Equivalent number of looks for Sentinel-1 IW GRD. Fixed internally to
# match the documented practical assumption for this helper.
_ENL = 5


def gamma_map(kernel_size: int = 7) -> Callable[[ee.Image], ee.Image]:
    """Build a mono-temporal Gamma MAP speckle filter.

    The returned callable takes one ``ee.Image`` and returns one
    ``ee.Image``, suitable for ``compose(..., preprocess=...)``. It is
    intended for the ``sentinel1_float`` preset (linear-unit Sentinel-1
    imagery). Applying this filter to dB-scaled imagery produces
    misleading values.

    The filter operates on the backscatter bands of the image. The
    ``angle`` band is preserved unchanged when present. Filtered values
    replace the original backscatter bands while band names and order
    are preserved.

    Parameters
    ----------
    kernel_size:
        Square neighbourhood window size in pixels. Must be a positive
        odd integer. Defaults to 7.

    Returns
    -------
    Callable[[ee.Image], ee.Image]
        A per-image callable usable with ``compose(..., preprocess=...)``.

    Raises
    ------
    GeeComposerError
        If *kernel_size* is not a positive odd integer.

    Notes
    -----
    Implements the per-image Gamma MAP filter described in:
    Lopes A., Nezry E., Touzi R., Laur H. (1990). Maximum A Posteriori
    Speckle Filtering and First Order Texture Models in SAR Images.
    International Geoscience and Remote Sensing Symposium (IGARSS).

    The equivalent number of looks (ENL) is fixed at 5, matching the
    practical assumption for this Sentinel-1 IW GRD helper.

    This helper is mono-temporal and per-image. Multi-temporal filtering,
    radiometric terrain flattening, and additional border-noise correction
    are intentionally not part of this helper.
    """
    _validate_kernel_size(kernel_size)
    radius = (kernel_size - 1) // 2

    def _gamma_map(image: ee.Image) -> ee.Image:
        band_names = image.bandNames().remove("angle")

        reducers = ee.Reducer.mean().combine(
            reducer2=ee.Reducer.stdDev(), sharedInputs=True
        )
        stats = image.select(band_names).reduceNeighborhood(
            reducer=reducers,
            kernel=ee.Kernel.square(radius, "pixels"),
            optimization="window",
        )

        mean_bands = band_names.map(lambda b: ee.String(b).cat("_mean"))
        std_bands = band_names.map(lambda b: ee.String(b).cat("_stdDev"))
        z = stats.select(mean_bands)
        sig_z = stats.select(std_bands)

        filtered = _gamma_map_estimate(
            image.select(band_names), z.rename(band_names), sig_z.rename(band_names)
        )
        return image.addBands(srcImg=filtered, names=None, overwrite=True)

    _gamma_map.__name__ = f"gamma_map(kernel_size={kernel_size})"
    return _gamma_map


def _gamma_map_estimate(x, mean, stddev):
    """Gamma-prior MAP root; preserve the original center mask.

    Discriminant: mean**2 * (alpha - ENL - 1)**2 + 4*alpha*ENL*mean*x.
    Zero-mean nonnegative windows are homogeneous zero windows. Unused
    branches have safe denominators so threshold/constant cases stay finite.
    """
    cu = 1 / math.sqrt(_ENL)
    cmax = math.sqrt(2) * cu
    ci = stddev.divide(mean.where(mean.eq(0), 1))
    denominator = ci.pow(2).subtract(cu**2)
    alpha = denominator.where(denominator.lte(0), 1).pow(-1).multiply(1 + cu**2)
    b = alpha.subtract(_ENL + 1)
    discriminant = (
        mean.pow(2)
        .multiply(b.pow(2))
        .add(alpha.multiply(4 * _ENL).multiply(mean).multiply(x))
    )
    estimate = mean.multiply(b).add(discriminant.sqrt()).divide(alpha.multiply(2))
    return (
        mean.where(ci.gt(cu).And(ci.lt(cmax)), estimate)
        .where(ci.gte(cmax), x)
        .updateMask(x.mask())
    )


def _validate_kernel_size(kernel_size: int) -> None:
    """Validate *kernel_size* is a positive odd integer.

    Booleans are rejected even though ``bool`` is a subclass of ``int``.
    """
    if isinstance(kernel_size, bool) or not isinstance(kernel_size, int):
        raise GeeComposerError(
            f"kernel_size must be a positive odd integer, got "
            f"{type(kernel_size).__name__}."
        )
    if kernel_size < 1:
        raise GeeComposerError(
            f"kernel_size must be a positive odd integer, got {kernel_size}."
        )
    if kernel_size % 2 == 0:
        raise GeeComposerError(
            f"kernel_size must be a positive odd integer (got even {kernel_size})."
        )
