"""Six-resolution coarse-first composites, named periods and coarse GN Rao.

Implements the M005-S01 contract adopted by D126 (A1-A19). Every product is a
lazy Earth Engine graph: no helper evaluates, fetches metadata, authenticates or
fits a model. Source identity and projections come only from a validated
``Snapshot`` built from completed candidate (C1) and completion (C2) reads that
the caller supplies; executing and persisting those reads belongs to S03. Every
``Snapshot`` (factory-built, constructed directly or ``dataclasses.replace``-d) is
validated and canonicalized into immutable contents when it is created.

National MX-AEA reductions are admitted under D129/D130 (the reviewed G1 bound):
first stages use ``maxPixels`` 225 (10 -> 100 m) or 81 (10 -> 50 m) only for
EPSG:32611-32616 10 m north-up sources, and only where every point of every
evaluated first-stage cell lies in the G1 domain and within 5 degrees of the
source zone's central meridian. Each source's reduction is confined to its
admitted coarse-aligned rectangle (``_coarse_geometry``); a source that cannot
be admitted fails the request with a reason, never silently. Same-zone UTM grids
keep their exact interval-count bounds. There is no override.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timezone
from fractions import Fraction
from hashlib import sha256
from itertools import islice
import json
import math
import re
import sys

import ee
from shapely.geometry import shape

from . import _coarse_geometry as geo
from .datasets import _sentinel1_filters, sentinel1_float, sentinel2
from .exceptions import GeeComposerError
from .grouping import validate_periods
from .rao import RaoGrid, RaoNormalization, rao_q

PRESETS = (100, 250, 500, 1000, 5000, 10000)
MX_AEA = "MX-AEA"
MX_AEA_WKT = (
    'PROJCS["MX-AEA",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Albers_Conic_Equal_Area"],'
    'PARAMETER["latitude_of_center",12],PARAMETER["longitude_of_center",-102],'
    'PARAMETER["standard_parallel_1",17.5],PARAMETER["standard_parallel_2",29.5],'
    'PARAMETER["false_easting",2500000],PARAMETER["false_northing",0],UNIT["metre",1]]'
)
NO_PROOF = "unsupported_transformation"
#: Stage chains (from metres, to metres); later stages are nested in the target CRS.
CHAINS = {
    100: ((10, 100),),
    250: ((10, 50), (50, 250)),
    500: ((10, 100), (100, 500)),
    1000: ((10, 100), (100, 1000)),
    5000: ((10, 100), (100, 1000), (1000, 5000)),
    10000: ((10, 100), (100, 1000), (1000, 10000)),
}
NATIVE_M = 10
COUNT_MARGIN = 2
ORIGIN_MULTIPLE = 10000
DEFAULT_COVERAGE = 0.5
TEMPORAL = ("mean", "median", "sd_pop", "min", "max", "range")
DB_STATS = ("mean", "median", "min", "max")
S2_BANDS = ("B2", "B3", "B4", "B8")
S2_SCALE = 0.0001
S2_QUALITY_BAND = sentinel2._CS_PLUS_BAND
S1_BANDS = ("VV", "VH")
NATIONAL_FIRST = {100: 225, 50: 81}  # D129: native 10 m -> MX-AEA first-stage maxPixels
MAX_DIMENSION = 200000
READ_LIMIT = 1000
RUN_GROUP_LIMIT = 600
PERIOD_GROUP_LIMIT = 400
MAX_DOUBLE = sys.float_info.max
COVERAGE_TOLERANCE = 1e-12  # D179: fixed absolute final coverage-fraction tolerance; not configurable
_ID = re.compile(r"[A-Za-z0-9_-]{1,240}")
_UTM = re.compile(r"EPSG:326(\d\d)")
_DATATAKE = re.compile(r"(GS2[A-D]_\d{8}T\d{6}_\d{6})_N\d{2}\.\d{2}")
_ACQUISITION = re.compile(r"GS2[A-D]_\d{8}T\d{6}_\d{6}")
_S2_PLATFORMS = ("Sentinel-2A", "Sentinel-2B", "Sentinel-2C", "Sentinel-2D")
_FEATURE = re.compile(r"(B2|B3|B4|B8)__s_mean__t_median")
_DATATAKE_SUFFIX = r"_N[0-9]{2}\.[0-9]{2}$"
REFERENCE_CONFOUNDS = (
    "fine-first selects the lowest-ID valid granule per fine pixel while coarse-first selects the "
    "maximum-coverage granule per coarse cell; S1 is resampled to the fine grid by nearest neighbour; "
    "the operation order differs. The difference is a measured product difference, not temporal "
    "ordering in isolation."
)


class CoarseError(GeeComposerError):
    """A coarse request refused before any Earth Engine evaluation; ``code`` is stable."""

    def __init__(self, code, detail=None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ------------------------------------------------------------------ grids and admission


def _preset(value):
    if type(value) is not int or value not in PRESETS:
        raise CoarseError("resolution", f"resolution must be one of {PRESETS} metres")
    return value


def grid(resolution_m, crs=MX_AEA):
    """Preset grid on the fixed 10 km origin: MX-AEA (national) or one northern UTM zone."""
    _preset(resolution_m)
    if crs == MX_AEA:
        wkt = MX_AEA_WKT
    elif isinstance(crs, str) and _UTM.fullmatch(crs) and 1 <= int(crs[-2:]) <= 60:
        wkt = crs
    else:
        raise CoarseError("grid_crs", "crs must be MX-AEA or a northern UTM zone EPSG:326zz")
    return RaoGrid(wkt, (resolution_m, 0, 0, 0, -resolution_m, 0))


def grid_label(value):
    """Validate a coarse grid (A2/A3) and return ``(crs label, resolution)``.

    Planning validity only; evaluation additionally needs ``admit``."""
    if not isinstance(value, RaoGrid):
        raise CoarseError("grid", "grid must be a RaoGrid")
    if value.crs == MX_AEA_WKT:
        label = MX_AEA
    elif _UTM.fullmatch(value.crs) and 1 <= int(value.crs[-2:]) <= 60:
        label = value.crs
    else:
        raise CoarseError("grid_crs", "unsupported grid CRS")
    a, b, c, d, e, f = value.transform
    if b or d:
        raise CoarseError("grid_rotation")
    if a not in PRESETS or a != int(a):
        raise CoarseError("grid_resolution")
    if e != -a:
        raise CoarseError("grid_orientation")
    if c % ORIGIN_MULTIPLE or f % ORIGIN_MULTIPLE:
        raise CoarseError("grid_origin", "translation must be a multiple of 10 km")
    return label, int(a)


def _stage_grid(value, size):
    _, _, c, _, _, f = value.transform
    return RaoGrid(value.crs, (size, 0, c, 0, -size, f))


def _axis_count(source_origin, target_origin, size):
    """Exact positive-overlap 10 m source columns/rows per stage cell: the offset of
    the target origin modulo 10 m is the same for every cell (both are multiples of 10)."""
    offset = (Fraction(str(target_origin)) - Fraction(str(source_origin))) % NATIVE_M
    return size // NATIVE_M + (1 if offset else 0)


def _source_projection(crs, transform):
    if not isinstance(crs, str) or not isinstance(transform, (list, tuple)) or len(transform) != 6:
        raise CoarseError("missing_metadata", "projection needs crs and six affine coefficients")
    try:
        values = tuple(float(v) for v in transform)
    except (TypeError, ValueError):
        raise CoarseError("missing_metadata", "non-numeric affine") from None
    if not all(math.isfinite(v) for v in values):
        raise CoarseError("missing_metadata", "non-finite affine")
    return crs, values


def _source_grid(crs, transform):
    """An admitted native source grid (G1 S1): EPSG:32611-32616 with exactly
    ``[10, 0, c, 0, -10, f]``. Returns ``(crs, zone, c, f)``."""
    crs, (a, b, c, d, e, f) = _source_projection(crs, transform)
    zone = _UTM.fullmatch(crs)
    if not zone or not 11 <= int(zone.group(1)) <= 16:
        raise CoarseError("source_crs", crs)
    if b or d:
        raise CoarseError("source_affine")
    if abs(a) != NATIVE_M or abs(e) != NATIVE_M:
        raise CoarseError("source_pixel")
    if a < 0 or e > 0:
        raise CoarseError("source_orientation")
    return crs, int(zone.group(1)), c, f


def admit(value, projections):
    """Per-stage ``maxPixels`` for a chain whose source grids are admissible, else
    ``CoarseError``. Every contributing band's ``(crs, transform)`` must be a north-up,
    axis-aligned 10 m grid on EPSG:32611-32616.

    A same-zone UTM first stage additionally needs the target zone; its bound is the
    exact rows x columns + 2 per axis. An MX-AEA first stage gets the D129 bound (225
    for 10 -> 100 m, 81 for 10 -> 50 m). That bound alone admits nothing nationally:
    ``composite`` and ``fine_first_reference`` also check every evaluated first-stage
    cell's domain and zone offset per source (``_national``). Nested stages share the
    target CRS and origin: (r + 2)^2."""
    label, resolution = grid_label(value)
    if not projections:
        raise CoarseError("missing_metadata", "contributing band projections are required")
    chain = CHAINS[resolution]
    first = chain[0][1]
    nested = tuple((dst // src + COUNT_MARGIN) ** 2 for src, dst in chain[1:])
    if label == MX_AEA:
        for crs, transform in projections:
            _source_grid(crs, transform)
        return (NATIONAL_FIRST[first],) + nested
    _, _, c_t, _, _, f_t = value.transform
    bound = 0
    for crs, transform in projections:
        crs, _, c, f = _source_grid(crs, transform)
        if crs != label:
            raise CoarseError(NO_PROOF, "cross-CRS first stage to a UTM target has no proved bound")
        nx, ny = _axis_count(c, c_t, first), _axis_count(f, f_t, first)
        bound = max(bound, (nx + COUNT_MARGIN) * (ny + COUNT_MARGIN))
    return (bound,) + nested


# ------------------------------------------------------------------ request validation


def feature_names(bands, stat):
    """Stage-qualified coarse feature names ``<band>__s_mean__t_<stat>``."""
    if stat not in TEMPORAL:
        raise CoarseError("temporal", stat)
    return [f"{b}__s_mean__t_{stat}" for b in bands]


def reference_feature_names(bands, stat):
    """Benchmark-only fine-first names ``<band>__t_<stat>__s_mean``."""
    if stat not in TEMPORAL:
        raise CoarseError("temporal", stat)
    return [f"{b}__t_{stat}__s_mean" for b in bands]


def _aoi(value):
    if not isinstance(value, dict) or value.get("type") not in ("Polygon", "MultiPolygon"):
        raise CoarseError("aoi", "aoi must be a GeoJSON Polygon or MultiPolygon mapping")
    try:
        geometry = shape(value)
    except Exception:
        raise CoarseError("aoi", "invalid GeoJSON geometry") from None
    west, south, east, north = geometry.bounds
    if not geometry.is_valid or geometry.is_empty or geometry.area <= 0:
        raise CoarseError("aoi", "aoi must be a valid, non-empty polygon")
    if west < -180 or east > 180 or south < -85 or north > 85:
        raise CoarseError("aoi", "aoi coordinates must be longitude/latitude")
    return value


def _interval(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise CoarseError("interval", "start and end must be YYYY-MM-DD") from None
    if first.isoformat() != start or last.isoformat() != end or first >= last:
        raise CoarseError("interval", "start must precede the exclusive end")
    return _ms(first), _ms(last)


def _ms(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class _Spec:
    dataset: str
    bands: tuple
    mask: str | None
    filters: tuple
    temporal: tuple
    coverage_min: float
    index: str | None
    db: bool


def _spec(dataset, select, mask, filters, temporal, coverage_min, index, db):
    if dataset not in ("sentinel2", "sentinel1_float"):
        raise CoarseError("dataset", "coarse products use sentinel2 or sentinel1_float")
    allowed = S2_BANDS if dataset == "sentinel2" else S1_BANDS
    bands = (select,) if isinstance(select, str) else tuple(select or ())
    if not bands or len(set(bands)) != len(bands) or not set(bands) <= set(allowed):
        raise CoarseError("bands", f"select a unique non-empty subset of {allowed}")
    temporal = (temporal,) if isinstance(temporal, str) else tuple(temporal or ())
    if not temporal or len(set(temporal)) != len(temporal) or not set(temporal) <= set(TEMPORAL):
        raise CoarseError("temporal", f"temporal must be a unique non-empty subset of {TEMPORAL}")
    if isinstance(coverage_min, bool) or not isinstance(coverage_min, (int, float)) or not (
        math.isfinite(coverage_min) and 0 < coverage_min <= 1
    ):
        raise CoarseError("coverage_min", "coverage_min must lie in (0, 1]")
    if not isinstance(db, bool):
        raise CoarseError("db", "db must be a boolean")
    if dataset == "sentinel2":
        if mask != "s2_cloud_score_plus":
            raise CoarseError("mask", "Sentinel-2 coarse products require mask='s2_cloud_score_plus'")
        if filters not in (None, {}):
            raise CoarseError("filters", "Sentinel-2 coarse products take no filters")
        if db:
            raise CoarseError("db", "dB applies only to Sentinel-1 linear power")
        if index not in (None, "ndvi"):
            raise CoarseError("index", "index must be None or 'ndvi'")
        if index and (not {"B4", "B8"} <= set(bands) or not set(temporal) & {"mean", "median"}):
            raise CoarseError("index", "NDVI needs B4, B8 and a mean or median composite")
        normalized = ()
    else:
        if mask is not None or index is not None:
            raise CoarseError("mask", "Sentinel-1 coarse products take no mask or index")
        if not isinstance(filters, dict):
            raise CoarseError("filters", "Sentinel-1 needs filters with orbitPass and polarizations")
        _sentinel1_filters.validate_filters(filters)
        if filters.get("instrumentMode", _sentinel1_filters.DEFAULT_INSTRUMENT_MODE) != "IW":
            raise CoarseError("filters", "instrumentMode must be IW")
        if "orbitPass" not in filters:
            raise CoarseError("filters", "an explicit orbitPass is required")
        if not {"VV", "VH"} <= set(filters.get("polarizations", ())):
            raise CoarseError("filters", "polarizations must include VV and VH")
        normalized = (
            ("instrumentMode", "IW"),
            ("orbitPass", filters["orbitPass"]),
            ("polarizations", tuple(sorted(filters["polarizations"]))),
        )
    return _Spec(dataset, bands, mask, normalized, temporal, float(coverage_min), index, db)


def _contributing(spec):
    return spec.bands + ((S2_QUALITY_BAND,) if spec.dataset == "sentinel2" else ())


def snapshot_scope(spec_or_dataset, aoi, value, *, bands=None, mask=None, filters=None):
    """Fingerprint of the query scope a snapshot must match (dataset, AOI, grid,
    contributing bands, mask and normalized filters)."""
    if isinstance(spec_or_dataset, _Spec):
        spec = spec_or_dataset
    else:
        spec = _spec(spec_or_dataset, bands, mask, filters, ("median",), DEFAULT_COVERAGE, None, False)
    payload = dict(
        dataset=spec.dataset,
        aoi=aoi,
        grid=dict(crs=value.crs, transform=list(value.transform)),
        bands=list(_contributing(spec)),
        mask=spec.mask,
        filters=[[k, list(v) if isinstance(v, tuple) else v] for k, v in spec.filters],
    )
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ snapshot (C1/C2 rows)


def _limit(value):
    """F4: only a stricter positive integer read limit, never above the fixed ceiling."""
    if type(value) is not int or not 1 <= value <= READ_LIMIT:
        raise CoarseError("limit", f"limit must be an integer in 1..{READ_LIMIT}")
    return value


def _bounded(values, cap, what, code="uncertain"):
    """At most ``cap`` items, consuming no more than ``cap + 1`` (never truncates)."""
    if isinstance(values, (str, bytes, dict)):
        raise CoarseError(code, f"{what} must be a sequence")
    try:
        items = list(islice(iter(values), cap + 1))
    except TypeError:
        raise CoarseError(code, f"{what} must be iterable") from None
    if len(items) > cap:
        raise CoarseError("limit", f"{what} exceeds its limit of {cap}")
    return items


def _parts(value, size, code, what):
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)) or len(value) != size:
        raise CoarseError(code, f"{what} must have {size} parts")
    return tuple(value)


def _key(dataset, platform, acquisition, code="uncertain"):
    """Canonical acquisition key: Sentinel-2 (spacecraft, datatake without baseline) or
    Sentinel-1 (platform letter, missionDataTakeID)."""
    if dataset == "sentinel2":
        if platform not in _S2_PLATFORMS or not isinstance(acquisition, str) or not _ACQUISITION.fullmatch(acquisition):
            raise CoarseError(code, "missing or malformed Sentinel-2 acquisition key")
        return (platform, acquisition)
    if not isinstance(platform, str) or not re.fullmatch(r"[A-Z]", platform):
        raise CoarseError(code, "missing or malformed platform_number")
    if (isinstance(acquisition, bool) or not isinstance(acquisition, (int, float)) or not math.isfinite(acquisition)
            or acquisition != int(acquisition) or acquisition < 0):
        raise CoarseError(code, "missing or malformed missionDataTakeID")
    return (platform, int(acquisition))


def _contributing_names(dataset):
    return S2_BANDS + (S2_QUALITY_BAND,) if dataset == "sentinel2" else S1_BANDS


@dataclass(frozen=True)
class Snapshot:
    """Validated completed C1/C2 reads bound to one query scope. Immutable.

    ``groups`` holds ``(key, first_time_ms, member_ids)`` for acquisitions whose
    earliest complete-member time lies in ``[start, end)``; members after ``end`` are
    retained. ``excluded`` lists keys of acquisitions that began before ``start``.
    ``projections`` maps each member id to its contributing bands'
    ``(band, crs, transform)``. ``extents`` (optional, needed only for MX-AEA targets)
    maps each member id to its native grid ``(width, height)`` in pixels, so its
    footprint is ``[c, c + 10 width] x [f - 10 height, f]``; it is empty or covers
    exactly the members.

    Every construction path validates and canonicalizes: unique keys (included and
    excluded disjoint), at most 600 keys and 1000 members, sorted unique member ids
    in exactly one acquisition each, integer group times inside ``[start, end)``, and
    exactly one projection entry per member with the same contributing bands on one
    finite native grid. Contents become sorted tuples, so later changes to a caller's
    containers cannot alter an accepted snapshot. Errors are ``snapshot_invalid`` or
    ``limit``."""

    dataset: str
    start: str
    end: str
    scope: str
    groups: tuple
    excluded: tuple
    projections: tuple
    extents: tuple = ()

    def __post_init__(self):
        dataset = self.dataset
        if dataset not in ("sentinel2", "sentinel1_float"):
            raise CoarseError("snapshot_invalid", "dataset must be sentinel2 or sentinel1_float")
        try:
            lo, hi = _interval(self.start, self.end)
        except CoarseError:
            raise CoarseError("snapshot_invalid", "start/end must be an ISO run interval") from None
        if not isinstance(self.scope, str) or not re.fullmatch(r"[0-9a-f]{64}", self.scope):
            raise CoarseError("snapshot_invalid", "scope must be a sha256 hex digest")
        bad = "snapshot_invalid"
        groups = []
        for item in _bounded(self.groups, RUN_GROUP_LIMIT, "acquisition groups", bad):
            key, first, ids = _parts(item, 3, bad, "a group")
            key = _key(dataset, *_parts(key, 2, bad, "an acquisition key"), code=bad)
            if type(first) is not int or not lo <= first < hi:
                raise CoarseError(bad, "group times must be integer milliseconds inside the run interval")
            ids = _bounded(ids, READ_LIMIT, "member ids", bad)
            if not ids or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in ids):
                raise CoarseError(bad, "every group needs valid member image ids")
            if len(set(ids)) != len(ids):
                raise CoarseError(bad, "a member id is repeated within an acquisition")
            groups.append((key, first, tuple(sorted(ids))))
        excluded = [_key(dataset, *_parts(k, 2, bad, "an excluded key"), code=bad)
                    for k in _bounded(self.excluded, RUN_GROUP_LIMIT, "excluded keys", bad)]
        keys = [g[0] for g in groups] + excluded
        if len(keys) > RUN_GROUP_LIMIT:
            raise CoarseError("limit", "more than 600 acquisition keys")
        if len(set(keys)) != len(keys):
            raise CoarseError(bad, "an acquisition key is repeated or both included and excluded")
        members = [i for g in groups for i in g[2]]
        if len(members) > READ_LIMIT:
            raise CoarseError("limit", "more than 1000 member images")
        if len(set(members)) != len(members):
            raise CoarseError(bad, "a member image belongs to more than one acquisition")
        allowed = _contributing_names(dataset)
        tables = {}
        for entry in _bounded(self.projections, READ_LIMIT, "projection entries", bad):
            image_id, table = _parts(entry, 2, bad, "a projection entry")
            if not isinstance(image_id, str) or image_id in tables:
                raise CoarseError(bad, "projection entries need unique member ids")
            rows = []
            for band_entry in _bounded(table, 32, "contributing bands", bad):
                band, crs, transform = _parts(band_entry, 3, bad, "a band projection")
                if band not in allowed:
                    raise CoarseError(bad, f"unexpected contributing band {band!r}")
                try:
                    crs, transform = _source_projection(crs, transform)
                except CoarseError:
                    raise CoarseError(bad, f"malformed projection for {image_id}") from None
                rows.append((band, crs, transform))
            rows.sort()
            if not rows or len({r[0] for r in rows}) != len(rows):
                raise CoarseError(bad, "each member needs unique contributing bands")
            if len({r[1:] for r in rows}) != 1:
                raise CoarseError(bad, "a member's contributing bands must share one native grid")
            if dataset == "sentinel2" and S2_QUALITY_BAND not in {r[0] for r in rows}:
                raise CoarseError(bad, "Sentinel-2 members need the cs_cdf projection")
            tables[image_id] = tuple(rows)
        if set(tables) != set(members):
            raise CoarseError(bad, "projection entries must cover exactly the member images")
        if len({tuple(r[0] for r in table) for table in tables.values()}) > 1:
            raise CoarseError(bad, "all members need the same contributing bands")
        extents = {}
        for entry in _bounded(self.extents, READ_LIMIT, "extents", bad):
            image_id, dims = _parts(entry, 2, bad, "an extent")
            dims = _parts(dims, 2, bad, "native dimensions")
            if not isinstance(image_id, str) or image_id in extents or not all(_dimension(v) for v in dims):
                raise CoarseError(bad, "extents need unique member ids and two integers in 1..200000")
            extents[image_id] = dims
        if extents and set(extents) != set(members):
            raise CoarseError(bad, "extents must cover exactly the member images, or be empty")
        object.__setattr__(self, "extents", tuple(sorted(extents.items())))
        object.__setattr__(self, "groups", tuple(sorted(groups, key=lambda g: (g[1], g[0]))))
        object.__setattr__(self, "excluded", tuple(sorted(excluded)))
        object.__setattr__(self, "projections", tuple(sorted(tables.items())))

    @property
    def bands(self):
        """The members' contributing bands (sorted), or ``()`` for an empty snapshot."""
        return tuple(r[0] for r in self.projections[0][1]) if self.projections else ()

    @property
    def fingerprint(self):
        payload = [self.dataset, self.start, self.end, self.scope, self.groups, self.excluded, self.projections]
        if self.extents:  # unchanged fingerprints for snapshots without national metadata
            payload.append(self.extents)
        text = json.dumps(payload, separators=(",", ":"))
        return sha256(text.encode("utf-8")).hexdigest()

    def member_projections(self, ids):
        table = dict(self.projections)
        if any(i not in table for i in ids):
            raise CoarseError("snapshot_invalid", "a requested member has no projection entry")
        return [(crs, list(transform)) for i in ids for _, crs, transform in table[i]]

    def groups_in(self, start, end):
        lo, hi = _interval(start, end)
        run_lo, run_hi = _interval(self.start, self.end)
        if lo < run_lo or hi > run_hi:
            raise CoarseError("snapshot_interval", "the period lies outside the snapshot's run interval")
        return tuple(g for g in self.groups if lo <= g[1] < hi)


