"""National MX-AEA activation under D129/D130 (S02 round 3).

Synthetic national-span requests built through the real production entry points
with the installed EE client offline (``real_ee``; any evaluation fails). Geometry
expectations come from PROJ (pyproj), independently of ``_coarse_geometry``; they
test the admission algorithm, not real Mexico imagery or catalogue coverage.
"""

import dataclasses
import json
import math

import pytest
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union

from geecomposer import _coarse_geometry as geo
from geecomposer import coarse, grouping
from geecomposer.rao import RaoGrid, RaoNormalization
from coarse_oracle import overlapping, q, rao, spatial, temporal
from ee_graph import Graph

BANDS = ["B2", "B3", "B4", "B8"]
S2_BANDS = BANDS + ["cs_cdf"]
AEA = CRS.from_wkt(coarse.MX_AEA_WKT)
TO_AEA = Transformer.from_crs("EPSG:4326", AEA, always_xy=True)
FROM_AEA = Transformer.from_crs(AEA, "EPSG:4326", always_xy=True)


def square(west, south, east, north):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


# Disconnected national-span AOI: a Pacific island (zone 11), a Sonora piece on the
# zone 12/13 seam and a Yucatan piece (zone 16).
AOI = {"type": "MultiPolygon", "coordinates": [
    [square(-118.40, 28.95, -118.25, 29.10)],
    [square(-108.50, 27.10, -107.40, 27.45)],
    [square(-87.70, 20.30, -87.20, 20.60)],
]}
LOCAL_AOI = {"type": "Polygon", "coordinates": [square(-110.30, 27.20, -110.10, 27.40)]}
TILE = 10980  # Sentinel-2 10 m tile width/height in pixels
# zone, upper-left (c, f): synthetic S2-like tiles covering the AOI pieces
TILES = {
    "Z11": (11, 300000, 3300000),
    "Z12": (12, 699960, 3100020),
    "Z13": (13, 199980, 3100020),
    "Z16": (16, 399960, 2300040),
}


def ms(text):
    from datetime import datetime, timezone
    return round(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def proj(zone, c, f, dims=True):
    value = {"crs": f"EPSG:326{zone}", "transform": [10, 0, c, 0, -10, f]}
    if dims:
        value["dimensions"] = [TILE, TILE]
    return value


def s2_row(image_id, when, datatake, tile, spacecraft="Sentinel-2A", dims=True, bands=S2_BANDS):
    zone, c, f = TILES[tile] if isinstance(tile, str) else tile
    return {"id": image_id, "time_ms": ms(when), "SPACECRAFT_NAME": spacecraft,
            "DATATAKE_IDENTIFIER": f"GS2A_20250101T000000_{datatake:06d}_N05.11",
            "projections": {b: proj(zone, c, f, dims) for b in bands}}


# Two acquisitions in June: datatake 1 spans zones 11, 12 and 13 (12/13 overlap on the
# seam); datatake 2 covers zones 12 and 16. One July acquisition; other months are empty.
ROWS = [
    s2_row("SYNTH_N1_Z11", "2025-06-10T18:20:00Z", 1, "Z11"),
    s2_row("SYNTH_N1_Z12", "2025-06-10T18:20:30Z", 1, "Z12"),
    s2_row("SYNTH_N1_Z13", "2025-06-10T18:21:00Z", 1, "Z13"),
    s2_row("SYNTH_N2_Z12", "2025-06-15T17:50:00Z", 2, "Z12", "Sentinel-2B"),
    s2_row("SYNTH_N2_Z16", "2025-06-15T16:10:00Z", 2, "Z16", "Sentinel-2B"),
    s2_row("SYNTH_N3_Z16", "2025-07-02T16:10:00Z", 3, "Z16"),
]
S2 = dict(dataset="sentinel2", aoi=AOI, grid=coarse.grid(1000), select=BANDS, mask="s2_cloud_score_plus")


def national_snapshot(rows=ROWS, **kw):
    params = {**S2, **kw}
    return coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=rows, completion_rows=rows,
                                 **params)


def national(start="2025-06-01", end="2025-07-01", snapshot=None, **kw):
    params = {**S2, **kw}
    snap = snapshot if snapshot is not None else national_snapshot(
        **{k: v for k, v in params.items() if k in ("aoi", "grid", "select", "dataset", "mask", "filters")})
    return coarse.composite(start=start, end=end, snapshot=snap, **params), snap


