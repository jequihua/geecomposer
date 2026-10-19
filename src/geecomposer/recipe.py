"""Strict, credential-free recipe and administrator-policy validation.

The recipe surface intentionally contains named operations, never Python,
expressions, asset URLs, vector paths or credential/project settings.
Destinations are GCS {bucket, prefix} or, outside institutional policy, a
personal Drive {drive_folder} name.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from shapely.geometry import shape

CEILINGS = dict(
    recipe_bytes=1_048_576,
    vertices=10_000,
    bands=12,
    window=7,
    outputs=12,
    pixels=10_000_000,
    scenes=1000,
    calibration_pixels=1_000_000,
    active_exports=2,
    deadline_seconds=1800,
    status_retries=3,
)
EXIT_CODES = dict(
    succeeded=0,
    validated=0,
    dry_run=0,
    running=10,
    cancelling=10,
    validation=2,
    auth=3,
    no_data=4,
    conflict=5,
    uncertain=6,
    deadline=7,
    cancelled=8,
    export=9,
    artifact=11,
)


class RunnerError(Exception):
    """Only fixed, non-sensitive messages cross the CLI error boundary."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.code = EXIT_CODES[status]


def require(condition, message, status="validation"):
    if not condition:
        raise RunnerError(status, message)


def fields(value, required, optional=()):
    require(type(value) is dict, "expected a JSON object")
    require(set(required) <= value.keys(), "required field missing")
    require(value.keys() <= set(required) | set(optional), "unknown field")


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, "integer outside permitted range")
    return value


def number(value, low=-math.inf, high=math.inf):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    require(valid, "finite numeric value outside permitted range")
    return value


def choice(value, values):
    require(type(value) is str and value in values, "unsupported named option")
    return value


def canonical(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RunnerError("validation", "invalid JSON value") from None


def digest(value):
    return sha256(canonical(value)).hexdigest()


def parse_json(data, limit=CEILINGS["recipe_bytes"]):
    require(isinstance(data, bytes) and len(data) <= limit, "JSON byte limit exceeded")

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise RunnerError("validation", "non-finite JSON number")

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        # Also catches float overflow (1e999), surrogates and deep nesting.
        canonical(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise RunnerError("validation", "malformed JSON") from None


def read_json(path, limit=CEILINGS["recipe_bytes"]):
    try:
        require(Path(path).is_file(), "local input must be a regular JSON file")
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise RunnerError("validation", "cannot read bounded JSON file") from None
    return parse_json(data, limit)


def location(value):
    fields(value, ("bucket", "prefix"))
    bucket, prefix = value["bucket"], value["prefix"]
    require(
        type(bucket) is str
        and re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", bucket)
        and not bucket.startswith("goog")
        and "google" not in bucket,
        "invalid bare bucket name",
    )
    require(
        type(prefix) is str
        and len(prefix) <= 240
        and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix),
        "invalid relative prefix",
    )
    return value


def drive_folder(value):
    """Personal Drive destination: a folder *name* (EE resolves names, not IDs)."""
    fields(value, ("drive_folder",))
    require(
        type(value["drive_folder"]) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value["drive_folder"]),
        "invalid Drive folder name",
    )
    return value


def destination(value):
    """Either GCS {bucket, prefix} or Drive {drive_folder}; never reinterpreted."""
    require(type(value) is dict, "expected a JSON object")
    return (drive_folder if "drive_folder" in value else location)(value)


def allowed(value, locations):
    return any(
        value["bucket"] == entry["bucket"]
        and (
            value["prefix"] == entry["prefix"] or value["prefix"].startswith(entry["prefix"] + "/")
        )
        for entry in locations
    )


def limits(value, ceilings):
    fields(value, (), ceilings)
    result = dict(ceilings)
    for key, ceiling in ceilings.items():
        if key in value:
            result[key] = integer(value[key], 0 if key == "status_retries" else 1, ceiling)
    require(result["window"] in (1, 3, 5, 7), "window ceiling must be odd")
    require(result["deadline_seconds"] >= 120, "deadline must allow bounded finalization")
    return result


