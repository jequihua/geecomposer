"""Bounded, explicit per-observation NIRv Rao workflow.

This is a client orchestrator, not a mapped EE function. Small summary requests
are synchronous; images stay server-side and no exports are started.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import ee
import numpy as np

from .rao import RaoGrid, fit_normalization, rao_q, _positive_int, _decimals
from .datasets.sentinel2 import COLLECTION_ID
from .validation import date_interval as _interval
from .transforms.indices import nirv

WORLD_COVER_CLASSES = (10, 20, 30, 40, 60, 90, 95, 100)


class NoRaoDataError(ValueError):
    """No scientifically usable observations or season remain."""


def _classes(values, name, allowed):
    values = tuple(values)
    if not values or any(type(x) is not int or x not in allowed for x in values):
        raise ValueError(f"invalid {name}")
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {name}")
    return values


def prepare_sentinel2_nirv(
    image,
    *,
    grid,
    region,
    scl_classes=(4, 5),
    world_cover_classes=WORLD_COVER_CLASSES,
    world_cover=None,
):
    """Strict common-support reflectance means, then NDVI and NIRv.

    SCL and WorldCover are sampled categorically on the native red-band grid.
    Every contributing fine pixel must be valid. The supplied grid normally has
    aligned 20 m cells; continuous reflectance is area-averaged, never class codes.
    ``world_cover_classes=None`` explicitly disables the fixed land-cover mask.
    """
    if not isinstance(grid, RaoGrid):
        raise ValueError("grid must be a RaoGrid")
    scl_classes = _classes(scl_classes, "SCL classes", range(12))
    if world_cover_classes is not None:
        world_cover_classes = _classes(
            world_cover_classes,
            "WorldCover classes",
            (10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100),
        )
    image = ee.Image(image)
    native = image.select("B4").projection()
    source = image.select(["B4", "B8"]).toDouble().reproject(native)
    scl = image.select("SCL").remap(list(scl_classes), [1] * len(scl_classes), 0)
    valid = source.mask().reduce(ee.Reducer.min()).gt(0).And(scl.reproject(native))
    if world_cover_classes is not None:
        cover = (
            ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
            if world_cover is None
            else ee.Image(world_cover)
        )
        land = cover.remap(list(world_cover_classes), [1] * len(world_cover_classes), 0)
        valid = valid.And(land.reproject(native))
    valid = valid.unmask(0, sameFootprint=False).reproject(native)
    support = (
        valid.toDouble()
        .reduceResolution(ee.Reducer.mean(), bestEffort=False, maxPixels=1024)
        .reproject(grid.projection())
    )
    reflectance = (
        source.updateMask(valid)
        .multiply(0.0001)
        .reduceResolution(ee.Reducer.mean(), bestEffort=False, maxPixels=1024)
        .reproject(grid.projection())
    )
    reflectance = reflectance.updateMask(support.gte(1 - 1e-12))
    n, r = reflectance.select("B8"), reflectance.select("B4")
    denominator = n.add(r)
    ndvi = (
        n.subtract(r).divide(denominator).updateMask(denominator.neq(0)).rename("ndvi")
    )
    return ee.Image(
        nirv()(reflectance)
        .addBands(ndvi)
        .clip(region)
        .copyProperties(image, ["system:time_start", "system:index"])
        .set(
            {
                "rao:reflectance_scale": 0.0001,
                "rao:scl_classes": list(scl_classes),
                "rao:worldcover": (
                    ("ESA/WorldCover/v200" if world_cover is None else "custom")
                    if world_cover_classes is not None
                    else "disabled"
                ),
                "rao:worldcover_classes": (
                    list(world_cover_classes)
                    if world_cover_classes is not None
                    else "disabled"
                ),
                "rao:fine_support": "all_contributors",
            }
        )
    )


def exact_quantile(values, probability=0.9):
    """R type-7/NumPy linear quantile, never EE's histogram approximation."""
    values = sorted(float(x) for x in values)
    if not values or not all(math.isfinite(x) for x in values):
        raise NoRaoDataError("quantile requires nonempty finite scene summaries")
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("quantile probability must lie in [0,1]")
    index = (len(values) - 1) * probability
    left = math.floor(index)
    right = math.ceil(index)
    return values[left] + (index - left) * (values[right] - values[left])


def _in_interval(milliseconds, interval):
    # Convert UTC milliseconds without host timezone dependence.
    from datetime import datetime, timezone

    day = datetime.fromtimestamp(milliseconds / 1000, timezone.utc).date()
    return interval[0] <= day < interval[1]


