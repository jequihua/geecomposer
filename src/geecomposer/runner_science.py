"""Recipe-to-native-task adapter using the existing public scientific functions."""

from __future__ import annotations

import re

import ee

from .compose import compose
from .grouping import compose_yearly
from .datasets import sentinel1, sentinel1_float, sentinel2
from .datasets.sentinel1_preprocessing import gamma_map
from .export.drive import export_to_drive
from .export.gcs import export_to_gcs
from .rao import RaoGrid, fit_normalization, rao_q
from .seasonal import NoRaoDataError, nirv_seasonal_rao
from .transforms.indices import ndvi, nirv
from .transforms.expressions import expression_transform
from .recipe import RunnerError, canonical, digest, pixel_bound, require

# Diagnostic step names (runner and preparation); nothing else is ever recorded.
PHASES = frozenset(
    {"initialize", "prepare", "bounds", "scenes", "calibration", "graph", "persist", "allocate",
     "submit", "poll", "outputs"}
)


def tag(exc, name):
    """Record the step a failure interrupted (first tag wins); the exception, its
    status and its propagation are unchanged."""
    try:
        if "phase" not in vars(exc):
            exc.phase = name
    except (TypeError, AttributeError):
        pass


def _snapshot(recipe, region, start, end, saved=None):
    module = {
        "sentinel2": sentinel2,
        "sentinel1": sentinel1,
        "sentinel1_float": sentinel1_float,
    }[recipe["dataset"]]
    maximum = recipe["limits"]["scenes"]
    if saved is None:
        col = module.load_collection(region, start, end, filters=recipe.get("filters"))
        count = col.size().getInfo()
        require(type(count) is int and count > 0, "no input scenes", "no_data")
        require(count <= maximum, "scene limit exceeded")
        # The extra sentinel entry detects catalogue growth between the scalar
        # count and snapshot read. Excess is rejected, never silently truncated.
        saved = col.limit(maximum + 1).aggregate_array("system:index").getInfo()
    require(
        type(saved) is list and 0 < len(saved) <= maximum,
        "scene snapshot exceeds budget",
    )
    require(
        all(type(x) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,240}", x) for x in saved),
        "unusable source scene identities",
        "uncertain",
    )
    require(len(set(saved)) == len(saved), "duplicate source identity", "uncertain")
    ids = sorted(saved)
    return (
        ee.ImageCollection.fromImages([ee.Image(module.COLLECTION_ID + "/" + x) for x in ids]),
        ids,
    )


EXPORT_NODATA = -9999


def export_encoded(product):
    """The runner's final export encoding (internal; applied after all science).

    Masked cells of each band, inside or outside the product's own footprint
    (sameFootprint=False), get the finite EXPORT_NODATA sentinel that the export
    also declares as noData; the native constructor's reprojection and clip to
    the requested region are applied afterwards and unchanged. Valid pixels,
    including zero and any non-finite value at a valid pixel, are not altered.
    Every band is double.
    """
    return product.unmask(EXPORT_NODATA, False).toDouble()


def config_fingerprint(config):
    serializable = dict(config)
    expression = serializable.get("expression")
    if isinstance(expression, ee.ComputedObject):
        serializable["expression"] = ee.serializer.encode(expression, for_cloud_api=True)
    return digest(serializable)


def discovery_marker(request_id, run_binding, product):
    """Opaque, 67-character description binding the original ID to this product."""
    return "gc-" + digest([request_id, run_binding, product])


def request_config(config, marker):
    """Keep the native unstarted config; bind only its supported description."""
    return dict(config, description=marker)


def prepare(recipe, prefix, budget, previous=None):
    """Snapshot sources, check resource bounds, construct unstarted native tasks.

    The runner persists sources and fingerprints before submitting anything.
    Resume uses exactly these sources and rejects a changed configuration.
    prefix is the reserved GCS object prefix, or the Drive file-name base.
    A failure carries the step it interrupted as its diagnostic phase.
    """
    step = ["prepare"]
    try:
        return _prepare(recipe, prefix, budget, previous, step)
    except Exception as exc:
        tag(exc, step[0])
        raise


