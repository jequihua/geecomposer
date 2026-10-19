"""Coarse grids, contributor admission and stage parameters (S01 F01; A1-A3, A8).

Admission is pure Python; graph checks use the installed EE client offline
(``real_ee``) and never evaluate. Interval counts are checked against an
independent enumeration in ``coarse_oracle``.
"""

from fractions import Fraction

import pytest

from geecomposer import coarse
from geecomposer.rao import RaoGrid
from coarse_oracle import overlapping
from ee_graph import Graph

ORIGIN = (400000, 3100000)
SOURCES = {  # F01 same-zone sources
    "aligned": ("EPSG:32612", [10, 0, 499980, 0, -10, 3100020]),
    "translated": ("EPSG:32612", [10, 0, 500003, 0, -10, 3100007]),
    "one_axis": ("EPSG:32612", [10, 0, 500000, 0, -10, 3100007]),
}
EXPECTED = {  # F01 expected_bounds
    100: {"aligned": (144,), "translated": (169,), "one_axis": (156,)},
    250: {"aligned": (49, 49), "translated": (64, 49), "one_axis": (56, 49)},
    500: {"aligned": (144, 49), "translated": (169, 49), "one_axis": (156, 49)},
    1000: {"aligned": (144, 144), "translated": (169, 144), "one_axis": (156, 144)},
    5000: {"aligned": (144, 144, 49), "translated": (169, 144, 49), "one_axis": (156, 144, 49)},
    10000: {"aligned": (144, 144, 144), "translated": (169, 144, 144), "one_axis": (156, 144, 144)},
}
MXAEA_SOURCES = [  # F01 pilot AOIs' ordinary UTM metadata (national bounds under D129)
    ("EPSG:32612", [10, 0, 500000, 0, -10, 3100000]),
    ("EPSG:32614", [10, 0, 400000, 0, -10, 2200000]),
    ("EPSG:32613", [10, 0, 300000, 0, -10, 2460000]),
]


def utm_grid(resolution):
    return RaoGrid("EPSG:32612", (resolution, 0, ORIGIN[0], 0, -resolution, ORIGIN[1]))