def validate_policy(value):
    fields(
        value,
        (
            "version",
            "project",
            "service_account",
            "outputs",
            "inputs",
            "control",
            "limits",
            "poll_seconds",
            "image_version",
            "mode",
        ),
    )
    integer(value["version"], 1, 1)
    choice(value["mode"], ("hosted", "local"))
    require(
        type(value["project"]) is str
        and re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", value["project"]),
        "invalid policy project",
    )
    require(
        type(value["service_account"]) is str
        and re.fullmatch(
            r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com",
            value["service_account"],
        ),
        "policy requires an attached service account",
    )
    for key in ("outputs", "inputs"):
        require(
            type(value[key]) is list and 1 <= len(value[key]) <= 20,
            "policy allowlist required",
        )
        for item in value[key]:
            location(item)
    location(value["control"])
    # Keep replaceable state separate from write-once raster destinations.
    require(
        all(item["bucket"] != value["control"]["bucket"] for item in value["outputs"]),
        "control and raster buckets must be separate",
    )
    result = dict(value, limits=limits(value["limits"], CEILINGS))
    integer(value["poll_seconds"], 15, 60)
    require(
        type(value["image_version"]) is str
        and re.fullmatch(r"[A-Za-z0-9:._-]{1,160}", value["image_version"]),
        "invalid image version",
    )
    return result


def interval(start, end):
    for item in (start, end):
        require(
            type(item) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item),
            "invalid ISO date",
        )
        try:
            date.fromisoformat(item)
        except ValueError:
            raise RunnerError("validation", "invalid ISO date") from None
    require(start < end, "date interval must be nonempty and half-open")


def validate_aoi(value, maximum):
    fields(value, ("type", "coordinates"))
    choice(value["type"], ("Polygon", "MultiPolygon"))
    coordinates = value["coordinates"]
    require(type(coordinates) is list and coordinates, "empty AOI")
    polygons = [coordinates] if value["type"] == "Polygon" else coordinates
    count = 0
    for polygon in polygons:
        require(type(polygon) is list and polygon, "invalid polygon")
        for ring in polygon:
            require(type(ring) is list and len(ring) >= 4, "invalid closed ring")
            count += len(ring)
            require(count <= maximum, "AOI vertex limit exceeded")
            for point in ring:
                require(type(point) is list and len(point) == 2, "AOI requires 2D positions")
                number(point[0], -180, 180)
                number(point[1], -85, 85)
            require(ring[0] == ring[-1], "AOI rings must close")
            require(
                all(abs(a[0] - b[0]) < 180 for a, b in zip(ring, ring[1:])),
                "antimeridian-crossing AOIs are unsupported",
            )
    geometry = shape(value)
    require(
        geometry.is_valid and not geometry.is_empty and geometry.area > 0,
        "invalid AOI topology",
    )
    return geometry.bounds


def validate_grid(value):
    fields(value, ("crs", "transform"))
    crs = value["crs"]
    require(
        type(crs) is str
        and (
            crs in ("EPSG:4326", "EPSG:3857")
            or re.fullmatch(r"EPSG:32[67](?:0[1-9]|[1-5][0-9]|60)", crs)
        ),
        "unsupported grid CRS",
    )
    transform = value["transform"]
    require(
        type(transform) is list and len(transform) == 6,
        "grid needs six affine coefficients",
    )
    for v in transform:
        number(v, -1e9, 1e9)
    a, b, _, d, e, _ = transform
    require(abs(a * e - b * d) >= 1e-16, "singular or unstable grid")


def pixel_bound(bounds, transform):
    """Conservative grid-cell bounding rectangle, including boundary cells."""
    a, b, c, d, e, f = transform
    det = a * e - b * d
    points = [
        ((e * (x - c) - b * (y - f)) / det, (-d * (x - c) + a * (y - f)) / det)
        for x in (bounds[0], bounds[2])
        for y in (bounds[1], bounds[3])
    ]
    widths = [
        math.ceil(max(p[i] for p in points)) - math.floor(min(p[i] for p in points)) + 4
        for i in (0, 1)
    ]
    return widths[0] * widths[1]


