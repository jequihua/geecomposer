"""Exact local Rao Q and generalizable normalization.

Graphs are lazy except ``fit_normalization``: it retrieves bounded sufficient
statistics, never pixels. All spatial operations use an explicit affine grid.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import combinations
import json
import math

import ee
import numpy as np


@dataclass(frozen=True)
class RaoGrid:
    """Output CRS and six affine coefficients, in EE row-major order."""

    crs: str
    transform: tuple[float, ...]

    def __post_init__(self):
        if not isinstance(self.crs, str) or not self.crs.strip():
            raise ValueError("crs must be a nonempty string")
        values = tuple(float(x) for x in self.transform)
        if len(values) != 6 or not all(math.isfinite(x) for x in values):
            raise ValueError("transform must contain six finite coefficients")
        if values[0] * values[4] == values[1] * values[3]:
            raise ValueError("transform must be invertible")
        object.__setattr__(self, "transform", values)

    def projection(self):
        return ee.Projection(self.crs, list(self.transform))

    def reduction(self, region, max_pixels):
        _positive_int(max_pixels, "max_pixels")
        return dict(
            geometry=region,
            crs=self.crs,
            crsTransform=list(self.transform),
            maxPixels=max_pixels,
            bestEffort=False,
        )


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _bands(bands):
    if isinstance(bands, str):
        bands = (bands,)
    else:
        bands = tuple(bands)
    if (
        not bands
        or len(bands) > 12
        or len(set(bands)) != len(bands)
        or any(not isinstance(b, str) or not b.strip() for b in bands)
    ):
        raise ValueError("select 1..12 unique, nonempty band names")
    return bands


def _decimals(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 9):
        raise ValueError("decimal_places must be None or an integer in 0..9")


def quantize(image, decimal_places):
    """Decimal rounding, with explicit ties-to-even (including negative ties)."""
    _decimals(decimal_places)
    image = ee.Image(image).toDouble()
    if decimal_places is None:
        return image
    factor = 10**decimal_places
    scaled = image.multiply(factor)
    lower = scaled.floor()
    fraction = scaled.subtract(lower)
    up = fraction.gt(0.5).Or(fraction.eq(0.5).And(lower.mod(2).neq(0)))
    return lower.add(up).divide(factor)


def _prepare(image, bands, grid, region, decimal_places):
    image = ee.Image(image).select(list(bands)).toDouble().reproject(grid.projection())
    # Comparisons reject NaN and infinities without substituting observations.
    valid = (
        image.mask()
        .reduce(ee.Reducer.min())
        .gt(0)
        .And(image.abs().lte(float(np.finfo(float).max)).reduce(ee.Reducer.min()))
    )
    image = image.updateMask(valid)
    if region is not None:
        image = image.clip(region)
    return quantize(image, decimal_places)


@dataclass(frozen=True)
class RaoNormalization:
    """Immutable calibration. Loadings are rows; PCA is never whitened."""

    bands: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    loadings: tuple[tuple[float, ...], ...]
    retained_fraction: float
    n_sigmas: float
    sample_count: int
    decimal_places: int | None
    calibration: str

    def __post_init__(self):
        bands = _bands(self.bands)
        means = tuple(float(x) for x in self.means)
        scales = tuple(float(x) for x in self.scales)
        loadings = tuple(tuple(float(x) for x in row) for row in self.loadings)
        p = len(bands)
        if len(means) != p or len(scales) != p or not 1 <= len(loadings) <= p:
            raise ValueError("calibration dimension mismatch")
        if any(len(row) != p for row in loadings):
            raise ValueError("loading dimension mismatch")
        if not np.isfinite([*means, *scales, *np.asarray(loadings).ravel()]).all():
            raise ValueError("calibration must be finite")
        if any(x <= 0 for x in scales):
            raise ValueError("scales must be positive")
        if not math.isfinite(self.n_sigmas) or self.n_sigmas <= 0:
            raise ValueError("n_sigmas must be positive and finite")
        if not 0 < self.retained_fraction <= 1:
            raise ValueError("retained_fraction must lie in (0, 1]")
        _positive_int(self.sample_count, "sample_count")
        if self.sample_count < 2:
            raise ValueError("at least two reference samples are required")
        if not isinstance(self.calibration, str) or not self.calibration:
            raise ValueError("calibration must identify the reference domain")
        _decimals(self.decimal_places)
        if not np.allclose(
            np.asarray(loadings) @ np.asarray(loadings).T,
            np.eye(len(loadings)),
            atol=1e-10,
            rtol=1e-8,
        ):
            raise ValueError("loadings must be orthonormal (unwhitened PCA)")
        for key, value in (
            ("bands", bands),
            ("means", means),
            ("scales", scales),
            ("loadings", loadings),
        ):
            object.__setattr__(self, key, value)

    @property
    def bound(self):
        return (
            2
            * self.n_sigmas
            * math.sqrt(math.ceil(len(self.bands) * self.retained_fraction))
        )

    @property
    def fingerprint(self):
        return sha256(self.to_json().encode()).hexdigest()

    def to_json(self):
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    @classmethod
    def from_json(cls, value):
        return cls(**json.loads(value))

    @classmethod
    def from_statistics(
        cls,
        bands,
        means,
        covariance,
        sample_count,
        *,
        variance_fraction=0.98,
        n_sigmas=6.0,
        decimal_places=None,
        calibration,
    ):
        """Fit from population covariance (ddof=0), not sample covariance."""
        bands = _bands(bands)
        p = len(bands)
        covariance = np.asarray(covariance, dtype=float)
        if covariance.shape != (p, p) or not np.isfinite(covariance).all():
            raise ValueError("invalid covariance")
        if not np.allclose(covariance, covariance.T, atol=1e-12, rtol=1e-10):
            raise ValueError("covariance must be symmetric")
        if np.any(np.diag(covariance) < 0):
            raise ValueError("negative variance")
        if variance_fraction is not None and not 0 < variance_fraction <= 1:
            raise ValueError("variance_fraction must be None or in (0, 1]")
        scales = np.sqrt(np.diag(covariance))
        scales = np.where(scales == 0, 1.0, scales)
        standardized = covariance / np.outer(scales, scales)
        eigenvalues, vectors = np.linalg.eigh(standardized)
        if eigenvalues.min() < -1e-10:
            raise ValueError("covariance is not positive semidefinite")
        eigenvalues = np.maximum(eigenvalues[::-1], 0)
        vectors = vectors[:, ::-1].T
        total = eigenvalues.sum()
        fraction = 1.0
        if variance_fraction is None or total == 0 or p == 1:
            vectors = np.eye(p)
        else:
            cumulative = np.cumsum(eigenvalues) / total
            k = min(p, int(np.searchsorted(cumulative, variance_fraction)) + 1)
            # Do not split a tied eigenspace arbitrarily.
            while k < p and np.isclose(
                eigenvalues[k], eigenvalues[k - 1], rtol=1e-12, atol=0
            ):
                k += 1
            vectors = vectors[:k]
            fraction = min(1.0, float(eigenvalues[:k].sum() / total))
        # Deterministic signs for persisted calibration fingerprints.
        for row in vectors:
            if row[np.argmax(np.abs(row))] < 0:
                row *= -1
        return cls(
            bands,
            tuple(means),
            tuple(scales),
            tuple(map(tuple, vectors)),
            fraction,
            float(n_sigmas),
            sample_count,
            decimal_places,
            calibration,
        )

    def apply(self, image):
        z = (
            ee.Image(image)
            .select(list(self.bands))
            .subtract(ee.Image.constant(list(self.means)))
            .divide(ee.Image.constant(list(self.scales)))
        )
        pcs = [
            z.multiply(ee.Image.constant(list(row)))
            .reduce(ee.Reducer.sum())
            .rename(f"pc_{i}")
            for i, row in enumerate(self.loadings)
        ]
        return ee.Image.cat(pcs).toDouble()


def fit_normalization(
    image,
    bands,
    *,
    grid,
    region,
    variance_fraction=0.98,
    n_sigmas=6.0,
    decimal_places=None,
    max_pixels=1_000_000,
    calibration=None,
):
    """Evaluate two small reductions to fit GN. Never call inside ``Collection.map``.

    The fit counts grid centers equally, uses population variance and fails
    rather than sampling or coarsening when ``max_pixels`` is exceeded.
    """
    bands = _bands(bands)
    if region is None:
        raise ValueError("an explicit reference region is required")
    image = _prepare(image, bands, grid, region, decimal_places)
    args = grid.reduction(region, max_pixels)
    summary = image.reduceRegion(
        ee.Reducer.mean()
        .unweighted()
        .combine(ee.Reducer.count(), sharedInputs=True)
        .combine(ee.Reducer.minMax().unweighted(), sharedInputs=True),
        **args,
    ).getInfo()
    n = summary.get(bands[0] + "_count", 0) or 0
    if n < 2:
        raise ValueError(
            "normalization needs at least two common valid reference pixels"
        )
    # Extrema identify exact constants without suppressing real small variance.
    # A reduced mean of a non-dyadic constant can contain summation roundoff.
    constant = [summary[b + "_min"] == summary[b + "_max"] for b in bands]
    means = [
        summary[b + ("_min" if fixed else "_mean")] for b, fixed in zip(bands, constant)
    ]
    centered = image.subtract(ee.Image.constant(means))
    pairs = [(i, j) for i in range(len(bands)) for j in range(i, len(bands))]
    products = ee.Image.cat(
        [
            centered.select(i).multiply(centered.select(j)).rename(f"c_{i}_{j}")
            for i, j in pairs
        ]
    )
    sums = products.reduceRegion(ee.Reducer.sum().unweighted(), **args).getInfo()
    covariance = np.zeros((len(bands), len(bands)))
    for i, j in pairs:
        covariance[i, j] = covariance[j, i] = (
            0.0 if constant[i] or constant[j] else sums[f"c_{i}_{j}"] / n
        )
    if calibration is None:
        calibration = json.dumps(
            dict(region=ee.Geometry(region).serialize(), grid=asdict(grid)),
            sort_keys=True,
        )
    return RaoNormalization.from_statistics(
        bands,
        means,
        covariance,
        int(n),
        variance_fraction=variance_fraction,
        n_sigmas=n_sigmas,
        decimal_places=decimal_places,
        calibration=calibration,
    )


def rao_q(
    image,
    bands,
    *,
    grid,
    region=None,
    window_size=3,
    model=None,
    decimal_places=None,
    missing="strict",
    min_valid=None,
):
    """Build exact Rao Q plus ``rao_valid_count`` and ``rao_coverage`` bands.

    ``model=None`` returns ``rao_raw``; a frozen calibration returns ``rao_gn``.
    ``region`` is a true sampling boundary. For processing tiles, pass the full
    analysis region here and crop/export tiles only AFTER building this graph.
    """
    bands = _bands(bands)
    if not isinstance(grid, RaoGrid):
        raise ValueError("grid must be a RaoGrid")
    if type(window_size) is not int or window_size not in (1, 3, 5, 7):
        raise ValueError("window_size must be 1, 3, 5 or 7 (exact pair-cost bound)")
    if missing not in ("strict", "valid_subset"):
        raise ValueError("missing must be strict or valid_subset")
    size = window_size**2
    if missing == "strict":
        if min_valid is not None:
            raise ValueError("min_valid applies only to valid_subset")
        required = size
    else:
        _positive_int(min_valid, "min_valid")
        if min_valid > size:
            raise ValueError("min_valid exceeds window area")
        required = min_valid
    if model is not None:
        if model.bands != bands or model.decimal_places != decimal_places:
            raise ValueError(
                "model bands/quantization must match the requested transform"
            )
    prepared = _prepare(image, bands, grid, region, decimal_places)
    valid = (
        prepared.mask().reduce(ee.Reducer.min()).gt(0).unmask(0, sameFootprint=False)
    )
    values = model.apply(prepared) if model is not None else prepared
    kernel = ee.Kernel.square((window_size - 1) // 2, "pixels", False)
    arrays = values.unmask(0, sameFootprint=False).neighborhoodToArray(kernel)
    flags = valid.neighborhoodToArray(kernel)
    positions = [(y, x) for y in range(window_size) for x in range(window_size)]
    neighbors = [arrays.arrayGet(list(pos)) for pos in positions]
    present = [flags.arrayGet(list(pos)).rename("present") for pos in positions]
    count = ee.ImageCollection.fromImages(present).sum().rename("rao_valid_count")
    terms = [
        neighbors[a]
        .subtract(neighbors[b])
        .pow(2)
        .reduce(ee.Reducer.sum())
        .sqrt()
        .multiply(present[a])
        .multiply(present[b])
        .rename("pair")
        for a, b in combinations(range(size), 2)
    ]
    total = (
        ee.ImageCollection.fromImages(terms).sum() if terms else ee.Image.constant(0)
    )
    q = total.multiply(2).divide(count.max(1).pow(2))
    if model is not None:
        q = q.divide(model.bound)
    q = q.rename("rao_gn" if model is not None else "rao_raw").updateMask(
        count.gte(required).And(valid)
    )
    result = q.addBands(count).addBands(count.divide(size).rename("rao_coverage"))
    if region is not None:
        result = result.clip(region)
    return result.reproject(grid.projection()).set(
        {
            "rao:metric": "ordered_pair_euclidean",
            "rao:window_size": window_size,
            "rao:missing": missing,
            "rao:min_valid": required,
            "rao:decimal_places": "none" if decimal_places is None else decimal_places,
            "rao:bands": list(bands),
            "rao:grid": json.dumps(asdict(grid)),
            "rao:normalization": model.to_json() if model else "none",
            "rao:calibration_sha256": model.fingerprint if model else "none",
        }
    )


def rao(bands, **kwargs):
    """Transform factory compatible with ``compose(transform=...)``.

    Normalized collection transforms require an explicitly fitted frozen model.
    The seasonal helper fits independent per-scene models outside server maps.
    """

    def transform(image):
        return rao_q(image, bands, **kwargs)

    transform.__name__ = "rao"
    return transform