def _prepare(recipe, prefix, budget, previous, step):
    budget.timeout()
    step[0] = "bounds"
    grid = RaoGrid(**recipe["grid"])
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    # EE computes the enclosing rectangle in the requested CRS, including curved
    # projected edges. Local affine inversion yields a conservative pixel bound.
    coordinates = region.bounds(maxError=0.01, proj=ee.Projection(grid.crs)).coordinates().getInfo()
    require(
        type(coordinates) is list and len(coordinates) == 1 and len(coordinates[0]) <= 10,
        "unusable projected AOI bound",
        "uncertain",
    )
    points = coordinates[0]
    require(len(points) >= 4, "unusable projected AOI bound", "uncertain")
    try:
        bounds = (
            min(x[0] for x in points),
            min(x[1] for x in points),
            max(x[0] for x in points),
            max(x[1] for x in points),
        )
        pixels = pixel_bound(bounds, grid.transform)
    except (TypeError, ValueError, OverflowError, IndexError):
        raise RunnerError("uncertain", "cannot establish output pixel bound") from None
    cap = recipe["limits"]["pixels"]
    workflow = recipe["workflow"]
    if workflow == "seasonal_nirv" or recipe.get("rao", {}).get("normalization") == "gn":
        cap = min(cap, recipe["limits"]["calibration_pixels"])
    require(0 < pixels <= cap, "projected pixel/calibration limit exceeded")
    if workflow == "yearly":
        intervals = [(str(y), f"{y:04d}-01-01", f"{y + 1:04d}-01-01") for y in recipe["years"]]
    else:
        seasonal = recipe.get("seasonal", {})
        intervals = [
            (
                "all",
                min(recipe["start"], seasonal.get("reference_start", recipe["start"])),
                max(recipe["end"], seasonal.get("reference_end", recipe["end"])),
            )
        ]
    step[0] = "scenes"
    sources, collections = {}, {}
    for key, start, end in intervals:
        budget.timeout()
        collections[key], sources[key] = _snapshot(
            recipe, region, start, end, previous["sources"][key] if previous else None
        )
    require(
        sum(map(len, sources.values())) <= recipe["limits"]["scenes"],
        "total scene limit exceeded",
    )
    provenance = dict(
        sources=sources,
        pixels_upper_bound=pixels,
        grid=recipe["grid"],
        dataset=recipe["dataset"],
        workflow=workflow,
        dtype="float64",
        nodata=EXPORT_NODATA,
    )
    if workflow == "seasonal_nirv":
        step[0] = "calibration"
        options = dict(recipe.get("seasonal", {}))
        scene_outputs = options.pop("scene_outputs", True)
        try:
            seasonal = nirv_seasonal_rao(
                region=region,
                grid=grid,
                start=recipe["start"],
                end=recipe["end"],
                collection=collections["all"],
                max_scenes=recipe["limits"]["scenes"],
                max_pixels=recipe["limits"]["calibration_pixels"],
                **options,
            )
        except NoRaoDataError:
            raise RunnerError("no_data", "no usable seasonal observations") from None
        products = [
            ("seasonal_mean", seasonal.mean_image),
            ("observation_count", seasonal.observation_count),
        ]
        if scene_outputs:
            products.extend(
                (f"scene_{i:04d}", image) for i, image in enumerate(seasonal.scene_images)
            )
        provenance.update(
            seasonal=seasonal.provenance,
            scene_summaries=list(seasonal.scene_summaries),
            site_estimate=seasonal.site_estimate,
            threshold=seasonal.threshold,
        )
        # Custom collection object is a snapshot; its identity is recorded above.
        provenance["seasonal"]["collection"] = sentinel2.COLLECTION_ID
    else:
        transform = {
            "identity": None,
            "ndvi": ndvi,
            "nirv": lambda: nirv(reflectance_scale=0.0001),
            "vh_vv_ratio": lambda: expression_transform(
                "vh / vv", {"vh": "VH", "vv": "VV"}, "vh_vv_ratio"
            ),
        }[recipe.get("transform", "identity")]
        kwargs = dict(
            aoi=region,
            select=recipe["bands"],
            reducer=recipe.get("reducer", "median"),
            transform=transform() if transform else None,
            preprocess=gamma_map() if "preprocess" in recipe else None,
        )
        step[0] = "graph"
        images = {}
        for key, start, end in intervals:
            col = collections[key]
            if "mask" in recipe:
                col = sentinel2.apply_mask(col, recipe["mask"])
            if workflow == "yearly":
                images[key] = compose_yearly([int(key)], collection=col, **kwargs)[int(key)]
            else:
                images[key] = compose(collection=col, start=start, end=end, **kwargs)
        if workflow == "rao":
            options = dict(recipe["rao"])
            normalization = options.pop("normalization")
            band_names = recipe["bands"] if not transform else [recipe["transform"]]
            model = None
            if normalization == "gn":
                step[0] = "calibration"
                # Explicit valid-sample check provides a stable no-data result.
                count = (
                    images["all"]
                    .select(band_names)
                    .mask()
                    .reduce(ee.Reducer.min())
                    .selfMask()
                    .reduceRegion(
                        ee.Reducer.count(),
                        **grid.reduction(region, recipe["limits"]["calibration_pixels"]),
                    )
                    .values()
                    .get(0)
                    .getInfo()
                )
                require(
                    type(count) in (int, float) and count >= 2,
                    "insufficient calibration samples",
                    "no_data",
                )
                model = fit_normalization(
                    images["all"],
                    band_names,
                    grid=grid,
                    region=region,
                    max_pixels=recipe["limits"]["calibration_pixels"],
                    decimal_places=options.get("decimal_places"),
                )
                provenance.update(calibration=model.to_json(), calibration_sha256=model.fingerprint)
            step[0] = "graph"
            images["all"] = rao_q(
                images["all"],
                band_names,
                grid=grid,
                region=region,
                model=model,
                **options,
            )
        products = [
            ("year_" + key if workflow == "yearly" else workflow, value)
            for key, value in images.items()
        ]
    step[0] = "graph"
    require(0 < len(products) <= recipe["limits"]["outputs"], "output task limit exceeded")
    require(
        len(canonical(provenance)) <= 700_000,
        "private provenance exceeds byte budget",
        "artifact",
    )
    result = []
    destination = recipe["destination"]
    for name, product in products:
        budget.timeout()
        # Final boundary: explicit -9999 encoding of masked cells, double bands
        # (Rao value/count/coverage). The public helpers stay metadata-only.
        image = export_encoded(product)
        if "drive_folder" in destination:
            # Drive has no object prefix: prefix is a unique run file-name base.
            task = export_to_drive(
                image,
                name,
                destination["drive_folder"],
                region,
                file_name_prefix=f"{prefix}_{name}",
                max_pixels=cap,
                crs=grid.crs,
                crs_transform=grid.transform,
                no_data=EXPORT_NODATA,
            )
        else:
            task = export_to_gcs(
                image,
                name,
                destination["bucket"],
                region,
                file_name_prefix=f"{prefix}/{name}/image",
                max_pixels=cap,
                crs=grid.crs,
                crs_transform=grid.transform,
                no_data=EXPORT_NODATA,
                cloud_optimized=True,
            )
        # Match native Task.start's explicit default and bind it in the fingerprint.
        task.config["workloadTag"] = ""
        result.append(
            dict(
                name=name,
                config=task.config,
                fingerprint=config_fingerprint(task.config),
            )
        )
    return result, provenance