def _row_key(dataset, row):
    if dataset == "sentinel2":
        datatake = row.get("DATATAKE_IDENTIFIER")
        match = _DATATAKE.fullmatch(datatake) if isinstance(datatake, str) else None
        if not match:
            raise CoarseError("uncertain", "missing or malformed Sentinel-2 acquisition key")
        return _key(dataset, row.get("SPACECRAFT_NAME"), match.group(1))
    return _key(dataset, row.get("platform_number"), row.get("missionDataTakeID"))


def _row(dataset, row, bands):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]):
        raise CoarseError("uncertain", "missing or malformed image id")
    time = row.get("time_ms")
    if type(time) is not int:
        raise CoarseError("uncertain", "missing or malformed system:time_start")
    key = _row_key(dataset, row)
    projections = row.get("projections")
    if not isinstance(projections, dict) or not set(bands) <= set(projections):
        raise CoarseError("missing_metadata", "projections for every contributing band are required")
    table, dims = [], set()
    for band in bands:
        entry = projections[band]
        if not isinstance(entry, dict):
            raise CoarseError("missing_metadata", band)
        crs, transform = _source_projection(entry.get("crs"), entry.get("transform"))
        table.append((band, crs, transform))
        dims.add(_row_dimensions(entry.get("dimensions")))
    if len({(crs, t) for _, crs, t in table}) != 1:
        raise CoarseError("source_projection_mismatch", "contributing bands must share one native grid")
    if len(dims) != 1:
        code = "missing_metadata" if None in dims else "source_projection_mismatch"
        raise CoarseError(code, "contributing bands must share one native extent (dimensions), all or none given")
    return row["id"], time, key, tuple(table), dims.pop()