def validate_recipe(value, policy=None, *, admission=True):
    fields(
        value,
        ("version", "workflow", "dataset", "aoi", "destination", "bands"),
        (
            "grid",
            "start",
            "end",
            "years",
            "reducer",
            "mask",
            "preprocess",
            "transform",
            "filters",
            "rao",
            "seasonal",
            "coarse",
            "limits",
        ),
    )
    integer(value["version"], 1, 1)
    workflow = choice(value["workflow"], ("composite", "yearly", "rao", "seasonal_nirv", "coarse"))
    # The coarse workflow resolves its grid from its own section; every other
    # workflow keeps the required explicit export grid.
    require(workflow == "coarse" or "grid" in value, "required field missing")
    require((workflow == "coarse") == ("coarse" in value), "coarse options require the coarse workflow")
    dataset = choice(value["dataset"], ("sentinel2", "sentinel1", "sentinel1_float"))
    budget = limits(value.get("limits", {}), policy["limits"] if policy else CEILINGS)
    require(
        len(canonical(value)) <= budget["recipe_bytes"],
        "canonical recipe byte limit exceeded",
    )
    bounds = validate_aoi(value["aoi"], budget["vertices"])
    if workflow != "coarse":
        validate_grid(value["grid"])
    # Institutional policy admits only allowlisted GCS locations.
    (location if policy else destination)(value["destination"])
    if policy:
        require(
            allowed(value["destination"], policy["outputs"]),
            "destination denied by policy",
            "auth",
        )
    bands = value["bands"]
    options = (
        ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12")
        if dataset == "sentinel2"
        else ("VV", "VH", "HH", "HV", "angle")
    )
    require(
        type(bands) is list and 1 <= len(bands) <= budget["bands"],
        "band count exceeds budget",
    )
    for band in bands:
        choice(band, options)
    require(len(set(bands)) == len(bands), "duplicate band")
    if workflow == "yearly":
        require(
            "start" not in value and "end" not in value,
            "yearly dates must come from years",
        )
        years = value.get("years")
        require(
            type(years) is list and 1 <= len(years) <= budget["outputs"],
            "year/output limit exceeded",
        )
        for year in years:
            integer(year, 1, 9998)
        require(len(set(years)) == len(years), "duplicate year")
    else:
        require("years" not in value, "years only apply to yearly workflow")
        interval(value.get("start"), value.get("end"))
    choice(
        value.get("reducer", "median"),
        ("median", "mean", "min", "max", "mosaic", "count"),
    )
    if "mask" in value:
        require(dataset == "sentinel2", "mask requires Sentinel-2")
        choice(value["mask"], ("s2_cloud_score_plus",))
    if "preprocess" in value:
        require(dataset == "sentinel1_float", "Gamma MAP requires linear Sentinel-1")
        choice(value["preprocess"], ("gamma_map",))
        require(budget["window"] >= 7, "default Gamma MAP window exceeds policy")
    transform = value.get("transform", "identity")
    choice(transform, ("identity", "ndvi", "nirv", "vh_vv_ratio"))
    if transform in ("ndvi", "nirv"):
        require(
            dataset == "sentinel2" and {"B4", "B8"} <= set(bands),
            "index needs Sentinel-2 red/NIR bands",
        )
    if transform == "vh_vv_ratio":
        require(
            dataset == "sentinel1_float" and {"VV", "VH"} <= set(bands),
            "ratio needs linear VV/VH",
        )
    filters = value.get("filters", {})
    fields(
        filters,
        (),
        (("instrumentMode", "orbitPass", "polarizations") if dataset != "sentinel2" else ()),
    )
    if "instrumentMode" in filters:
        choice(filters["instrumentMode"], ("IW", "EW", "SM"))
    if "orbitPass" in filters:
        choice(filters["orbitPass"], ("ASCENDING", "DESCENDING"))
    if "polarizations" in filters:
        pol = filters["polarizations"]
        require(type(pol) is list and 1 <= len(pol) <= 4, "invalid polarizations")
        for p in pol:
            choice(p, ("VV", "VH", "HH", "HV"))
        require(len(set(pol)) == len(pol), "duplicate polarization")
    if workflow == "rao":
        rao = value.get("rao")
        fields(rao, ("normalization",), ("window_size", "decimal_places"))
        choice(rao["normalization"], ("raw", "gn"))
    else:
        require("rao" not in value, "Rao options require Rao workflow")
    if workflow == "seasonal_nirv":
        require(
            dataset == "sentinel2" and bands == ["B4", "B8"],
            "seasonal workflow needs B4/B8",
        )
        require(
            not set(value) & {"transform", "preprocess", "mask", "filters", "reducer"},
            "seasonal workflow owns preprocessing and reduction",
        )
        seasonal = value.get("seasonal", {})
        fields(
            seasonal,
            (),
            (
                "reference_start",
                "reference_end",
                "window_size",
                "decimal_places",
                "scene_outputs",
            ),
        )
        interval(
            seasonal.get("reference_start", value["start"]),
            seasonal.get("reference_end", value["end"]),
        )
        require(
            type(seasonal.get("scene_outputs", True)) is bool,
            "scene_outputs must be boolean",
        )
        require(budget["outputs"] >= 2, "seasonal workflow requires at least two outputs")
    else:
        require("seasonal" not in value, "seasonal options require seasonal workflow")
    science = value.get("rao", value.get("seasonal", {}))
    window = science.get("window_size", 3)
    if workflow in ("rao", "seasonal_nirv"):
        integer(window, 1, budget["window"])
        require(window % 2 == 1, "window must be odd")
    if science.get("decimal_places") is not None:
        integer(science["decimal_places"], 0, 9)
    if workflow == "coarse":
        from .runner_coarse import validate as coarse_validate

        # admission=False only inspects or stops a trusted stored record; it skips
        # exactly the coarse execution-admission predicates (runner_coarse.validate).
        coarse_validate(value, budget, admission)
        return dict(value, limits=budget)
    pixels = (
        pixel_bound(bounds, value["grid"]["transform"])
        if value["grid"]["crs"] == "EPSG:4326"
        else None
    )
    cap = budget["pixels"]
    if workflow == "seasonal_nirv" or science.get("normalization") == "gn":
        cap = min(cap, budget["calibration_pixels"])
    require(pixels is None or pixels <= cap, "pixel/calibration budget exceeded")
    return dict(value, limits=budget)


