"""Coarse-first recipes: validation, bounded metadata reads, snapshot/model replay, tasks.

Internal integration of the accepted ``geecomposer.coarse`` science into the recipe
runner. Nothing here changes that science: every graph comes from ``coarse.composite``
and ``coarse.rao`` on the full coarse-cell union.

Service boundary (metadata). A run reads, through the existing EE client and its
bounded, deadline-checked transport, at most four ``getInfo`` responses: the AOI
bounds in the grid's projection, candidate rows (C1), completion rows (C2) and, for
Sentinel-2, Cloud Score+ rows for the C2 members. A reference fit adds three
(sample count and the two inside ``fit_normalization``). None of these multiply by
periods or tile cores, and resume replays the persisted snapshot/model without any
of them. Responses follow the documented ``ImageCollection.getInfo`` schema:
``features[].{id, properties, bands[].{id, crs, crs_transform, dimensions[, origin]}}``.
"""

from __future__ import annotations

import math
import re

import ee

from . import coarse
from .datasets import sentinel2
from .exceptions import GeeComposerError
from .grouping import monthly_periods, validate_periods
from .rao import RaoNormalization, fit_normalization
from .recipe import RunnerError, canonical, choice, fields, integer, number, require
from .runner_science import EXPORT_NODATA, aoi_window, export_encoded, native_task, window_rectangle

SECTION_FIELDS = ("crs", "spatial", "coverage_min", "temporal", "index", "db", "periods", "tiles",
                  "product", "rao", "snapshot")
FORBIDDEN = ("reducer", "transform", "preprocess", "rao", "seasonal", "years")
FINAL_BANDS = 32
TILE_MAX = 12
RAO_WINDOW = 3  # the accepted coarse Rao's fixed 3x3 neighbourhood (coarse.rao)
MAX_DIMENSION = coarse.MAX_DIMENSION
# Fixed refusal codes of the accepted coarse API; their messages are allowlisted.
UNCERTAIN_CODES = ("uncertain", "missing_metadata", "source_projection_mismatch", "snapshot_invalid")
CODES = UNCERTAIN_CODES + (
    "limit", "snapshot_scope", "national_domain", "zone_offset", "source_extent", "source_crs",
    "source_pixel", "source_affine", "source_orientation", "unsupported_transformation", "model",
    "model_bands", "bands", "temporal", "index", "mask", "filters", "db", "dataset", "aoi", "interval",
    "coverage_min", "grid", "grid_crs", "resolution", "snapshot_interval", "fine_grid",
)
MESSAGES = frozenset(
    {f"coarse request refused: {code}" for code in CODES}
    | {
        "coarse calibration was interrupted; no durable model to replay",
        "no acquisitions in any declared period",
        "persisted coarse snapshot changed",
        "persisted coarse model changed",
        "coarse source metadata unusable",
        "native dimensions missing from source metadata",
        "Cloud Score+ metadata missing for a source image",
        "coarse core exceeds the output pixel limit",
        "coarse reference fit exceeds the calibration ceiling",
        "coarse source read limit exceeded",
        "coarse snapshot exceeds the scene ceiling",
        "coarse Rao 3x3 support exceeds the window ceiling",
    }
)


def refused(exc):
    """A CoarseError as a fixed, non-sensitive RunnerError (code names are fixed)."""
    code = getattr(exc, "code", None)
    code = code if code in CODES else "uncertain"
    status = "uncertain" if code in UNCERTAIN_CODES else "validation"
    return RunnerError(status, f"coarse request refused: {code}")


# ------------------------------------------------------------------ recipe contract


def _temporal(value):
    value = ["median"] if value is None else value
    require(type(value) is list and 1 <= len(value) <= len(coarse.TEMPORAL), "invalid temporal statistics")
    for item in value:
        choice(item, coarse.TEMPORAL)
    require(len(set(value)) == len(value), "duplicate temporal statistic")
    return tuple(value)