def _dimension(value):
    return type(value) is int and 1 <= value <= MAX_DIMENSION


def _row_dimensions(value):
    """Optional band ``dimensions`` ``[width, height]`` (Earth Engine band metadata)."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 2 or \
            not all(_dimension(v) for v in value):
        raise CoarseError("missing_metadata", "dimensions must be two integers in 1..200000")
    return tuple(value)


def build_snapshot(*, dataset, aoi, grid, start, end, select, candidate_rows, completion_rows,
                   mask=None, filters=None, limit=READ_LIMIT):
    """Validate completed candidate (date-filtered) and completion (key-filtered, no
    date) rows and assign whole acquisitions by their earliest complete-member time.

    Rows: ``{"id", "time_ms", key fields, "projections": {band: {"crs", "transform"}}}``;
    each band entry may also carry ``"dimensions": [width, height]`` (needed for MX-AEA
    targets; all bands of a row, and all members, give it or none do).
    Key fields are ``SPACECRAFT_NAME``/``DATATAKE_IDENTIFIER`` (Sentinel-2) or
    ``platform_number``/``missionDataTakeID`` (Sentinel-1). Every candidate id must
    appear once in C1 and once in C2 with identical normalized time, acquisition key
    and contributing-band projections; anything else is ``uncertain`` (or
    ``missing_metadata``). ``limit`` (1..1000, default 1000) bounds each read: at most
    ``limit + 1`` rows are consumed and an overflow fails; more than 600 candidate
    acquisition keys fail before pre-run exclusion. Nothing is truncated or fetched."""
    spec = _spec(dataset, select, mask, filters, ("median",), DEFAULT_COVERAGE, None, False)
    limit = _limit(limit)
    lo, hi = _interval(start, end)
    bands = _contributing(spec)
    candidates = {}
    for item in (_row(dataset, r, bands) for r in _bounded(candidate_rows, limit, "candidate read")):
        if item[0] in candidates:
            raise CoarseError("uncertain", f"candidate image {item[0]} appears more than once")
        if not lo <= item[1] < hi:
            raise CoarseError("uncertain", "candidate row outside the run interval")
        candidates[item[0]] = item
    keys = {item[2] for item in candidates.values()}
    if len(keys) > RUN_GROUP_LIMIT:
        raise CoarseError("limit", "more than 600 candidate acquisition keys")
    members = {}
    for item in (_row(dataset, r, bands) for r in _bounded(completion_rows, limit, "completion read")):
        if item[0] in members or item[2] not in keys:
            raise CoarseError("uncertain", "completion rows must be unique members of candidate keys")
        members[item[0]] = item
    for image_id, item in candidates.items():
        if image_id not in members:
            raise CoarseError("uncertain", "a candidate image is absent from the completion read")
        if members[image_id] != item:
            raise CoarseError("uncertain", f"candidate and completion metadata disagree for {image_id}")
    by_key = {}
    for item in members.values():
        by_key.setdefault(item[2], []).append(item)
    groups, excluded, projections, extents = [], [], [], []
    for key in sorted(by_key):
        group = sorted(by_key[key], key=lambda item: item[0])
        first = min(item[1] for item in group)  # <= its candidate member's time, so < end
        if first < lo:
            excluded.append(key)
        else:
            groups.append((key, first, tuple(item[0] for item in group)))
            projections.extend((item[0], item[3]) for item in group)
            extents.extend((item[0], item[4]) for item in group)
    given = [e for e in extents if e[1] is not None]
    if given and len(given) != len(extents):
        raise CoarseError("missing_metadata", "native dimensions must be given for all members or none")
    return Snapshot(dataset, start, end, snapshot_scope(spec, aoi, grid), tuple(groups),
                    tuple(excluded), tuple(projections), tuple(given))


def _base_collection(spec, region):
    if spec.dataset == "sentinel2":
        return ee.ImageCollection(sentinel2.COLLECTION_ID).filterBounds(region)
    filters = dict(spec.filters)
    col = (
        ee.ImageCollection(sentinel1_float.COLLECTION_ID)
        .filterBounds(region)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.eq("orbitProperties_pass", filters["orbitPass"]))
        .filter(ee.Filter.eq("resolution_meters", NATIVE_M))
    )
    for pol in ("VV", "VH"):
        col = col.filter(ee.Filter.listContains("transmitterReceiverPolarisation", pol))
    return col


def candidate_query(*, dataset, aoi, grid, start, end, select, mask=None, filters=None, limit=READ_LIMIT):
    """Lazy C1 query: product filters, cell-union bounds and the run interval, limited to
    ``limit + 1`` rows so an overflow is detectable (``limit`` 1..1000). Building it
    evaluates nothing."""
    spec = _spec(dataset, select, mask, filters, ("median",), DEFAULT_COVERAGE, None, False)
    limit = _limit(limit)
    _interval(start, end)
    return _base_collection(spec, cell_region(aoi, grid)).filterDate(start, end).limit(limit + 1)


def completion_query(*, dataset, aoi, grid, keys, select, mask=None, filters=None, limit=READ_LIMIT):
    """Lazy C2 query: the same scope with **no date filter**, restricted to the candidate
    acquisition keys (Sentinel-2 datatake without processing baseline; Sentinel-1
    ``missionDataTakeID``; platform is re-checked on the rows). At most 601 keys are
    consumed; 1..600 valid keys and ``limit`` 1..1000 are required."""
    spec = _spec(dataset, select, mask, filters, ("median",), DEFAULT_COVERAGE, None, False)
    limit = _limit(limit)
    keys = sorted({_key(spec.dataset, *_parts(k, 2, "uncertain", "a key"))
                   for k in _bounded(keys, RUN_GROUP_LIMIT, "completion keys")})
    if not keys:
        raise CoarseError("limit", "completion needs 1..600 keys")
    col = _base_collection(spec, cell_region(aoi, grid))
    if spec.dataset == "sentinel2":
        col = col.map(
            lambda image: image.set(
                "geecomposer_acquisition",
                ee.String(image.get("DATATAKE_IDENTIFIER")).replace(_DATATAKE_SUFFIX, ""),
            )
        )
        col = col.filter(ee.Filter.inList("geecomposer_acquisition", [k[1] for k in keys]))
    else:
        col = col.filter(ee.Filter.inList("missionDataTakeID", sorted({k[1] for k in keys})))
    return col.limit(limit + 1)


# ------------------------------------------------------------------ graph construction


def cell_region(aoi, value):
    """Union of the target-grid cells intersecting the AOI (A4 full-cell support)."""
    _aoi(aoi)
    grid_label(value)
    geometry = ee.Geometry(aoi, "EPSG:4326", False)
    return geometry.coveringGrid(value.projection()).geometry()


def _source(spec, image_id):
    if spec.dataset == "sentinel2":
        image = ee.Image(f"{sentinel2.COLLECTION_ID}/{image_id}")
        values = image.select(list(spec.bands)).toDouble().multiply(S2_SCALE)
        score = ee.Image(f"{sentinel2._CS_PLUS_COLLECTION}/{image_id}").select(S2_QUALITY_BAND)
        return values, score.gte(sentinel2._CS_PLUS_THRESHOLD)
    image = ee.Image(f"{sentinel1_float.COLLECTION_ID}/{image_id}")
    return image.select(list(spec.bands)).toDouble(), None


def _zeros(names, projection):
    return ee.Image.constant([0] * len(names)).rename(names).setDefaultProjection(projection)


def sum_weight_inputs(values, quality, names, projection=None, clip=None):
    """Contract section 6 steps 1-7: finite S/W reducer inputs with mask exactly 1.

    The finite flag is taken first; non-finite values are replaced with 0 before any
    multiplication; the fractional validity m is carried once, as a value; and
    ``where`` on a constant zero base keeps the base's unbounded footprint. In the
    synthetic paths measured in M005-S05 correction247, ``where`` gave its output the
    value's fractional mask, so that mask reached the reducer and weighted it a second
    time; the stacked value therefore keeps only presence (mask > 0) as its mask
    (``updateMask`` replaced a nonzero mask there). The same paths read a mask back as
    float32, so the support is cast to double before it forms W; otherwise W and every
    S/W would lose double precision.
    The base takes the native source projection unless an explicit one is given (a
    computed composite has no native grid of its own). For MX-AEA targets ``clip`` is
    the admitted rectangle plus margin: the reducer input exists only there."""
    projection = values.select(0).projection() if projection is None else projection
    support = values.mask().reduce(ee.Reducer.min()).toDouble()
    finite = values.abs().lte(MAX_DOUBLE).reduce(ee.Reducer.min())
    weight = support.multiply(finite) if quality is None else support.multiply(quality).multiply(finite)
    replaced = values.where(finite.Not(), 0)
    numerator = replaced.multiply(weight)
    value = numerator.addBands(weight).rename(names)
    base = _zeros(names, projection)
    if clip is not None:
        base = base.clip(clip)
    return base.where(weight.gt(0), value.updateMask(value.mask().gt(0)))


def _staged(image, value, bounds, names, keep=None):
    """reduceResolution(mean, bestEffort=False, maxPixels) per stage, rebased between
    stages so every reducer input keeps mask 1; no threshold here. With ``keep`` (an
    admitted coarse-aligned rectangle, MX-AEA), the first stage's output is kept only
    there; elsewhere the rebase is a mask-1 zero, so margin cells never leak."""
    _, resolution = grid_label(value)
    for index, ((_, size), max_pixels) in enumerate(zip(CHAINS[resolution], bounds)):
        projection = _stage_grid(value, size).projection()
        reduced = image.reduceResolution(ee.Reducer.mean(), False, max_pixels).reproject(projection)
        test = ee.Image.constant(1) if keep is None or index else ee.Image.constant(1).clip(keep)
        image = _zeros(names, projection).where(test, reduced)
    return image


def _select_granule(stacks):
    """Per cell: the member with the largest W; ties keep the smaller id (strict >)."""
    best = stacks[0]
    for item in stacks[1:]:
        best = best.where(item.select("W").gt(best.select("W")), item)
    return best


def _coverage_admitted(weight, theta):
    """Final coverage admission shared by the coarse-first observation and the fine-first
    reference (D179): finite W, W > 0, and W >= theta or |W - theta| <= 1e-12.

    W is a full-cell coverage fraction in double; an exact theta (for example a half
    cell) can arrive a few ulps below theta after the staged means. The fixed absolute
    tolerance only decides admission; W, S and every value stay unrounded, and zero or
    absent support is never admitted, whatever theta is. Use only after the last stage."""
    finite = weight.abs().lte(MAX_DOUBLE)
    near = weight.subtract(theta).abs().lte(COVERAGE_TOLERANCE)
    return finite.And(weight.gt(0)).And(weight.gte(theta).Or(near))


def _observation(best, bands, theta, projection):
    """Final-only threshold: value S/W and valid=1 where coverage is admitted, else zeros."""
    weight = best.select("W")
    valid = _coverage_admitted(weight, theta)
    value = best.select([f"S_{b}" for b in bands], list(bands)).divide(weight)
    names = list(bands) + ["W", "valid"]
    return _zeros(names, projection).where(valid, value.addBands(weight).addBands(ee.Image.constant(1)).rename(names))


def _median(images):
    """Exact order statistic from an explicit sort (no histogram); even N averages the
    two central values. A padding element keeps indices valid when N = 0 (masked)."""
    ordered = ee.ImageCollection.fromImages(images).toArray().arrayProject([0]).arraySort()
    count = ordered.arrayLength(0)
    padded = ordered.arrayCat(ee.Image.constant(0).toArray(), 0)
    low = count.subtract(1).max(0).divide(2).floor().toInt()
    high = count.divide(2).floor().toInt()
    return padded.arrayGet(low).add(padded.arrayGet(high)).divide(2).updateMask(count.gt(0))


def temporal_statistics(observations, bands, stats, *, prefix="s_mean", db=False, index=None):
    """Equal-observation statistics over binary-valid observations (explicit sums).

    Every collection operation here (sum, min/max, the sorted-array median) needs one
    band type, but inferred types follow each observation's values; each observation
    is cast to double once, first, so every derived collection is homogeneous. The cast
    changes no value, mask, band name or order."""
    observations = [ee.Image(o).toDouble() for o in observations]
    total = ee.ImageCollection.fromImages(observations).sum()
    count = total.select("valid")
    defined = count.gt(0)
    out, done = [], {}
    for band in bands:
        mean = total.select(band).divide(count).updateMask(defined)
        masked = [o.select(band).updateMask(o.select("valid")) for o in observations]
        low = ee.ImageCollection.fromImages(masked).min()
        high = ee.ImageCollection.fromImages(masked).max()
        for stat in stats:
            if stat == "mean":
                image = mean
            elif stat == "median":
                image = _median(masked)
            elif stat == "sd_pop":
                squares = [o.select(band).subtract(mean).multiply(o.select("valid")).pow(2) for o in observations]
                image = ee.ImageCollection.fromImages(squares).sum().divide(count).sqrt().updateMask(defined)
            elif stat == "min":
                image = low
            elif stat == "max":
                image = high
            else:
                image = high.subtract(low)
            done[(band, stat)] = image
            out.append(image.rename(f"{band}__{prefix}__t_{stat}"))
    if db:
        for band in bands:
            for stat in stats:
                if stat in DB_STATS:
                    image = done[(band, stat)]
                    out.append(image.log10().multiply(10).updateMask(image.gt(0)).rename(f"{band}__{prefix}__t_{stat}__db"))
    if index == "ndvi":
        for stat in stats:
            if stat in ("median", "mean"):
                red, nir = done[("B4", stat)], done[("B8", stat)]
                denominator = nir.add(red)
                ndvi = nir.subtract(red).divide(denominator).updateMask(denominator.gt(0))
                out.append(ndvi.rename(f"ndvi__of__{prefix}__t_{stat}"))
    out.append(count.rename("obs__t_count"))
    out.append(total.select("W").divide(count).updateMask(defined).rename("cov__t_mean"))
    return ee.Image.cat(out)


def _empty(names, projection, zero="obs__t_count"):
    """Schema-preserving empty product: masked statistics in ``names`` order and a valid
    constant 0 for the ``zero`` band (the last name)."""
    bands = [ee.Image.constant(0).updateMask(0).rename(n) for n in names if n != zero]
    return ee.Image.cat(bands + [ee.Image.constant(0).rename(zero)]).setDefaultProjection(projection)


def output_names(spec):
    names = [f"{b}__s_mean__t_{s}" for b in spec.bands for s in spec.temporal]
    if spec.db:
        names += [f"{b}__s_mean__t_{s}__db" for b in spec.bands for s in spec.temporal if s in DB_STATS]
    if spec.index == "ndvi":
        names += [f"ndvi__of__s_mean__t_{s}" for s in spec.temporal if s in ("median", "mean")]
    return names + ["obs__t_count", "cov__t_mean"]


def _membership(region):
    return ee.Image.constant(1).clip(region).mask().gt(0)


def _aea_rectangle(rect):
    return ee.Geometry.Rectangle(list(rect), ee.Projection(MX_AEA_WKT), False)


def _national_request(aoi, resolution):
    """The MX-AEA region that every requested coarse cell lies in (AOI mapped with a
    chord margin, plus one coarse-cell diagonal); the AOI must lie in the G1 domain."""
    return _national_region(json.dumps(aoi, sort_keys=True), resolution)


@lru_cache(maxsize=32)
def _national_region(aoi_text, resolution):
    geometry = shape(json.loads(aoi_text))
    west, south, east, north = geometry.bounds
    if west < geo.LON_RANGE[0] or east > geo.LON_RANGE[1] or south < geo.LAT_RANGE[0] or north > geo.LAT_RANGE[1]:
        raise CoarseError("national_domain", "the AOI must lie within latitude 13.5..34.0 and longitude -119.5..-85.5")
    return geo.aoi_region(geometry).buffer(resolution * math.sqrt(2))


def _national(value, aoi, snapshot, members):
    """D129/D130 per-source, whole-cell admission for an MX-AEA target.

    For each member: its native footprint (from ``extents``) is covered in MX-AEA; the
    part that can reach requested cells is aligned outward to whole coarse cells
    (``R``); every point of ``R`` plus a 1 km halo, which contains every first-stage
    cell the confined reducer can evaluate, must lie in the G1 domain and within 5
    degrees of the member's zone meridian. Returns ``({id: (zone, R)}, excluded)``,
    where excluded members cannot reach any requested cell. Any failure refuses the
    request with the member and reason; nothing is dropped silently."""
    _, resolution = grid_label(value)
    _, _, c_t, _, _, f_t = value.transform
    reach = _national_request(aoi, resolution)
    tables, extents = dict(snapshot.projections), dict(snapshot.extents)
    pieces, excluded = {}, []
    for image_id in members:
        if image_id not in extents:
            raise CoarseError("missing_metadata", "MX-AEA admission needs native dimensions for every member")
        _, crs, transform = tables[image_id][0]
        _, zone, c, f = _source_grid(crs, transform)
        cover, failure = geo.footprint_cover(zone, c, f, *extents[image_id])
        if failure:
            raise CoarseError(failure, f"{image_id}: native footprint outside the supported latitude/zone band")
        bounds = geo.intersection_bounds(reach, cover)
        if bounds is None:
            excluded.append(image_id)
            continue
        rect = geo.aligned(bounds, resolution, c_t, f_t)
        failure = geo.domain_failure(geo.rectangle_extremes(*geo.expanded(rect, geo.CHECK_HALO_M)), zone)
        if failure:
            raise CoarseError(failure, f"{image_id} (zone {zone}) would reduce cells outside the reviewed G1 conditions")
        pieces[image_id] = (zone, rect)
    return pieces, excluded


def _national_record(pieces, excluded):
    return json.dumps(
        {
            "halo_m": geo.CHECK_HALO_M,
            "clip_margin_m": geo.CLIP_MARGIN_M,
            "footprint_margin_m": geo.FOOTPRINT_MARGIN_M,
            "pieces": [[i, zone, list(rect)] for i, (zone, rect) in sorted(pieces.items())],
            "excluded_disjoint": sorted(excluded),
        },
        separators=(",", ":"),
    )


def _bind(snapshot, spec, aoi, value, start, end):
    if snapshot is None:
        raise CoarseError("missing_metadata", "a completed Snapshot is required; no metadata is fetched")
    if not isinstance(snapshot, Snapshot) or snapshot.dataset != spec.dataset:
        raise CoarseError("snapshot_scope", "snapshot must be a Snapshot for this dataset")
    if snapshot.scope != snapshot_scope(spec, aoi, value):
        raise CoarseError("snapshot_scope", "snapshot was built for a different query scope")
    if snapshot.projections and snapshot.bands != tuple(sorted(_contributing(spec))):
        raise CoarseError("snapshot_scope", "snapshot members carry different contributing bands")
    groups = snapshot.groups_in(start, end)
    if len(groups) > PERIOD_GROUP_LIMIT:
        raise CoarseError("limit", "more than 400 acquisition groups in the period")
    return groups


def composite(*, dataset, aoi, start, end, select, grid, snapshot=None, temporal=("median",),
              coverage_min=DEFAULT_COVERAGE, mask=None, filters=None, index=None, db=False):
    """Coarse-first composite: per acquisition, staged area/mask-weighted spatial mean of
    each granule, maximum-W granule per cell, threshold after the last stage; then
    equal-observation temporal statistics. Returns a lazy ``ee.Image``.

    ``snapshot`` (a ``Snapshot`` for this exact scope) supplies image identity and
    projections; without it the call fails, it never fetches. On the national MX-AEA
    grid every member also needs native ``extents``; each member's reduction is
    confined to its admitted rectangle (``_national``) and an inadmissible member
    refuses the request."""
    spec = _spec(dataset, select, mask, filters, temporal, coverage_min, index, db)
    label, resolution = grid_label(grid)
    _aoi(aoi)
    _interval(start, end)
    national = label == MX_AEA
    if national:
        _national_request(aoi, resolution)
    groups = _bind(snapshot, spec, aoi, grid, start, end)
    members = [i for g in groups for i in g[2]]
    bounds = admit(grid, snapshot.member_projections(members)) if members else ()
    pieces, excluded = _national(grid, aoi, snapshot, members) if national and members else ({}, [])
    region = cell_region(aoi, grid)
    projection = grid.projection()
    names = [f"S_{b}" for b in spec.bands] + ["W"]
    observations = []
    for _, _, ids in groups:
        stacks = []
        for i in ids:
            if not national:
                stacks.append(_staged(sum_weight_inputs(*_source(spec, i), names), grid, bounds, names))
            elif i in pieces:  # members outside ``pieces`` cannot reach a requested cell
                rect = pieces[i][1]
                clip = _aea_rectangle(geo.expanded(rect, geo.CLIP_MARGIN_M))
                inputs = sum_weight_inputs(*_source(spec, i), names, clip=clip)
                stacks.append(_staged(inputs, grid, bounds, names, keep=_aea_rectangle(rect)))
        if stacks:
            observations.append(_observation(_select_granule(stacks), spec.bands, spec.coverage_min, projection))
    if observations:
        image = temporal_statistics(observations, spec.bands, spec.temporal, db=spec.db, index=spec.index)
        image = image.setDefaultProjection(projection)
    else:
        image = _empty(output_names(spec), projection)
    image = image.updateMask(_membership(region))
    if national:
        image = image.set("coarse:national", _national_record(pieces, excluded))
    return image.set(
        {
            "coarse:product": "s2_reflectance" if dataset == "sentinel2" else "s1_linear_power",
            "coarse:resolution_m": resolution,
            "coarse:grid": json.dumps(dict(crs=label, transform=list(grid.transform))),
            "coarse:stage_max_pixels": list(bounds),
            "coarse:coverage_min": spec.coverage_min,
            "coarse:temporal": list(spec.temporal),
            "coarse:start": start,
            "coarse:end": end,
            "coarse:groups": len(groups),
            "coarse:sources": members,
            "coarse:snapshot": snapshot.fingerprint,
        }
    )


def _four_features(bands):
    """F5: exactly one ``<band>__s_mean__t_median`` feature for each of B2, B3, B4 and B8,
    returned as source bands in the given (model) order."""
    matches = [_FEATURE.fullmatch(b) if isinstance(b, str) else None for b in bands]
    if not all(matches) or sorted(m.group(1) for m in matches) != sorted(S2_BANDS):
        raise CoarseError("model_bands", "a coarse Rao model needs exactly the B2, B3, B4 and B8 "
                                         "<band>__s_mean__t_median features, once each")
    return [m.group(1) for m in matches]


def _model_features(model):
    if not isinstance(model, RaoNormalization):
        raise CoarseError("model", "a frozen RaoNormalization is required; nothing is fitted here")
    return _four_features(model.bands)


def rao(composite_image, *, model, grid, aoi):
    """Strict 3x3 GN Rao (existing ``rao_q``) on a coarse median composite with one
    frozen model. Eight bands: Rao value/count/coverage, ``obs__t_count`` and the four
    features. Support is 3X x 3X coarse cells; never fits."""
    _model_features(model)
    label, resolution = grid_label(grid)
    result = rao_q(composite_image, list(model.bands), grid=grid, region=cell_region(aoi, grid),
                   window_size=3, model=model, missing="strict")
    result = result.select(["rao_gn", "rao_valid_count", "rao_coverage"],
                           ["coarse_rao_gn", "coarse_rao_valid_count", "coarse_rao_coverage"])
    result = result.addBands(composite_image.select(["obs__t_count"] + list(model.bands)))
    return result.set(
        {
            "coarse:product": "coarse_rao_gn_3x3",
            "coarse:resolution_m": resolution,
            "coarse:support_m": 3 * resolution,
            "coarse:grid": json.dumps(dict(crs=label, transform=list(grid.transform))),
            "rao:calibration_sha256": model.fingerprint,
        }
    )


def composite_periods(periods, *, model=None, snapshot=None, **kwargs):
    """One call for named periods (for example ``grouping.monthly_periods(2025)``): the
    same grid, snapshot and, for Rao, one frozen model. Empty periods keep the schema
    with masked statistics and zero count. Never fits or evaluates."""
    periods = validate_periods(periods)
    if model is not None:
        bases = _model_features(model)
        if tuple(kwargs.get("temporal", ("median",))) != ("median",) or kwargs.get("index") is not None:
            raise CoarseError("model", "Rao periods need temporal=('median',) and no index")
        if list(kwargs.get("select", ())) != bases:
            raise CoarseError("model_bands", "select must equal the model's feature bands, in order")
    results = {}
    for label, start, end in periods:
        image = composite(start=start, end=end, snapshot=snapshot, **kwargs)
        if model is not None:
            image = rao(image, model=model, grid=kwargs["grid"], aoi=kwargs["aoi"])
        results[label] = image.set({"coarse:period": label, "coarse:period_start": start, "coarse:period_end": end})
    return results


def _fine_grid(value, target_label):
    if not isinstance(value, RaoGrid):
        raise CoarseError("fine_grid", "fine_grid must be a RaoGrid")
    match = _UTM.fullmatch(value.crs)
    a, b, c, d, e, f = value.transform
    if not match or not 11 <= int(match.group(1)) <= 16 or (a, b, d, e) != (10, 0, 0, -10) or c % 10 or f % 10:
        raise CoarseError("fine_grid", "fine_grid must be a 10 m north-up UTM 11-16 grid on 10 m multiples")
    if target_label != MX_AEA and value.crs != target_label:
        raise CoarseError(NO_PROOF, "fine-to-target stage must stay in one CRS for a UTM target")
    return value


def fine_first_reference(*, dataset, aoi, start, end, select, grid, fine_grid, snapshot=None,
                         temporal=("median",), coverage_min=DEFAULT_COVERAGE, mask=None, filters=None):
    """Benchmark-only reference in the opposite order: per fine pixel the lowest-ID valid
    granule, the temporal statistic on the declared 10 m ``fine_grid`` (sources resampled
    to it by nearest neighbour), then the same staged spatial mean. Member sources pass
    the same admission as the coarse product. Bands are ``<band>__t_<stat>__s_mean``
    plus ``cov__s_mean``. A period without acquisitions keeps that schema: masked
    statistics and ``cov__s_mean`` 0 on the target cell union. On an MX-AEA target the
    reference stays local: the AOI's whole coarse-cell rectangle plus halo must satisfy
    the G1 domain and the fine grid's zone offset, and the fine-to-MX-AEA reduction is
    confined to that rectangle. Not a supported product."""
    spec = _spec(dataset, select, mask, filters, temporal, coverage_min, None, False)
    label, resolution = grid_label(grid)
    _aoi(aoi)
    _interval(start, end)
    fine = _fine_grid(fine_grid, label)
    keep = None
    if label == MX_AEA:
        _, fine_zone, _, _ = _source_grid(fine.crs, list(fine.transform))
        _, _, c_t, _, _, f_t = grid.transform
        keep = geo.aligned(_national_request(aoi, resolution).bounds, resolution, c_t, f_t)
        failure = geo.domain_failure(geo.rectangle_extremes(*geo.expanded(keep, geo.CHECK_HALO_M)), fine_zone)
        if failure:
            raise CoarseError(failure, "the fine-first reference is local: its AOI must fit one fine-grid zone")
    groups = _bind(snapshot, spec, aoi, grid, start, end)
    members = [i for g in groups for i in g[2]]
    if members:
        admit(grid, snapshot.member_projections(members))  # no source the coarse product would refuse
        if keep is not None:
            _national(grid, aoi, snapshot, members)
    bounds = admit(grid, [(fine.crs, list(fine.transform))])
    out_names = [f"{b}__t_{s}__s_mean" for b in spec.bands for s in spec.temporal]
    if groups:
        reference = _fine_first(spec, groups, fine.projection(), grid, bounds, out_names, keep)
    else:  # no acquisition: statistics missing, spatial coverage zero; no empty collection
        reference = _empty(out_names + ["cov__s_mean"], grid.projection(), zero="cov__s_mean")
    if keep is not None:
        reference = reference.set("reference:national_rectangle", json.dumps(list(keep)))
    return reference.updateMask(_membership(cell_region(aoi, grid))).set(
        {
            "reference:benchmark_only": True,
            "reference:order": "temporal_then_spatial",
            "reference:fine_grid": json.dumps(dict(crs=fine.crs, transform=list(fine.transform))),
            "reference:fine_grid_resampling": "nearest",
            "reference:granule_selection": "lowest_id_valid_per_fine_pixel",
            "reference:confounds": REFERENCE_CONFOUNDS,
            "coarse:stage_max_pixels": list(bounds),
        }
    )


def _fine_first(spec, groups, fine_projection, grid, bounds, out_names, keep=None):
    names = list(spec.bands) + ["valid"]
    observations = []
    for _, _, ids in groups:
        chosen = _zeros(names, fine_projection)
        for image_id in sorted(ids, reverse=True):  # smaller ids applied last, so they win
            values, quality = _source(spec, image_id)
            support = values.mask().reduce(ee.Reducer.min())
            finite = values.abs().lte(MAX_DOUBLE).reduce(ee.Reducer.min())
            weight = support.multiply(finite) if quality is None else support.multiply(quality).multiply(finite)
            candidate = values.where(finite.Not(), 0).addBands(ee.Image.constant(1)).rename(names)
            chosen = chosen.where(weight.gt(0), candidate)
        observations.append(chosen.addBands(chosen.select("valid").rename("W")))
    stats = temporal_statistics(observations, spec.bands, spec.temporal, prefix="fine")
    stat_names = [f"{b}__fine__t_{s}" for b in spec.bands for s in spec.temporal]
    defined = stats.select("obs__t_count").gt(0)
    values = stats.select(stat_names).updateMask(defined).setDefaultProjection(fine_projection)
    stage_names = [f"S_{n}" for n in stat_names] + ["W"]
    clip = None if keep is None else _aea_rectangle(geo.expanded(keep, geo.CLIP_MARGIN_M))
    inputs = sum_weight_inputs(values, None, stage_names, fine_projection, clip=clip)
    staged = _staged(inputs, grid, bounds, stage_names, keep=None if keep is None else _aea_rectangle(keep))
    weight = staged.select("W")
    valid = _coverage_admitted(weight, spec.coverage_min)
    reference = staged.select([f"S_{n}" for n in stat_names], out_names).divide(weight).updateMask(valid)
    return reference.addBands(weight.rename("cov__s_mean"))


def reference_feature_map(model_bands, reference_bands):
    """A19: ordered one-to-one map from reference ``<b>__t_median__s_mean`` bands to the
    frozen model's ``<b>__s_mean__t_median`` bands, by source band name. The model must
    carry exactly the four admitted features (F5)."""
    model_bands = list(model_bands)
    bases = _four_features(model_bands)
    reference_bands = list(reference_bands)
    if len(set(reference_bands)) != len(reference_bands):
        raise CoarseError("duplicate", "reference bands must be unique")
    pairs = []
    for base, model_band in zip(bases, model_bands):
        name = f"{base}__t_median__s_mean"
        if name not in reference_bands:
            raise CoarseError("missing", f"reference feature {name} is missing")
        pairs.append((name, model_band))
    return tuple(pairs)


def reference_rao(reference, *, model, grid, aoi, reference_bands):
    """Rao of the benchmark reference with the same frozen model: features are mapped by
    name into the model's band order (no refit, no model change); outputs keep the
    reference identity as ``reference_rao_*``."""
    _model_features(model)
    pairs = reference_feature_map(model.bands, reference_bands)
    grid_label(grid)
    mapped = reference.select([r for r, _ in pairs], [m for _, m in pairs])
    result = rao_q(mapped, list(model.bands), grid=grid, region=cell_region(aoi, grid),
                   window_size=3, model=model, missing="strict")
    result = result.select(["rao_gn", "rao_valid_count", "rao_coverage"],
                           ["reference_rao_gn", "reference_rao_valid_count", "reference_rao_coverage"])
    return result.set(
        {
            "reference:feature_map": json.dumps([list(p) for p in pairs]),
            "reference:benchmark_only": True,
            "rao:calibration_sha256": model.fingerprint,
        }
    )
