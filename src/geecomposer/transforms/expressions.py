"""Expression-based transform factories.

Allows users to build transforms from Earth Engine expression strings with
explicit band mappings.
"""

from __future__ import annotations

from typing import Callable

import ee


def expression_transform(
    expression: str,
    band_map: dict[str, str],
    name: str,
    extra_vars: dict | None = None,
) -> Callable[[ee.Image], ee.Image]:
    """Build a transform from an Earth Engine expression.

    Parameters
    ----------
    expression:
        An Earth Engine expression string (e.g. ``"(nir - red) / (nir + red)"``).
    band_map:
        Mapping from variable names used in *expression* to actual band names
        in the input image.  For example ``{"nir": "B8", "red": "B4"}``.
    name:
        Name for the single output band.
    extra_vars:
        Optional extra constant variables to inject into the expression
        context (not band references).

    Returns
    -------
    Callable[[ee.Image], ee.Image]
        A transform callable suitable for ``compose(transform=...)``.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a non-empty string.")
    if not isinstance(band_map, dict) or not band_map:
        raise ValueError("band_map must be a non-empty dict mapping aliases to band names.")
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string.")

    def _fn(img: ee.Image) -> ee.Image:
        mapped: dict = {alias: img.select(band) for alias, band in band_map.items()}
        if extra_vars:
            mapped.update(extra_vars)
        return img.expression(expression, mapped).rename(name)

    _fn.__name__ = f"expression_transform({name!r})"
    return _fn