def _periods(value, start, end):
    if value is None:
        return (("all", start, end),)
    if value == "monthly":
        year = int(start[:4])
        require(start == f"{year:04d}-01-01" and end == f"{year + 1:04d}-01-01",
                "monthly periods need a calendar-year run")
        return monthly_periods(year)
    require(type(value) is list and 1 <= len(value) <= 12, "invalid named periods")
    items = []
    for item in value:
        fields(item, ("label", "start", "end"))
        items.append((item["label"], item["start"], item["end"]))
    try:
        periods = validate_periods(items)
    except GeeComposerError:
        raise RunnerError("validation", "invalid named periods") from None
    require(all(start <= p[1] and p[2] <= end for p in periods), "named periods must lie inside the run")
    return periods


def _cores(value):
    value = {} if value is None else value
    fields(value, (), ("columns", "rows", "cores"))
    columns = integer(value.get("columns", 1), 1, TILE_MAX)
    rows = integer(value.get("rows", 1), 1, TILE_MAX)
    selected = value.get("cores")
    if selected is None:
        return columns, rows, tuple((r, c) for r in range(rows) for c in range(columns))
    require(type(selected) is list and 1 <= len(selected) <= rows * columns, "invalid tile core selection")
    cores = []
    for item in selected:
        require(type(item) is list and len(item) == 2, "invalid tile core selection")
        cores.append((integer(item[0], 0, rows - 1), integer(item[1], 0, columns - 1)))
    require(len(set(cores)) == len(cores), "duplicate tile core")
    return columns, rows, tuple(sorted(cores))


def _name(label, core, single):
    base = "p" + label.lower().replace("-", "_")
    return base if single else f"{base}_r{core[0]:02d}c{core[1]:02d}"