def record(image):
    graph = Graph(image)
    for call in graph.calls("Element.set"):
        if graph.constant(call["key"]) == "coarse:national":
            return json.loads(graph.constant(call["value"]))
    raise AssertionError("no coarse:national record")


def footprint_aea(zone, c, f, w=TILE, h=TILE, step=1000.0):
    """Independent PROJ image of a native footprint boundary in MX-AEA (dense polygon)."""
    to_geo = Transformer.from_crs(f"EPSG:326{zone}", "EPSG:4326", always_xy=True)
    ring = []
    for (ax, ay), (bx, by) in (((c, f - 10 * h), (c + 10 * w, f - 10 * h)), ((c + 10 * w, f - 10 * h), (c + 10 * w, f)),
                               ((c + 10 * w, f), (c, f)), ((c, f), (c, f - 10 * h))):
        n = max(1, math.ceil(math.hypot(bx - ax, by - ay) / step))
        for i in range(n):
            lon, lat = to_geo.transform(ax + (bx - ax) * i / n, ay + (by - ay) * i / n)
            ring.append(TO_AEA.transform(lon, lat))
    return Polygon(ring)


def aoi_cells_aea(aoi, size):
    """Independent cover of the requested coarse cells: every grid cell meeting the AOI
    (dense PROJ image), enumerated only inside this small synthetic AOI's bounds."""
    parts = []
    for polygon in shape(aoi).geoms if shape(aoi).geom_type == "MultiPolygon" else [shape(aoi)]:
        coords = []
        ring = list(polygon.exterior.coords)
        for (a, b), (cc, d) in zip(ring, ring[1:]):
            for i in range(50):
                coords.append(TO_AEA.transform(a + (cc - a) * i / 50, b + (d - b) * i / 50))
        mapped = Polygon(coords)
        x0, y0, x1, y1 = mapped.bounds
        cells = [box(i * size, j * size, (i + 1) * size, (j + 1) * size)
                 for i in range(math.floor(x0 / size), math.ceil(x1 / size))
                 for j in range(math.floor(y0 / size), math.ceil(y1 / size))
                 if box(i * size, j * size, (i + 1) * size, (j + 1) * size).intersects(mapped)]
        parts.append(unary_union(cells))
    return unary_union(parts)


# ------------------------------------------------------------------ 1. bounds and stages