def acquisition_groups(metadata):
    """One spacecraft/orbit/UTC-day observation; sorted indices set mosaic priority.

    Later system:index wins overlaps among valid prepared pixels. Different
    spacecraft or orbits remain separate observations. Missing identity fails.
    """
    groups = {}
    seen = set()
    for item in metadata:
        if any(item.get(k) is None for k in ("id", "time", "spacecraft", "orbit")):
            raise ValueError("scene lacks acquisition identity metadata")
        if item["id"] in seen:
            raise ValueError("duplicate source scene id")
        seen.add(item["id"])
        key = (item["spacecraft"], item["orbit"], int(item["time"]) // 86_400_000)
        groups.setdefault(key, []).append(item)
    return [
        sorted(items, key=lambda x: x["id"])
        for _, items in sorted(
            groups.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])
        )
    ]


def select_season(
    rows,
    reference_interval,
    output_interval,
    *,
    quantile=0.9,
    minimum_coverage=0.5,
    comparison="gt",
):
    """Select quality-accepted rows and exact reference threshold without EE."""
    if not 0 <= minimum_coverage <= 1 or not math.isfinite(minimum_coverage):
        raise ValueError("minimum_coverage must lie in [0,1]")
    if comparison not in ("gt", "ge"):
        raise ValueError("comparison must be gt or ge")
    quality = [
        row
        for row in rows
        if row.get("median_ndvi") is not None
        and math.isfinite(row["median_ndvi"])
        and row.get("median_rao") is not None
        and math.isfinite(row["median_rao"])
        and row["input_coverage"] >= minimum_coverage
        and row["valid_windows"] > 0
    ]
    reference = [
        row["median_ndvi"]
        for row in quality
        if _in_interval(row["time"], reference_interval)
    ]
    threshold = exact_quantile(reference, quantile)
    selected = [
        row
        for row in quality
        if _in_interval(row["time"], output_interval)
        and (
            row["median_ndvi"] > threshold
            if comparison == "gt"
            else row["median_ndvi"] >= threshold
        )
    ]
    if not selected:
        raise NoRaoDataError("no observations pass peak-season selection")
    return selected, threshold


@dataclass(frozen=True)
class SeasonalRaoResult:
    mean_image: ee.Image
    observation_count: ee.Image
    scene_images: tuple[ee.Image, ...]
    scene_summaries: tuple[dict, ...]
    site_estimate: float
    threshold: float
    provenance: dict

    def summary_table(self):
        """A geometry-free table, suitable for a separate explicit table export."""
        return ee.FeatureCollection(
            [ee.Feature(None, row) for row in self.scene_summaries]
        )


