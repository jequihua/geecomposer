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

This is not a Sentinel-1 ARD framework. See
``docs/GEECOMPOSER_MILESTONE_007_S1_FLOAT_SPECKLE_FILTERING.md`` for the
full context.
"""

from __future__ import annotations

import math
from typing import Callable

import ee

from ..exceptions import GeeComposerError

# Equivalent number of looks for Sentinel-1 IW GRD. Fixed internally to
# match the practical assumption used in the reference SAR ARD code in
# 90_legacy_review/s1_correction.
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
    practical assumption for Sentinel-1 IW GRD used in the reference
    SAR ARD code at ``90_legacy_review/s1_correction``.

    This helper is mono-temporal and per-image. Multi-temporal filtering,
    radiometric terrain flattening, and additional border-noise correction
    are intentionally not part of this helper.
    """
    _validate_kernel_size(kernel_size)
    radius = kernel_size / 2

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

        ci = sig_z.divide(z)
        cu_val = 1.0 / math.sqrt(_ENL)
        cmax_val = math.sqrt(2.0) * cu_val

        cu = ee.Image.constant(cu_val)
        cmax = ee.Image.constant(cmax_val)
        enl_img = ee.Image.constant(_ENL)
        one_img = ee.Image.constant(1)
        two_img = ee.Image.constant(2)

        alpha = one_img.add(cu.pow(2)).divide(ci.pow(2).subtract(cu.pow(2)))

        q = image.select(band_names).expression(
            "z**2 * (z * alpha - enl - 1)**2 + 4 * alpha * enl * b() * z",
            {"z": z, "alpha": alpha, "enl": _ENL},
        )
        r_hat = (
            z.multiply(alpha.subtract(enl_img).subtract(one_img))
            .add(q.sqrt())
            .divide(two_img.multiply(alpha))
        )

        # ci <= cu       -> homogeneous region; use neighbourhood mean
        z_hat = z.updateMask(ci.lte(cu)).rename(band_names)
        # cu < ci < cmax -> textured medium; use Gamma MAP estimate
        r_hat = (
            r_hat.updateMask(ci.gt(cu))
            .updateMask(ci.lt(cmax))
            .rename(band_names)
        )
        # ci >= cmax     -> strong scatterer; retain original value
        x = image.select(band_names).updateMask(ci.gte(cmax)).rename(band_names)

        filtered = ee.ImageCollection([z_hat, r_hat, x]).sum()
        return image.addBands(srcImg=filtered, names=None, overwrite=True)

    _gamma_map.__name__ = f"gamma_map(kernel_size={kernel_size})"
    return _gamma_map


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