class Plan:
    """Everything a coarse recipe determines locally (no remote access)."""

    def __init__(self, recipe, budget):
        section = recipe["coarse"]
        self.recipe, self.budget, self.section = recipe, budget, section
        self.dataset = recipe["dataset"]
        self.resolution = section["resolution"]
        self.crs = section.get("crs", coarse.MX_AEA)
        try:
            self.grid = coarse.grid(self.resolution, self.crs)
            self.label, _ = coarse.grid_label(self.grid)
        except GeeComposerError as exc:
            raise refused(exc) from None
        self.national = self.label == coarse.MX_AEA
        self.product = section.get("product", "composite")
        self.temporal = _temporal(section.get("temporal"))
        try:
            self.spec = coarse._spec(
                self.dataset, recipe["bands"], recipe.get("mask"), recipe.get("filters"), self.temporal,
                section.get("coverage_min", coarse.DEFAULT_COVERAGE), section.get("index"), section.get("db", False),
            )
        except GeeComposerError as exc:
            raise refused(exc) from None
        self.periods = _periods(section.get("periods"), recipe["start"], recipe["end"])
        self.columns, self.rows, self.cores = _cores(section.get("tiles"))
        single = len(self.cores) == 1 and self.columns * self.rows == 1
        self.names = {(p[0], core): _name(p[0], core, single) for p in self.periods for core in self.cores}
        require(
            len(set(self.names.values())) == len(self.names)
            and all(re.fullmatch(r"[a-z0-9_]{1,60}", n) for n in self.names.values()),
            "period labels give invalid or colliding task names",
        )
        self.model, self.fit = None, None
        if self.product == "rao":
            self._rao(section.get("rao"))
        else:
            require("rao" not in section, "Rao options require the coarse Rao product")
        self.final_bands = (
            ["coarse_rao_gn", "coarse_rao_valid_count", "coarse_rao_coverage", "obs__t_count"]
            + coarse.feature_names(self.spec.bands, "median")
            if self.product == "rao"
            else coarse.output_names(self.spec)
        )

    def _rao(self, value):
        fields(value, (), ("model", "fit"))
        require(len(value) == 1, "coarse Rao needs exactly one of model or fit")
        require(self.temporal == ("median",) and self.spec.index is None, "coarse Rao uses the temporal median only")
        features = coarse.feature_names(self.spec.bands, "median")
        if "model" in value:
            model = value["model"]
            require(type(model) is dict and len(canonical(model)) <= 65_536, "invalid frozen Rao model")
            try:
                self.model = RaoNormalization(**model)
                bases = coarse._model_features(self.model)
            except (TypeError, ValueError):
                raise RunnerError("validation", "invalid frozen Rao model") from None
            except GeeComposerError as exc:
                raise refused(exc) from None
            require(bases == list(self.spec.bands), "bands must equal the frozen model's features, in order")
        else:
            fit = value["fit"]
            fields(fit, ("start", "end"))
            _periods([dict(label="fit", start=fit["start"], end=fit["end"])], self.recipe["start"], self.recipe["end"])
            try:
                coarse._four_features(features)
            except GeeComposerError as exc:
                raise refused(exc) from None
            self.fit = (fit["start"], fit["end"])

    @property
    def declared_tasks(self):
        return len(self.periods) * len(self.cores)

    def chain(self):
        stages = []
        for index, (src, dst) in enumerate(coarse.CHAINS[self.resolution]):
            if index:
                bound, kind = (dst // src + coarse.COUNT_MARGIN) ** 2, "proved_nested"
            elif self.national:
                bound, kind = coarse.NATIONAL_FIRST[dst], "conditional_bound_per_source"
            else:
                bound, kind = (dst // 10 + 1 + coarse.COUNT_MARGIN) ** 2, "conservative_bound_until_metadata"
            stages.append(dict(from_m=src, to_m=dst, max_pixels=bound, kind=kind, best_effort=False))
        return stages


def validate(recipe, budget, admission=True):
    """Strict coarse recipe checks; nothing remote. Returns the local plan.

    ``admission=False`` is used only to inspect or stop a trusted stored record (it
    never prepares or submits). It skips exactly the two execution-admission
    predicates, the Rao window ceiling and the inline snapshot's scene ceiling, so a
    record made before they existed can still be stopped; all else is validated."""
    require(recipe["dataset"] in ("sentinel2", "sentinel1_float"), "coarse products use sentinel2 or sentinel1_float")
    require(not set(recipe) & set(FORBIDDEN), "the coarse section owns reducer, transform and Rao options")
    section = recipe.get("coarse")
    fields(section, ("resolution",), SECTION_FIELDS)
    require(type(section["resolution"]) is int and section["resolution"] in coarse.PRESETS,
            "coarse resolution must be 100, 250, 500, 1000, 5000 or 10000 m")
    require(type(section.get("crs", coarse.MX_AEA)) is str, "unsupported coarse grid CRS")
    choice(section.get("spatial", "mean"), ("mean",))
    coverage = number(section.get("coverage_min", coarse.DEFAULT_COVERAGE), 0, 1)
    require(coverage > 0, "coverage_min must lie in (0, 1]")
    if "index" in section:
        choice(section["index"], ("ndvi",))
    require(type(section.get("db", False)) is bool, "db must be boolean")
    choice(section.get("product", "composite"), ("composite", "rao"))
    plan = Plan(recipe, budget)
    if admission and plan.product == "rao":
        require(budget["window"] >= RAO_WINDOW, "coarse Rao 3x3 support exceeds the window ceiling")
    if "grid" in recipe:
        expected = dict(crs=plan.crs, transform=list(plan.grid.transform))
        require(
            type(recipe["grid"]) is dict
            and recipe["grid"].get("crs") == expected["crs"]
            and recipe["grid"].get("transform") == expected["transform"]
            and set(recipe["grid"]) == {"crs", "transform"},
            "contradictory aggregation and export grids",
        )
    require(len(plan.final_bands) <= FINAL_BANDS, "final band count exceeds the coarse limit")
    require(plan.declared_tasks <= budget["outputs"],
            "declared periods times tile cores exceed the output ceiling")
    if plan.national:
        try:
            region = coarse._national_request(recipe["aoi"], plan.resolution)
        except GeeComposerError as exc:
            raise refused(exc) from None
        if plan.fit:
            cells = _cells(conservative_window(plan, region))
            require(cells <= budget["calibration_pixels"], "coarse reference fit exceeds the calibration ceiling")
    if "snapshot" in section:
        decode_snapshot(section["snapshot"], plan, "validation", admission)
    return plan


# ------------------------------------------------------------------ windows and cores


def conservative_window(plan, region=None):
    """Offline MX-AEA window from the conservative AOI region (dry-run only)."""
    region = region if region is not None else coarse._national_request(plan.recipe["aoi"], plan.resolution)
    x0, y0, x1, y1 = region.bounds
    _, _, c_t, _, _, f_t = plan.grid.transform
    size = plan.resolution
    col0, col1 = math.floor((x0 - c_t) / size), math.ceil((x1 - c_t) / size)
    row0, row1 = math.floor((f_t - y1) / size), math.ceil((f_t - y0) / size)
    return dict(col_off=col0, row_off=row0, width=col1 - col0, height=row1 - row0)


def _cells(window):
    return window["width"] * window["height"]


def partition(window, columns, rows):
    """Aligned cores: integer column/row blocks of the window, keeping global offsets."""
    def splits(offset, size, parts):
        edges = [offset + (size * i) // parts for i in range(parts + 1)]
        return [(edges[i], edges[i + 1] - edges[i]) for i in range(parts)]

    result = {}
    for r, (row_off, height) in enumerate(splits(window["row_off"], window["height"], rows)):
        for c, (col_off, width) in enumerate(splits(window["col_off"], window["width"], columns)):
            result[(r, c)] = dict(col_off=col_off, row_off=row_off, width=width, height=height)
    return result


# ------------------------------------------------------------------ snapshot codec (persisted/inline)


def encode_snapshot(snap):
    tables, extents = dict(snap.projections), dict(snap.extents)
    return dict(
        dataset=snap.dataset, start=snap.start, end=snap.end, scope=snap.scope, bands=list(snap.bands),
        groups=[[list(k), t, list(ids)] for k, t, ids in snap.groups],
        excluded=[list(k) for k in snap.excluded],
        members={i: [t[0][1], list(t[0][2]), list(extents[i]) if i in extents else None] for i, t in tables.items()},
        fingerprint=snap.fingerprint,
    )


def decode_snapshot(value, plan, status, admission=True):
    """Rebuild and revalidate a compact snapshot; it must belong to this exact request."""
    message = "persisted coarse snapshot changed" if status == "conflict" else "coarse request refused: snapshot_invalid"
    try:
        fields(value, ("dataset", "start", "end", "scope", "bands", "groups", "excluded", "members", "fingerprint"))
        members, bands = value["members"], value["bands"]
        require(type(members) is dict and len(members) <= coarse.READ_LIMIT, message)
        require(type(bands) is list and 1 <= len(bands) <= 8 and all(type(b) is str for b in bands), message)
        for item in members.values():
            require(type(item) is list and len(item) == 3, message)
        projections = [(i, [(b, item[0], item[1]) for b in bands]) for i, item in members.items()]
        extents = [(i, item[2]) for i, item in members.items() if item[2] is not None]
        snap = coarse.Snapshot(value["dataset"], value["start"], value["end"], value["scope"], value["groups"],
                               value["excluded"], projections, extents)
    except (RunnerError, GeeComposerError, TypeError, ValueError, AttributeError):
        raise RunnerError(status, message) from None
    expected = coarse.snapshot_scope(plan.spec, plan.recipe["aoi"], plan.grid)
    require(
        snap.fingerprint == value["fingerprint"]
        and snap.scope == expected
        and (snap.dataset, snap.start, snap.end) == (plan.dataset, plan.recipe["start"], plan.recipe["end"]),
        message,
        status,
    )
    # Every supplied or persisted snapshot fits the effective scenes ceiling, counted on
    # the complete validated snapshot (one projection entry per member image); an
    # oversized one is refused whole, never truncated.
    if admission:
        require(len(snap.projections) <= min(plan.budget["scenes"], coarse.READ_LIMIT),
                "coarse snapshot exceeds the scene ceiling", status)
    return snap


# ------------------------------------------------------------------ metadata service boundary


def _band(band):
    """One band's native grid from getInfo: crs, crs_transform, dimensions and an
    optional integer pixel origin folded into the translation."""
    unusable = "coarse source metadata unusable"
    require(type(band) is dict and type(band.get("crs")) is str, unusable, "uncertain")
    transform = band.get("crs_transform")
    require(
        type(transform) is list and len(transform) == 6
        and all(type(v) in (int, float) and not isinstance(v, bool) and math.isfinite(v) for v in transform),
        unusable,
        "uncertain",
    )
    dims = band.get("dimensions")
    require(dims is not None, "native dimensions missing from source metadata", "uncertain")
    require(
        type(dims) is list and len(dims) == 2
        and all(type(v) is int and 1 <= v <= MAX_DIMENSION for v in dims),
        unusable,
        "uncertain",
    )
    origin = band.get("origin")
    if origin is not None:
        require(type(origin) is list and len(origin) == 2 and all(type(v) is int and v >= 0 for v in origin),
                unusable, "uncertain")
        a, b, c, d, e, f = transform
        require(b == 0 and d == 0, unusable, "uncertain")
        transform = [a, b, c + a * origin[0], d, e, f + e * origin[1]]
    return dict(crs=band["crs"], transform=list(transform), dimensions=list(dims))


KEY_FIELDS = {"sentinel2": ("SPACECRAFT_NAME", "DATATAKE_IDENTIFIER"),
              "sentinel1_float": ("platform_number", "missionDataTakeID")}


def decode_rows(info, wanted, dataset, limit):
    """Rows for ``coarse.build_snapshot`` from one ``ImageCollection.getInfo`` response."""
    unusable = "coarse source metadata unusable"
    require(type(info) is dict and info.get("type") == "ImageCollection", unusable, "uncertain")
    features = info.get("features")
    require(type(features) is list, unusable, "uncertain")
    require(len(features) <= limit, "coarse source read limit exceeded", "validation")
    rows = []
    for item in features:
        require(type(item) is dict and item.get("type") == "Image", unusable, "uncertain")
        props, bands = item.get("properties"), item.get("bands")
        require(type(props) is dict and type(bands) is list and len(bands) <= 32, unusable, "uncertain")
        image_id = props.get("system:index")
        require(
            type(image_id) is str and type(item.get("id")) is str and item["id"].endswith("/" + image_id),
            unusable,
            "uncertain",
        )
        row = {"id": image_id, "time_ms": props.get("system:time_start")}
        for key in KEY_FIELDS.get(dataset, ()):
            if key in props:
                row[key] = props[key]
        projections = {}
        for band in bands:
            require(type(band) is dict, unusable, "uncertain")
            if band.get("id") in wanted:
                require(band["id"] not in projections, unusable, "uncertain")
                projections[band["id"]] = _band(band)
        row["projections"] = projections
        rows.append(row)
    return rows


def _info(budget, collection):
    budget.timeout()
    return collection.getInfo()


def read_snapshot(plan, budget, limit):
    """C1 over the run interval and cell union, C2 by acquisition key without dates,
    Cloud Score+ for the members (S2); then the accepted snapshot validation."""
    recipe, spec = plan.recipe, plan.spec
    base = dict(dataset=plan.dataset, aoi=recipe["aoi"], grid=plan.grid, select=list(spec.bands),
                mask=recipe.get("mask"), filters=recipe.get("filters"))
    try:
        c1 = coarse.candidate_query(start=recipe["start"], end=recipe["end"], limit=limit, **base)
        rows1 = decode_rows(_info(budget, c1.select(list(spec.bands))), spec.bands, plan.dataset, limit)
        rows2 = []
        if rows1:
            keys = sorted({coarse._row_key(plan.dataset, r) for r in rows1})
            c2 = coarse.completion_query(keys=keys, limit=limit, **base)
            rows2 = decode_rows(_info(budget, c2.select(list(spec.bands))), spec.bands, plan.dataset, limit)
        if plan.dataset == "sentinel2" and rows2:
            ids = sorted({r["id"] for r in rows2})
            cs = (ee.ImageCollection(sentinel2._CS_PLUS_COLLECTION)
                  .filter(ee.Filter.inList("system:index", ids))
                  .select([coarse.S2_QUALITY_BAND]).limit(limit + 1))
            scores = decode_rows(_info(budget, cs), (coarse.S2_QUALITY_BAND,), "cloud_score_plus", limit)
            table = {}
            for row in scores:
                require(row["id"] not in table and coarse.S2_QUALITY_BAND in row["projections"],
                        "Cloud Score+ metadata missing for a source image", "uncertain")
                table[row["id"]] = row["projections"][coarse.S2_QUALITY_BAND]
            for row in rows1 + rows2:
                require(row["id"] in table, "Cloud Score+ metadata missing for a source image", "uncertain")
                row["projections"][coarse.S2_QUALITY_BAND] = dict(table[row["id"]])
        return coarse.build_snapshot(start=recipe["start"], end=recipe["end"], candidate_rows=rows1,
                                     completion_rows=rows2, limit=limit, **base)
    except GeeComposerError as exc:
        raise refused(exc) from None


# ------------------------------------------------------------------ preparation


def prepare(recipe, prefix, budget, previous, step):
    """Snapshot, (optional) calibration, one graph per populated period, one native
    unstarted task per period and selected core. Science stays on the full cell union;
    each task exports one aligned core with the existing -9999/double encoding."""
    plan = validate(recipe, recipe["limits"])
    limits = recipe["limits"]
    stored = previous.get("coarse") if previous else None
    step[0] = "bounds"
    region = ee.Geometry(recipe["aoi"], proj="EPSG:4326", geodesic=False)
    if stored is not None:
        window = previous.get("export_window")
        require(type(window) is dict and set(window) == {"col_off", "row_off", "width", "height"},
                "persisted coarse snapshot changed", "conflict")
    else:
        window, _ = aoi_window(region, plan.grid)
    cores = partition(window, plan.columns, plan.rows)
    selected = {core: cores[core] for core in plan.cores}
    require(all(0 < _cells(w) <= limits["pixels"] for w in selected.values()),
            "coarse core exceeds the output pixel limit")
    step[0] = "scenes"
    if stored is not None:
        snap = decode_snapshot(stored.get("snapshot"), plan, "conflict")
    elif "snapshot" in plan.section:
        snap = decode_snapshot(plan.section["snapshot"], plan, "validation")
    else:
        snap = read_snapshot(plan, budget, min(limits["scenes"], coarse.READ_LIMIT))
    common = dict(dataset=plan.dataset, aoi=recipe["aoi"], select=list(plan.spec.bands), grid=plan.grid,
                  snapshot=snap, mask=recipe.get("mask"), filters=recipe.get("filters"))
    model = plan.model
    try:
        if plan.fit:
            step[0] = "calibration"
            if stored is not None:
                text = stored.get("model")
                try:
                    model = RaoNormalization.from_json(text)
                except (TypeError, ValueError, AttributeError):
                    raise RunnerError("conflict", "persisted coarse model changed") from None
                require(model.fingerprint == stored.get("calibration_sha256"), "persisted coarse model changed",
                        "conflict")
            else:
                require(_cells(window) <= limits["calibration_pixels"],
                        "coarse reference fit exceeds the calibration ceiling")
                reference = coarse.composite(start=plan.fit[0], end=plan.fit[1], temporal=("median",),
                                             coverage_min=plan.spec.coverage_min, **common)
                features = coarse.feature_names(plan.spec.bands, "median")
                cells = coarse.cell_region(recipe["aoi"], plan.grid)
                budget.timeout()
                count = (reference.select(features).mask().reduce(ee.Reducer.min()).selfMask()
                         .reduceRegion(ee.Reducer.count(), **plan.grid.reduction(cells, limits["calibration_pixels"]))
                         .values().get(0).getInfo())
                require(type(count) in (int, float) and count >= 2, "insufficient calibration samples", "no_data")
                budget.timeout()
                model = fit_normalization(reference, features, grid=plan.grid, region=cells,
                                          max_pixels=limits["calibration_pixels"])
                coarse._model_features(model)
        step[0] = "graph"
        outcomes, tasks = [], []
        for label, start, end in plan.periods:
            budget.timeout()
            groups = snap.groups_in(start, end)
            outcomes.append(dict(label=label, start=start, end=end, acquisitions=len(groups),
                                 outcome="populated" if groups else "empty"))
            if not groups:
                continue
            image = coarse.composite(start=start, end=end, temporal=plan.temporal, coverage_min=plan.spec.coverage_min,
                                     index=plan.spec.index, db=plan.spec.db, **common)
            if plan.product == "rao":
                image = coarse.rao(image, model=model, grid=plan.grid, aoi=recipe["aoi"])
            for core in plan.cores:
                tasks.append((plan.names[(label, core)], image, selected[core]))
    except GeeComposerError as exc:
        raise refused(exc) from None
    require(tasks, "no acquisitions in any declared period", "no_data")
    require(len(tasks) <= limits["outputs"], "output task limit exceeded")
    provenance = dict(
        workflow="coarse",
        dataset=plan.dataset,
        grid=dict(crs=plan.label, wkt=plan.grid.crs, transform=list(plan.grid.transform)),
        export_window=window,
        pixels_upper_bound=max(_cells(w) for w in selected.values()),
        dtype="float64",
        nodata=EXPORT_NODATA,
        coarse=dict(
            snapshot=encode_snapshot(snap),
            periods=outcomes,
            cores=[dict(row=r, column=c, **selected[(r, c)]) for r, c in plan.cores],
            product=plan.product,
            final_bands=plan.final_bands,
            model=model.to_json() if model else None,
            calibration_sha256=model.fingerprint if model else None,
            reference=list(plan.fit) if plan.fit else None,
        ),
    )
    require(len(canonical(provenance)) <= 700_000, "private provenance exceeds byte budget", "artifact")
    result = []
    for name, image, core in tasks:
        budget.timeout()
        # No raw-AOI clip: membership is the full coarse-cell union; the core rectangle
        # bounds the sentinel fill exactly as the existing export window does.
        result.append(native_task(name, export_encoded(image), window_rectangle(plan.grid, core), plan.grid,
                                  limits["pixels"], prefix, recipe["destination"]))
    return result, provenance


# ------------------------------------------------------------------ dry-run


def estimate(recipe):
    """Credential-free report: known plan facts, conservative bounds and unknowns."""
    budget = recipe["limits"]
    plan = validate(recipe, budget)
    window = conservative_window(plan) if plan.national else None
    cores = partition(window, plan.columns, plan.rows) if window else None
    core_pixels = max(_cells(cores[c]) for c in plan.cores) if cores else None
    fit_calls = 3 if plan.fit else 0
    inline = "snapshot" in plan.section
    calls = 1 + (0 if inline else (3 if plan.dataset == "sentinel2" else 2)) + fit_calls
    units = ("reflectance (unitless, 0.0001 scale applied)" if plan.dataset == "sentinel2"
             else "linear backscatter power (dB bands where requested)")
    prerequisites = ["completed C1/C2 metadata reads with native band CRS, affine and dimensions"
                     if not inline else "inline completed snapshot (validated, fingerprinted)"]
    if plan.product == "rao":
        prerequisites.append("supplied frozen four-feature model" if plan.model
                             else "one bounded reference median fit (calibration-ceiling checked)")
    return dict(
        status="dry_run",
        workflow="coarse",
        warnings=[],
        tasks=None,
        task_upper_bound=plan.declared_tasks,
        task_limit=budget["outputs"],
        active_exports=budget["active_exports"],
        periods=[p[0] for p in plan.periods],
        task_names=[plan.names[(p[0], c)] for p in plan.periods for c in plan.cores],
        cores=[list(c) for c in plan.cores],
        order="spatial_mean_then_temporal",
        grid=dict(crs=plan.label, resolution_m=plan.resolution, transform=list(plan.grid.transform)),
        stages=plan.chain(),
        final_bands=plan.final_bands,
        units=units if plan.product == "composite" else "Rao GN value, count and coverage plus reflectance features",
        coverage_min=plan.spec.coverage_min,
        rao_support_m=RAO_WINDOW * plan.resolution if plan.product == "rao" else None,
        prerequisites=prerequisites,
        metadata_calls_upper_bound=calls,
        known=["periods", "task_names", "task_upper_bound", "cores", "grid", "stages", "final_bands", "order"],
        conservative_bound=dict(
            export_window=window,
            core_pixel_upper_bound=core_pixels,
            note="MX-AEA window from the conservative AOI region; the run uses the projected AOI bounds",
        ),
        unknown=["scene_count", "acquisitions_per_period", "empty_periods", "eecu_seconds", "runtime"],
        scene_count=None,
        scene_limit=budget["scenes"],
        pixel_upper_bound=core_pixels,
        pixel_limit=budget["pixels"],
        calibration_pixel_limit=budget["calibration_pixels"],
        output_pixels_are_not_compute_cost=True,
        deadline_seconds=budget["deadline_seconds"],
        online_preflight_required=True,
        limits=budget,
        destination=recipe["destination"],
        run_prefix="allocated on reservation",
    )