@pytest.mark.parametrize("resolution", coarse.PRESETS)
def test_national_bounds_for_all_six_presets(resolution):
    chain = coarse.CHAINS[resolution]
    expected = (225 if chain[0][1] == 100 else 81,) + tuple((b // a + 2) ** 2 for a, b in chain[1:])
    sources = [("EPSG:32611", [10, 0, 300000, 0, -10, 3300000]), ("EPSG:32616", [10, 0, 399965, 0, -10, 2300033])]
    assert coarse.admit(coarse.grid(resolution), sources) == expected
    assert coarse.grid(resolution).crs == coarse.MX_AEA_WKT and coarse.grid(resolution).transform == (
        resolution, 0, 0, 0, -resolution, 0)


def test_250_m_chain_uses_the_50_m_intermediate(real_ee, runner_offline):
    image, _ = national(grid=coarse.grid(250))
    graph = Graph(image)
    stages = list(graph.calls("Image.reduceResolution"))
    assert sorted({graph.constant(s["maxPixels"]) for s in stages}) == [49, 81]
    assert all(graph.constant(s["bestEffort"]) is False for s in stages)
    for piece in record(image)["pieces"]:
        x0, y0, x1, y1 = piece[2]
        assert all(v % 250 == 0 for v in (x0, y0, x1, y1))  # whole 250 m cells, so whole 50 m children


def test_stage_ancestry_reduce_then_reproject_to_nested_mx_aea_grids(real_ee, runner_offline):
    image, snap = national()
    graph = Graph(image)
    stages = list(graph.calls("Image.reduceResolution"))
    members = [i for g in snap.groups_in("2025-06-01", "2025-07-01") for i in g[2]]
    assert len(stages) == 2 * len(members)  # every member: 10->100 then 100->1000
    assert {graph.constant(s["maxPixels"]) for s in stages} == {225, 144}
    reprojects = list(graph.calls("Image.reproject"))
    transforms = {tuple(graph.constant(graph.resolve(r["crs"])["functionInvocationValue"]["arguments"]["transform"]))
                  for r in reprojects}
    assert transforms == {(100.0, 0.0, 0.0, 0.0, -100.0, 0.0), (1000.0, 0.0, 0.0, 0.0, -1000.0, 0.0)}
    crs = {graph.constant(graph.resolve(r["crs"])["functionInvocationValue"]["arguments"]["crs"]) for r in reprojects}
    assert crs == {coarse.MX_AEA_WKT}


# ------------------------------------------------------------------ 2. national span, coverage, seams


def test_every_member_contributes_over_its_whole_requested_support(real_ee, runner_offline):
    image, snap = national()
    rec = record(image)
    pieces = {i: (zone, box(*rect)) for i, zone, rect in rec["pieces"]}
    june = [i for g in snap.groups_in("2025-06-01", "2025-07-01") for i in g[2]]
    assert sorted(pieces) == sorted(june) and rec["excluded_disjoint"] == []
    text = image.serialize()
    for image_id in june:
        assert f"COPERNICUS/S2_SR_HARMONIZED/{image_id}" in text
    cells = aoi_cells_aea(AOI, 1000)
    for image_id, (zone, rect) in pieces.items():
        _, c, f = next(t for t in TILES.values() if f"Z{t[0]}" in image_id)
        reach = footprint_aea(zone, c, f).intersection(cells)
        assert not reach.is_empty and rect.contains(reach)  # no requested contribution falls outside R
        assert all(v % 1000 == 0 for v in rect.bounds)  # whole coarse cells: descendants are complete


def test_members_that_cannot_reach_the_request_are_excluded_and_recorded(real_ee, runner_offline):
    island = {"type": "Polygon", "coordinates": [square(-118.40, 28.95, -118.25, 29.10)]}
    image, snap = national(snapshot=national_snapshot(aoi=island), aoi=island)
    rec = record(image)
    assert [p[0] for p in rec["pieces"]] == ["SYNTH_N1_Z11"]
    assert rec["excluded_disjoint"] == ["SYNTH_N1_Z12", "SYNTH_N1_Z13", "SYNTH_N2_Z12", "SYNTH_N2_Z16"]
    cells = aoi_cells_aea(island, 1000)
    for image_id in rec["excluded_disjoint"]:  # independently: their footprints miss every requested cell
        zone, c, f = next(t for t in TILES.values() if f"Z{t[0]}" in image_id)
        assert not footprint_aea(zone, c, f).intersects(cells)
    text = image.serialize()
    loads = "COPERNICUS/S2_SR_HARMONIZED/"
    assert loads + "SYNTH_N1_Z11" in text  # excluded ids stay in provenance, but no source graph is built
    assert all(loads + i not in text for i in rec["excluded_disjoint"])


def test_requested_cell_union_keeps_islands_and_all_pieces(real_ee, runner_offline):
    image, _ = national()
    graph = Graph(image)
    covering = list(graph.calls("Geometry.coveringGrid"))
    assert covering
    projection = graph.resolve(covering[0]["proj"])["functionInvocationValue"]["arguments"]
    assert graph.constant(projection["crs"]) == coarse.MX_AEA_WKT
    text = image.serialize()
    for ring in AOI["coordinates"]:
        assert str(ring[0][0][0]) in text  # every disconnected piece reaches coveringGrid
    rects = [box(*p[2]) for p in record(image)["pieces"]]
    for piece in shape(AOI).geoms:  # each piece lies in some member's admitted rectangle
        centre = TO_AEA.transform(piece.centroid.x, piece.centroid.y)
        assert any(r.contains(box(centre[0] - 1, centre[1] - 1, centre[0] + 1, centre[1] + 1)) for r in rects)


def test_confinement_clips_inputs_and_keeps_only_whole_admitted_cells(real_ee, runner_offline):
    image, _ = national()
    graph = Graph(image)
    rects = {tuple(p[2]) for p in record(image)["pieces"]}
    clip_rects, keep_rects = set(), set()
    for call in graph.calls("Image.clip"):
        geometry = graph.resolve(call["geometry"]).get("functionInvocationValue")
        if not geometry or geometry["functionName"] != "GeometryConstructors.Rectangle":
            continue
        coords = tuple(graph.constant(geometry["arguments"]["coordinates"]))
        target = graph.resolve(call["input"])["functionInvocationValue"]["functionName"]
        (keep_rects if target == "Image.constant" else clip_rects).add(coords)
    assert keep_rects == rects
    margin = geo.CLIP_MARGIN_M
    assert clip_rects == {(x0 - margin, y0 - margin, x1 + margin, y1 + margin) for x0, y0, x1, y1 in rects}
    for x0, y0, x1, y1 in rects:  # every evaluated first-stage cell lies inside the checked halo
        assert margin + 100 * math.sqrt(2) < geo.CHECK_HALO_M
        assert (x1 - x0) % 1000 == 0 and (y1 - y0) % 1000 == 0


def test_same_acquisition_across_zones_is_one_observation_with_max_w_selection(real_ee, runner_offline):
    image, snap = national()
    graph = Graph(image)
    june = snap.groups_in("2025-06-01", "2025-07-01")
    assert [len(g[2]) for g in june] == [3, 2]  # datatake 1 spans three zones, datatake 2 two
    lists = [graph.resolve(c["images"])["arrayValue"]["values"] for c in graph.calls("ImageCollection.fromImages")]
    assert lists and all(len(v) == len(june) for v in lists)  # one observation per acquisition
    selections = [c for c in graph.calls("Image.where") if is_w_comparison(graph, c["test"])]
    assert len(selections) == sum(len(g[2]) - 1 for g in june)  # max-W chain within each acquisition


def is_w_comparison(graph, node):
    call = graph.resolve(node).get("functionInvocationValue", {})
    if call.get("functionName") != "Image.gt":
        return False
    left = graph.resolve(call["arguments"]["image1"]).get("functionInvocationValue", {})
    return left.get("functionName") == "Image.select" and graph.constant(left["arguments"]["bandSelectors"]) == ["W"]


def call_of(graph, node):
    inv = graph.resolve(node)["functionInvocationValue"]
    return inv["functionName"], inv["arguments"]


def test_national_stages_take_mask_one_inputs_and_threshold_only_at_the_end(real_ee, runner_offline):
    image, _ = national(coverage_min=0.37, temporal=("mean",))
    graph = Graph(image)
    stages = list(graph.calls("Image.reduceResolution"))
    for stage in stages:
        name, args = call_of(graph, stage["image"])
        assert name == "Image.where"  # finite S/W on a constant zero base
        base, base_args = call_of(graph, args["input"])
        assert base in ("Image.clip", "Image.setDefaultProjection")  # first stage confined, nested rebased
        if base == "Image.clip":
            assert call_of(graph, base_args["input"])[0] == "Image.setDefaultProjection"
        thresholds = [g for g in graph.calls("Image.gte", stage["image"])
                      if call_of(graph, g["image2"])[0] == "Image.constant"
                      and graph.constant(call_of(graph, g["image2"])[1]["value"]) == 0.37]
        assert not thresholds  # no coverage threshold feeds any spatial stage
    finals = [g for g in graph.calls("Image.gte") if call_of(graph, g["image2"])[0] == "Image.constant"
              and graph.constant(call_of(graph, g["image2"])[1]["value"]) == 0.37]
    assert finals and all(list(graph.calls("Image.reduceResolution", g["image1"])) for g in finals)


def test_oracle_seam_cell_takes_one_granule_and_full_denominators():
    """Two granules of one acquisition over a seam cell: max W wins (smaller id on ties);
    weights are never added across granules, and the denominator is the full cell."""
    weights = [q("1/4")] * 4
    granule_a = [1, 1, None, None]  # zone 12 granule covers half the cell
    granule_b = [3, 3, 3, 3]  # zone 13 granule covers it all
    coverage = {g: sum(w for w, v in zip(weights, vals) if v is not None) for g, vals in (("A", granule_a), ("B", granule_b))}
    assert coverage == {"A": q("1/2"), "B": 1} and max(coverage, key=coverage.get) == "B"
    w, s, mean = spatial([1, 1, 0, 0], [1, 1, 0, 0], weights, q("1/2"))
    assert (w, mean) == (q("1/2"), 1)  # missing quarter cells count as zero weight, not dropped
    assert temporal([3])["count"] == 1  # one observation for the acquisition


# ------------------------------------------------------------------ 3. whole-cell and zone boundaries


def test_rectangle_extremes_cover_curved_edges_exactly():
    x, y = TO_AEA.transform(-102.0, 33.95)  # straddles the Albers central meridian x = FE
    rect = (x - 50000, y - 2000, x + 50000, y)  # wide rectangle: the top edge bows north
    lat_min, lat_max, lon_min, lon_max = geo.rectangle_extremes(*rect)
    corner_lats = [FROM_AEA.transform(cx, cy)[1] for cx in (rect[0], rect[2]) for cy in (rect[1], rect[3])]
    top_mid = FROM_AEA.transform((rect[0] + rect[2]) / 2 + (2500000 - (rect[0] + rect[2]) / 2) * 0, rect[3])[1]
    apex_x = min(max(geo.FALSE_EASTING, rect[0]), rect[2])
    nearest = FROM_AEA.transform(apex_x, rect[3])[1]
    assert lat_max > max(corner_lats) and abs(lat_max - nearest) < 1e-9 and top_mid <= lat_max + 1e-12
    assert abs(lat_min - min(corner_lats)) < 1e-9
    corner_lons = [FROM_AEA.transform(cx, cy)[0] for cx in (rect[0], rect[2]) for cy in (rect[1], rect[3])]
    assert abs(lon_min - min(corner_lons)) < 1e-9 and abs(lon_max - max(corner_lons)) < 1e-9
    # dense PROJ sampling of the boundary never escapes the computed extremes
    for t in range(201):
        for px, py in ((rect[0] + (rect[2] - rect[0]) * t / 200, rect[1]), (rect[0] + (rect[2] - rect[0]) * t / 200, rect[3]),
                       (rect[0], rect[1] + (rect[3] - rect[1]) * t / 200), (rect[2], rect[1] + (rect[3] - rect[1]) * t / 200)):
            lon, lat = FROM_AEA.transform(px, py)
            assert lat_min - 1e-9 <= lat <= lat_max + 1e-9 and lon_min - 1e-9 <= lon <= lon_max + 1e-9


def cell_at(lon, lat, size=100):
    x, y = TO_AEA.transform(lon, lat)
    return (x - size / 2, y - size / 2, x + size / 2, y + size / 2)


@pytest.mark.parametrize(
    "lon, lat, zone, expected",
    [
        (-106.0010, 27.0, 12, None),  # centre and whole cell within 5 degrees of -111
        (-105.9998, 27.0, 12, "zone_offset"),  # centre at 5.0002 degrees: outside
        (-87.0, 34.0 - 0.0002, 16, "national_domain"),  # centre inside 34.0, top edge outside
        (-87.0, 13.5 + 0.0002, 16, "national_domain"),  # centre inside 13.5, bottom edge outside
        (-119.5 + 0.0002, 29.0, 11, "national_domain"),  # centre inside, west edge outside
        (-85.5 - 0.0002, 20.0, 16, "national_domain"),  # centre inside, east edge outside
        (-87.0, 20.0, 16, None),
    ],
)
def test_whole_cell_domain_and_zone_offset_use_every_point(lon, lat, zone, expected):
    cell = cell_at(lon, lat)
    cx, cy = (cell[0] + cell[2]) / 2, (cell[1] + cell[3]) / 2
    centre_lon, centre_lat = FROM_AEA.transform(cx, cy)
    assert abs(centre_lon - lon) < 1e-7 and abs(centre_lat - lat) < 1e-7
    assert geo.domain_failure(geo.rectangle_extremes(*cell), zone) == expected


def test_centre_inside_edge_outside_cell_is_refused_where_a_centre_test_would_admit():
    # a 100 m cell whose centre is 0.0002 deg inside the 5 degree line: its east edge is outside
    cell = cell_at(-106.0002, 27.0)
    centre_lon = FROM_AEA.transform((cell[0] + cell[2]) / 2, (cell[1] + cell[3]) / 2)[0]
    assert abs(centre_lon - (-111.0)) < 5.0
    east = max(FROM_AEA.transform(cell[2], y)[0] for y in (cell[1], cell[3]))
    assert east > -106.0
    assert geo.domain_failure(geo.rectangle_extremes(*cell), 12) == "zone_offset"


def test_member_whose_admitted_rectangle_crosses_the_zone_limit_refuses_the_request(real_ee):
    # a zone-12 tile reaching beyond -106 over a requested piece: refused, not truncated
    far = s2_row("SYNTH_FAR_Z12", "2025-06-10T18:20:30Z", 7, (12, 899960, 3100020))
    aoi = {"type": "Polygon", "coordinates": [square(-107.0, 27.1, -106.0, 27.4)]}
    with pytest.raises(coarse.CoarseError) as err:
        national(snapshot=national_snapshot([far], aoi=aoi), aoi=aoi)
    assert err.value.code == "zone_offset" and "SYNTH_FAR_Z12" in str(err.value)


def test_aoi_outside_the_reviewed_domain_is_refused_before_metadata():
    aoi = {"type": "Polygon", "coordinates": [square(-87.0, 33.9, -86.9, 34.1)]}
    with pytest.raises(coarse.CoarseError) as err:
        coarse.composite(start="2025-06-01", end="2025-07-01", snapshot=object(), **{**S2, "aoi": aoi})
    assert err.value.code == "national_domain"


def test_footprint_cover_contains_the_proj_image_of_every_tile():
    for zone, c, f in TILES.values():
        cover, failure = geo.footprint_cover(zone, c, f, TILE, TILE)
        assert failure is None and box(*cover).contains(footprint_aea(zone, c, f, step=100.0))


# ------------------------------------------------------------------ 4. metadata contract


def test_national_request_without_dimensions_fails_clearly(real_ee):
    rows = [dict(r, projections={b: proj(*TILES["Z12"], dims=False) for b in S2_BANDS}) for r in ROWS[1:2]]
    snap = national_snapshot(rows)
    assert snap.extents == ()
    with pytest.raises(coarse.CoarseError) as err:
        national(snapshot=snap)
    assert err.value.code == "missing_metadata"
    utm = RaoGrid("EPSG:32612", (1000, 0, 400000, 0, -1000, 3100000))
    local = coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=rows, completion_rows=rows,
                                  **{**S2, "aoi": LOCAL_AOI, "grid": utm})
    coarse.composite(start="2025-06-01", end="2025-07-01", snapshot=local, **{**S2, "aoi": LOCAL_AOI, "grid": utm})


@pytest.mark.parametrize(
    "dims, code",
    [([0, 10980], "missing_metadata"), ([10980], "missing_metadata"), ([10980.0, 10980], "missing_metadata"),
     ([True, 10980], "missing_metadata"), ("10980x10980", "missing_metadata"), ([10980, 200001], "missing_metadata")],
)
def test_malformed_dimensions_are_rejected(dims, code):
    row = s2_row("SYNTH_BAD", "2025-06-10T18:20:30Z", 1, "Z12")
    row["projections"] = {b: dict(proj(*TILES["Z12"]), dimensions=dims) for b in S2_BANDS}
    with pytest.raises(coarse.CoarseError) as err:
        national_snapshot([row])
    assert err.value.code == code


def test_dimensions_must_agree_across_bands_members_and_reads():
    row = s2_row("SYNTH_MIX", "2025-06-10T18:20:30Z", 1, "Z12")
    row["projections"]["cs_cdf"] = dict(proj(*TILES["Z12"]), dimensions=[5490, 5490])
    with pytest.raises(coarse.CoarseError) as err:
        national_snapshot([row])
    assert err.value.code == "source_projection_mismatch"
    partial = s2_row("SYNTH_PART", "2025-06-10T18:20:30Z", 1, "Z12")
    partial["projections"]["cs_cdf"] = proj(*TILES["Z12"], dims=False)
    with pytest.raises(coarse.CoarseError) as err:
        national_snapshot([partial])
    assert err.value.code == "missing_metadata"
    mixed = [ROWS[0], dict(ROWS[1], projections={b: proj(*TILES["Z12"], dims=False) for b in S2_BANDS})]
    with pytest.raises(coarse.CoarseError) as err:
        national_snapshot(mixed)
    assert err.value.code == "missing_metadata"
    changed = dict(ROWS[1], projections={b: dict(proj(*TILES["Z12"]), dimensions=[10980, 10979]) for b in S2_BANDS})
    with pytest.raises(coarse.CoarseError) as err:  # C1 and C2 disagree on the native extent
        coarse.build_snapshot(start="2025-01-01", end="2026-01-01", candidate_rows=[ROWS[1]], completion_rows=[changed],
                              **S2)
    assert err.value.code == "uncertain"


@pytest.mark.parametrize(
    "extents",
    [
        lambda s: s.extents[:-1],  # a member without an extent
        lambda s: s.extents + (("SYNTH_ZZ", (10980, 10980)),),  # a non-member
        lambda s: tuple((i, (w, 0)) for i, (w, _) in s.extents),  # zero size
        lambda s: tuple((i, (w, True)) for i, (w, _) in s.extents),  # bool
        lambda s: s.extents + s.extents[:1],  # duplicate entry
        lambda s: (("SYNTH_N1_Z11", 10980),) + s.extents[1:],  # not a pair
    ],
)
def test_snapshot_extents_are_validated_on_every_construction_path(extents):
    snap = national_snapshot()
    bad = extents(snap)
    with pytest.raises(coarse.CoarseError) as err:
        dataclasses.replace(snap, extents=bad)
    assert err.value.code == "snapshot_invalid"
    fields = {f.name: getattr(snap, f.name) for f in dataclasses.fields(snap)}
    with pytest.raises(coarse.CoarseError) as err:
        coarse.Snapshot(**{**fields, "extents": bad})
    assert err.value.code == "snapshot_invalid"


def test_extents_are_immutable_canonical_and_fingerprinted():
    snap = national_snapshot()
    held = [[i, list(d)] for i, d in reversed(snap.extents)]
    direct = coarse.Snapshot(snap.dataset, snap.start, snap.end, snap.scope, snap.groups, snap.excluded,
                             snap.projections, held)
    assert direct.extents == snap.extents and direct.fingerprint == snap.fingerprint
    held[0][1][0] = 1
    assert direct.extents == snap.extents
    without = dataclasses.replace(snap, extents=())
    assert without.fingerprint != snap.fingerprint
    local_rows = [dict(r, projections={b: proj(*TILES["Z12"], dims=False) for b in S2_BANDS}) for r in ROWS[1:2]]
    assert national_snapshot(local_rows).extents == ()


@pytest.mark.parametrize(
    "crs, transform, code",
    [
        ("EPSG:32617", [10, 0, 300000, 0, -10, 2300040], "source_crs"),
        ("EPSG:32610", [10, 0, 300000, 0, -10, 3300000], "source_crs"),
        ("EPSG:32612", [20, 0, 699960, 0, -20, 3100020], "source_pixel"),
        ("EPSG:32612", [10, 1, 699960, 0, -10, 3100020], "source_affine"),
        ("EPSG:32612", [10, 0, 699960, 0, 10, 3100020], "source_orientation"),
    ],
)
def test_unsupported_native_grids_are_refused(real_ee, crs, transform, code):
    row = s2_row("SYNTH_GRID", "2025-06-10T18:20:30Z", 1, "Z12")
    row["projections"] = {b: {"crs": crs, "transform": transform, "dimensions": [TILE, TILE]} for b in S2_BANDS}
    with pytest.raises(coarse.CoarseError) as err:
        national(snapshot=national_snapshot([row]))
    assert err.value.code == code


# ------------------------------------------------------------------ 5/6. workflows and months


def test_sentinel1_linear_national_without_gamma_map(real_ee, runner_offline):
    filters = {"orbitPass": "DESCENDING", "polarizations": ["VV", "VH"]}
    rows = []
    for k, tile in enumerate(("Z12", "Z16")):
        zone, c, f = TILES[tile]
        rows.append({"id": f"SYNTH_S1_{tile}", "time_ms": ms("2025-06-12T00:10:00Z") + k, "platform_number": "A",
                     "missionDataTakeID": 4242, "projections": {b: proj(zone, c, f) for b in ("VV", "VH")}})
    params = dict(dataset="sentinel1_float", select=["VV", "VH"], mask=None, filters=filters)
    snap = national_snapshot(rows, **params)
    image, _ = national(snapshot=snap, **params, temporal=("mean", "median"), db=True)
    text = image.serialize()
    assert "COPERNICUS/S1_GRD_FLOAT/SYNTH_S1_Z12" in text and "COPERNICUS/S1_GRD_FLOAT/SYNTH_S1_Z16" in text
    assert "0.0001" not in text and "focal" not in text.lower() and "s1_linear_power" in text
    assert len(record(image)["pieces"]) == 2


def normalization(bands):
    count = len(bands)
    return RaoNormalization(bands=list(bands), means=[0.05 * (i + 1) for i in range(count)],
                            scales=[0.02 * (i + 1) for i in range(count)],
                            loadings=[[1 if i == j else 0 for j in range(count)] for i in range(count)],
                            retained_fraction=1.0, n_sigmas=6.0, sample_count=10, decimal_places=None,
                            calibration="national-frozen")


def test_twelve_national_months_s2_and_rao_with_one_frozen_model(real_ee, runner_offline):
    order = ["B8", "B3", "B2", "B4"]
    model = normalization([f"{b}__s_mean__t_median" for b in order])
    before = (model.fingerprint, model.to_json())
    snap = national_snapshot(select=order)
    months = grouping.monthly_periods(2025)
    s2 = coarse.composite_periods(months, snapshot=snap, temporal=("median",), index="ndvi", **{**S2, "select": order})
    raos = coarse.composite_periods(months, model=model, snapshot=snap, temporal=("median",), **{**S2, "select": order})
    assert list(s2) == list(raos) == [m[0] for m in months]
    for label, image in s2.items():
        stages = list(Graph(image).calls("Image.reduceResolution"))
        assert (len(stages) == 10) if label == "2025-06" else (len(stages) == 2) if label == "2025-07" else not stages
    for image in raos.values():
        graph = Graph(image)
        assert model.fingerprint in image.serialize() and not list(graph.calls("Image.reduceRegion"))
        selections = [graph.constant(c["bandSelectors"]) for c in graph.calls("Image.select")]
        assert ["obs__t_count"] + list(model.bands) in selections
    assert (model.fingerprint, model.to_json()) == before
    assert rao([[0, 0, 0, 1]] + [[0, 0, 0, 0]] * 8) == q("16/81")  # four-feature metric (oracle)


# ------------------------------------------------------------------ fine-first (local only)


def test_local_fine_first_reference_on_mx_aea_and_its_refusals(real_ee, runner_offline):
    rows = [s2_row("SYNTH_L_Z12", "2025-06-10T18:20:30Z", 1, (12, 699960, 3100020))]
    params = {**S2, "aoi": LOCAL_AOI}
    fine = RaoGrid("EPSG:32612", (10, 0, 600000, 0, -10, 3100000))
    snap = national_snapshot(rows, aoi=LOCAL_AOI)
    image = coarse.fine_first_reference(start="2025-06-01", end="2025-07-01", snapshot=snap, fine_grid=fine,
                                        temporal=("median",), **params)
    graph = Graph(image)
    assert sorted({graph.constant(s["maxPixels"]) for s in graph.calls("Image.reduceResolution")}) == [144, 225]
    text = image.serialize()
    for value in ("reference:national_rectangle", "lowest_id_valid_per_fine_pixel", '"nearest"', "reference:confounds"):
        assert value in text
    with pytest.raises(coarse.CoarseError) as err:  # one fine grid cannot serve the whole country
        coarse.fine_first_reference(start="2025-06-01", end="2025-07-01", snapshot=national_snapshot(),
                                    fine_grid=fine, **S2)
    assert err.value.code in ("zone_offset", "national_domain")
    far_fine = RaoGrid("EPSG:32614", (10, 0, 0, 0, -10, 3100000))  # zone 14: AOI is > 5 degrees from -99
    with pytest.raises(coarse.CoarseError) as err:
        coarse.fine_first_reference(start="2025-06-01", end="2025-07-01", snapshot=snap, fine_grid=far_fine, **params)
    assert err.value.code == "zone_offset"


# ------------------------------------------------------------------ 7. no hidden work


def test_national_construction_requests_nothing(real_ee, runner_offline, monkeypatch):
    import ee

    def deny(*args, **kwargs):
        pytest.fail("national graph construction made an Earth Engine request")

    monkeypatch.setattr(ee.data, "_execute_cloud_call", deny)
    monkeypatch.setattr(ee.batch.Task, "start", deny)
    image, snap = national()
    assert snap.fingerprint in image.serialize()
    months = coarse.composite_periods(grouping.monthly_periods(2025), snapshot=snap, temporal=("median",), **S2)
    assert all(m.serialize() for m in months.values())


def test_interval_counts_still_match_for_same_zone_targets():
    """Same-CRS UTM bounds are unchanged by national activation (exact interval counts)."""
    utm = RaoGrid("EPSG:32612", (1000, 0, 400000, 0, -1000, 3100000))
    assert coarse.admit(utm, [("EPSG:32612", [10, 0, 500003, 0, -10, 3100007])]) == (169, 144)
    assert len(overlapping(500003, 10, 400000, 400100)) == 11