@pytest.mark.parametrize("resolution", coarse.PRESETS)
def test_presets_share_one_implementation_and_origin(resolution):
    g = coarse.grid(resolution)
    assert g.crs == coarse.MX_AEA_WKT and g.transform == (resolution, 0, 0, 0, -resolution, 0)
    assert coarse.grid_label(g) == (coarse.MX_AEA, resolution)
    assert coarse.grid(resolution, "EPSG:32612").transform == g.transform  # no origin/CRS switch by X
    chain = coarse.CHAINS[resolution]
    assert chain[0][0] == 10 and chain[-1][1] == resolution
    assert all(b % a == 0 and b // a <= 10 for a, b in chain)
    assert all(prev[1] == nxt[0] for prev, nxt in zip(chain, chain[1:]))
    assert 10000 % resolution == 0


@pytest.mark.parametrize("value", [200, 2000, 1000.0, 1000.5, "1000", True, 0, -1000, 20000, None])
def test_non_preset_resolutions_are_rejected(value):
    with pytest.raises(coarse.CoarseError) as err:
        coarse.grid(value)
    assert err.value.code == "resolution"


@pytest.mark.parametrize(
    "crs, transform, code",
    [
        ("EPSG:32614", (1000, 0, 400000, 0, -1000, 2500000), None),
        (coarse.MX_AEA_WKT, (500, 0, 20000, 0, -500, -30000), None),
        (coarse.MX_AEA_WKT, (1000, 0, 500, 0, -1000, 0), "grid_origin"),
        (coarse.MX_AEA_WKT, (1000, 1, 0, 0, -1000, 0), "grid_rotation"),
        (coarse.MX_AEA_WKT, (1000, 0, 0, 0, -500, 0), "grid_orientation"),
        (coarse.MX_AEA_WKT, (1000, 0, 0, 0, 1000, 0), "grid_orientation"),
        (coarse.MX_AEA_WKT, (2000, 0, 0, 0, -2000, 0), "grid_resolution"),
        ("EPSG:4326", (0.01, 0, -100, 0, -0.01, 20), "grid_crs"),
        ("EPSG:3857", (1000, 0, 0, 0, -1000, 0), "grid_crs"),
        ("EPSG:32712", (1000, 0, 0, 0, -1000, 0), "grid_crs"),
    ],
)
def test_grid_rules(crs, transform, code):
    if code is None:
        coarse.grid_label(RaoGrid(crs, transform))
        return
    with pytest.raises(coarse.CoarseError) as err:
        coarse.grid_label(RaoGrid(crs, transform))
    assert err.value.code == code


@pytest.mark.parametrize("resolution", coarse.PRESETS)
def test_national_admit_checks_source_grids_and_returns_d129_bounds(resolution):
    """D129: native 10 m -> MX-AEA first stages 225 (10->100 m) or 81 (10->50 m), nested
    (r+2)^2; admit checks every source grid but never admits the domain by itself."""
    g = coarse.grid(resolution)
    chain = coarse.CHAINS[resolution]
    first = 225 if chain[0][1] == 100 else 81
    assert coarse.admit(g, MXAEA_SOURCES) == (first,) + tuple((b // a + 2) ** 2 for a, b in chain[1:])
    for projections, code in (
        (None, "missing_metadata"),
        ([("EPSG:4326", [0.0001, 0, -110.1, 0, -0.0001, 27.5])], "source_crs"),
        ([("EPSG:32612", [20, 0, 500000, 0, -20, 3100000])], "source_pixel"),
        ([("EPSG:32612", [10, 0, float("inf"), 0, -10, 3100000])], "missing_metadata"),
    ):
        with pytest.raises(coarse.CoarseError) as err:
            coarse.admit(g, projections)
        assert err.value.code == code


def test_national_requests_need_a_snapshot_and_fail_before_earth_engine():
    """No hidden read: a national request without a completed snapshot fails before any
    Earth Engine object is built (EE is not initialized here)."""
    aoi = {"type": "Polygon", "coordinates": [[[-110.05, 27.3], [-109.95, 27.3], [-109.95, 27.4], [-110.05, 27.4], [-110.05, 27.3]]]}
    for builder in (coarse.composite, coarse.fine_first_reference):
        for snapshot, code in ((None, "missing_metadata"), (object(), "snapshot_scope")):
            kwargs = dict(dataset="sentinel2", aoi=aoi, start="2025-01-01", end="2025-04-01", select=["B4", "B8"],
                          grid=coarse.grid(1000), mask="s2_cloud_score_plus", snapshot=snapshot)
            if builder is coarse.fine_first_reference:
                kwargs["fine_grid"] = RaoGrid("EPSG:32612", (10, 0, 0, 0, -10, 0))
            with pytest.raises(coarse.CoarseError) as err:
                builder(**kwargs)
            assert err.value.code == code


@pytest.mark.parametrize("resolution", coarse.PRESETS)
@pytest.mark.parametrize("name", SOURCES)
def test_same_zone_bounds_match_independent_interval_counts(resolution, name):
    crs, (a, _, c, _, e, f) = SOURCES[name]
    bounds = coarse.admit(utm_grid(resolution), [SOURCES[name]])
    assert bounds == EXPECTED[resolution][name]
    first = coarse.CHAINS[resolution][0][1]
    columns = max(len(overlapping(c, a, ORIGIN[0] + j * first, ORIGIN[0] + (j + 1) * first)) for j in range(-3, 4))
    rows = max(len(overlapping(f, e, ORIGIN[1] - (k + 1) * first, ORIGIN[1] - k * first)) for k in range(-3, 4))
    assert bounds[0] == (columns + 2) * (rows + 2)
    for (src, dst), bound in zip(coarse.CHAINS[resolution][1:], bounds[1:]):
        nested = max(len(overlapping(ORIGIN[0], src, ORIGIN[0] + j * dst, ORIGIN[0] + (j + 1) * dst)) for j in range(-2, 3))
        assert nested == dst // src and bound == (nested + 2) ** 2
    assert max(bounds) <= 169


def test_explicit_interval_enumeration_example():
    pixels = overlapping(500003, 10, 400000, 400100)
    assert [[int(a), int(b)] for a, b in pixels] == [[399993 + 10 * i, 400003 + 10 * i] for i in range(11)]
    assert coarse._axis_count(500003, 400000, 100) == 11
    assert coarse._axis_count(Fraction(499980), 400000, 100) == 10


def test_f02_aoi_cells_use_positive_area_and_the_aoi_cell_denominator():
    """F02, oracle only: positive-area cell membership, overlap fractions and the valid-cell
    fraction over AOI cells. Server ``coveringGrid`` membership is FC04 (unverified here)."""
    cell, (xmin, ymin, xmax, ymax) = 1000, (1200, 500, 3000, 2000)
    columns = overlapping(0, cell, xmin, xmax)
    rows = overlapping(0, cell, ymin, ymax)
    fractions = {
        f"{int(x0) // cell},{int(y0) // cell}": (min(x1, xmax) - max(x0, xmin)) * (min(y1, ymax) - max(y0, ymin)) / cell**2
        for x0, x1 in columns for y0, y1 in rows
    }
    assert fractions == {"1,0": Fraction(2, 5), "2,0": Fraction(1, 2), "1,1": Fraction(4, 5), "2,1": 1}
    assert not {"3,0", "3,1", "1,2", "2,2", "0,0"} & set(fractions)  # edge-only contact is not membership
    observations = {"1,0": 0, "2,0": 3, "1,1": 1, "2,1": 2}
    valid = [c for c in fractions if observations[c] > 0]
    assert Fraction(len(valid), len(fractions)) == Fraction(3, 4)  # not len(valid) / len(valid) == 1


@pytest.mark.parametrize(
    "projection, code",
    [
        (None, "missing_metadata"),
        (("EPSG:32612", None), "missing_metadata"),
        (("EPSG:4326", [0.0001, 0, -110.1, 0, -0.0001, 27.5]), "source_crs"),
        (("EPSG:32712", [10, 0, 500000, 0, -10, 3100000]), "source_crs"),
        (("EPSG:32610", [10, 0, 500000, 0, -10, 3100000]), "source_crs"),
        (("EPSG:32612", [10, 1, 500000, 0, -10, 3100000]), "source_affine"),
        (("EPSG:32612", [20, 0, 500000, 0, -20, 3100000]), "source_pixel"),
        (("EPSG:32612", [10, 0, 500000, 0, 10, 3100000]), "source_orientation"),
        (("EPSG:32613", [10, 0, 200000, 0, -10, 3100000]), coarse.NO_PROOF),
        (("EPSG:32612", [10, 0, float("nan"), 0, -10, 0]), "missing_metadata"),
    ],
)
def test_same_zone_target_rejections(projection, code):
    with pytest.raises(coarse.CoarseError) as err:
        coarse.admit(utm_grid(1000), None if projection is None else [projection])
    assert err.value.code == code


def test_valid_nested_stage_does_not_cure_an_inadmissible_first_stage():
    nested = coarse.admit(utm_grid(1000), [SOURCES["aligned"]])[1]
    assert nested == 144
    with pytest.raises(coarse.CoarseError) as err:  # zone 17 is outside the reviewed EPSG:32611-32616
        coarse.admit(coarse.grid(1000), [("EPSG:32617", [10, 0, 500000, 0, -10, 3100000])])
    assert err.value.code == "source_crs"
    with pytest.raises(coarse.CoarseError) as err:  # a UTM target still refuses another zone's source
        coarse.admit(utm_grid(1000), [("EPSG:32613", [10, 0, 200000, 0, -10, 3100000])])
    assert err.value.code == coarse.NO_PROOF


def test_stage_graph_uses_declared_bounds_and_never_best_effort(real_ee):
    """Actual production staging: each stage is reduceResolution(mean, bestEffort=False,
    maxPixels=bound) followed by reproject to the nested stage grid of the target CRS."""
    import ee

    g = utm_grid(5000)
    bounds = coarse.admit(g, [SOURCES["translated"]])
    names = ["S_B4", "W"]
    source = ee.Image.constant([1, 1]).rename(names)
    image = coarse._staged(source, g, bounds, names)
    graph = Graph(image)
    calls = list(graph.calls("Image.reduceResolution"))
    assert sorted(graph.constant(c["maxPixels"]) for c in calls) == sorted(bounds)
    assert all(graph.constant(c["bestEffort"]) is False for c in calls)
    reducers = [graph.resolve(c["reducer"])["functionInvocationValue"]["functionName"] for c in calls]
    assert reducers == ["Reducer.mean"] * 3
    transforms = sorted(graph.constant(p["transform"])[0] for p in graph.calls("Projection"))
    assert {100, 1000, 5000} <= set(transforms)