def estimate(recipe):
    if recipe["workflow"] == "coarse":
        from .runner_coarse import estimate as coarse_estimate

        return coarse_estimate(recipe)
    budget = recipe["limits"]
    bounds = shape(recipe["aoi"]).bounds
    pixels = (
        pixel_bound(bounds, recipe["grid"]["transform"])
        if recipe["grid"]["crs"] == "EPSG:4326"
        else None
    )
    window = recipe.get("rao", recipe.get("seasonal", {})).get("window_size", 3)
    pairs = (
        window**2 * (window**2 - 1) // 2 if recipe["workflow"] in ("rao", "seasonal_nirv") else 0
    )
    tasks = len(recipe["years"]) if recipe["workflow"] == "yearly" else 1
    warnings = []
    if recipe["workflow"] == "seasonal_nirv":
        tasks = None if recipe.get("seasonal", {}).get("scene_outputs", True) else 2
        if tasks is None:
            warnings.append(
                dict(
                    code="seasonal_scene_output_limit",
                    fixed_outputs=2,
                    allowed_scene_outputs=budget["outputs"] - 2,
                    message="Selected scene exports must fit the effective output ceiling minus two fixed products. "
                    "Seasonal evaluation may be expensive before the selected count is known; "
                    "scene_outputs=false avoids scene exports. No scenes are silently clipped.",
                )
            )
    return dict(
        status="dry_run",
        warnings=warnings,
        tasks=tasks,
        task_limit=budget["outputs"],
        pixel_upper_bound=pixels,
        pixel_limit=budget["pixels"],
        scene_count=None,
        scene_limit=budget["scenes"],
        pairs_per_pixel=pairs,
        band_scene_pixel_ceiling=len(recipe["bands"])
        * budget["scenes"]
        * (pixels or budget["pixels"]),
        output_pixel_ceiling=(tasks or budget["outputs"]) * (pixels or budget["pixels"]),
        gamma_window_cells=49 if "preprocess" in recipe else 0,
        pair_band_scene_pixel_ceiling=pairs
        * len(recipe["bands"])
        * budget["scenes"]
        * (pixels or budget["pixels"]),
        online_preflight_required=True,
        limits=budget,
        destination=recipe["destination"],
        run_prefix="allocated on reservation",
    )