def nirv_seasonal_rao(
    *,
    region,
    grid,
    start,
    end,
    reference_start=None,
    reference_end=None,
    window_size=3,
    decimal_places=2,
    quantile=0.9,
    comparison="gt",
    minimum_coverage=0.5,
    scl_classes=(4, 5),
    world_cover_classes=WORLD_COVER_CLASSES,
    max_scenes=1000,
    max_pixels=1_000_000,
    model=None,
    collection=None,
):
    """Evaluate bounded scene summaries; return lazy selected-image products.

    Reference and output intervals are independent, half-open. The default
    normalization is fitted independently per grouped observation. A frozen
    model is an explicitly distinct alternative. No automatic export/auth.
    """
    output_interval = _interval(start, end)
    reference_start = reference_start or start
    reference_end = reference_end or end
    reference_interval = _interval(reference_start, reference_end)
    _positive_int(max_scenes, "max_scenes")
    _positive_int(max_pixels, "max_pixels")
    _decimals(decimal_places)
    if model is not None and (
        model.bands != ("nirv",) or model.decimal_places != decimal_places
    ):
        raise ValueError("model bands/quantization must match the requested transform")
    if type(window_size) is not int or window_size not in (1, 3, 5, 7):
        raise ValueError("window_size must be 1, 3, 5 or 7")
    if not math.isfinite(quantile) or not 0 <= quantile <= 1:
        raise ValueError("quantile must lie in [0,1]")
    if not math.isfinite(minimum_coverage) or not 0 <= minimum_coverage <= 1:
        raise ValueError("minimum_coverage must lie in [0,1]")
    if comparison not in ("gt", "ge"):
        raise ValueError("comparison must be gt or ge")
    args = grid.reduction(region, max_pixels)
    col = (
        ee.ImageCollection(COLLECTION_ID)
        if collection is None
        else ee.ImageCollection(collection)
    )
    col = col.filterBounds(region).filterDate(
        min(start, reference_start), max(end, reference_end)
    )
    count = col.size().getInfo()
    if count == 0:
        raise NoRaoDataError("no input Sentinel observations")
    if count > max_scenes:
        raise ValueError(
            "scene limit exceeded; narrow the interval/region, never truncate"
        )
    items = col.sort("system:index").toList(count)

    def identity(obj):
        obj = ee.Image(obj)
        return ee.Dictionary(
            {
                "id": obj.get("system:index"),
                "time": obj.get("system:time_start"),
                "spacecraft": obj.get("SPACECRAFT_NAME"),
                "orbit": obj.get("SENSING_ORBIT_NUMBER"),
            }
        )

    metadata = items.map(identity).getInfo()
    groups = acquisition_groups(metadata)
    total_pixels = (
        ee.Image.constant(1)
        .rename("total")
        .reproject(grid.projection())
        .reduceRegion(ee.Reducer.count(), **args)
        .getInfo()
        .get("total", 0)
    )
    if not total_pixels:
        raise NoRaoDataError("region contains no analysis-grid centers")
    rows, images = [], {}
    # Set maxRaw to the explicit pixel budget so EE median cannot switch to a
    # histogram within the admitted region. Fail on resource limits, never coarsen.
    median = ee.Reducer.median(maxRaw=max_pixels)
    summary_reducer = median.unweighted().combine(ee.Reducer.count(), sharedInputs=True)
    for group in groups:
        ids = [item["id"] for item in group]
        prepared = (
            col.filter(ee.Filter.inList("system:index", ids))
            .sort("system:index")
            .map(
                lambda image: prepare_sentinel2_nirv(
                    image,
                    grid=grid,
                    region=region,
                    scl_classes=scl_classes,
                    world_cover_classes=world_cover_classes,
                )
            )
        )
        observation = prepared.mosaic().setDefaultProjection(grid.projection())
        summary = (
            observation.select("ndvi").reduceRegion(summary_reducer, **args).getInfo()
        )
        valid_pixels = summary.get("ndvi_count", 0) or 0
        row = dict(
            id="|".join(ids),
            source_ids=ids,
            time=min(x["time"] for x in group),
            input_coverage=valid_pixels / total_pixels,
            valid_windows=0,
            window_coverage=0.0,
            median_ndvi=summary.get("ndvi_median"),
            median_rao=None,
            selected=False,
            exclusion=(
                "input_coverage"
                if valid_pixels / total_pixels < minimum_coverage
                else "insufficient_calibration_pixels"
            ),
        )
        if valid_pixels >= 2 and row["input_coverage"] >= minimum_coverage:
            calibration = model or fit_normalization(
                observation,
                ["nirv"],
                grid=grid,
                region=region,
                decimal_places=decimal_places,
                max_pixels=max_pixels,
            )
            q = rao_q(
                observation,
                ["nirv"],
                grid=grid,
                region=region,
                model=calibration,
                window_size=window_size,
                decimal_places=decimal_places,
            )
            q_summary = (
                q.select("rao_gn").reduceRegion(summary_reducer, **args).getInfo()
            )
            row.update(
                median_rao=q_summary.get("rao_gn_median"),
                valid_windows=q_summary.get("rao_gn_count", 0) or 0,
                calibration_sha256=calibration.fingerprint,
                normalization=calibration.to_json(),
            )
            row["window_coverage"] = row["valid_windows"] / total_pixels
            row["exclusion"] = (
                "no_valid_windows" if not row["valid_windows"] else "outside_season"
            )
            images[row["id"]] = q.set(
                {"system:time_start": row["time"], "rao:source_ids": ids}
            )
        rows.append(row)
    selected, threshold = select_season(
        rows,
        reference_interval,
        output_interval,
        quantile=quantile,
        minimum_coverage=minimum_coverage,
        comparison=comparison,
    )
    for row in selected:
        row.update(selected=True, exclusion="")
    selected_images = tuple(images[row["id"]] for row in selected)
    rasters = ee.ImageCollection.fromImages(
        [image.select("rao_gn") for image in selected_images]
    )
    provenance = dict(
        protocol="combined-1",
        collection=(
            COLLECTION_ID
            if collection is None
            else collection if isinstance(collection, str) else "custom"
        ),
        max_pixels=max_pixels,
        max_scenes=max_scenes,
        start=start,
        end=end,
        reference_start=reference_start,
        reference_end=reference_end,
        threshold=threshold,
        quantile=quantile,
        comparison=comparison,
        minimum_coverage=minimum_coverage,
        normalization="frozen" if model else "per_observation",
        grouping="spacecraft_orbit_utc_day_sorted_index_last_valid",
        input_scenes=count,
        observations=len(groups),
        selected_observations=len(selected),
        grid_crs=grid.crs,
        grid_transform=list(grid.transform),
        window_size=window_size,
        decimal_places="none" if decimal_places is None else decimal_places,
        scl_classes=list(scl_classes),
        world_cover_dataset=(
            "ESA/WorldCover/v200" if world_cover_classes is not None else "disabled"
        ),
        reflectance_scale=0.0001,
        fine_support="all_contributors",
        world_cover_classes=(
            list(world_cover_classes) if world_cover_classes is not None else "disabled"
        ),
    )
    return SeasonalRaoResult(
        rasters.mean()
        .rename("rao_gn_mean")
        .reproject(grid.projection())
        .set(provenance),
        rasters.count()
        .unmask(0, sameFootprint=False)
        .clip(region)
        .rename("rao_observation_count")
        .reproject(grid.projection())
        .set(provenance),
        selected_images,
        tuple(rows),
        float(np.mean([r["median_rao"] for r in selected])),
        threshold,
        provenance,
    )
