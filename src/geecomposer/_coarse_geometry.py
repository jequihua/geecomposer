"""National MX-AEA admission geometry for ``geecomposer.coarse`` (internal).

Pure-Python closed forms on WGS 84 for:

* the fixed MX-AEA Albers projection (forward map, and exact geodetic extremes of
  an MX-AEA rectangle from Albers polar geometry);
* UTM inverse coordinates from the sixth-order Krueger series (Karney 2011), used
  only to cover a source's native footprint with an explicit margin.

The reasoning behind each margin is in the S02 ``activation205/admission.md``
note. Comparisons apply ``ANGLE_EPS`` against admission, so floating rounding can
only make a check stricter. Nothing here touches Earth Engine.
"""

from __future__ import annotations

from functools import lru_cache
import math

import numpy as np
import shapely
from shapely.geometry import box

A = 6378137.0
F = 1 / 298.257223563
E2 = F * (2 - F)
E = math.sqrt(E2)
K0 = 0.9996

# MX-AEA (contract section 3): origin latitude, central meridian, standard parallels
LAT0, LON0, LAT1, LAT2 = 12.0, -102.0, 17.5, 29.5
FALSE_EASTING = 2500000.0

# Accepted G1 domain (D129): latitude, longitude and the whole-cell zone offset
LAT_RANGE = (13.5, 34.0)
LON_RANGE = (-119.5, -85.5)
ZONE_OFFSET = 5.0
ANGLE_EPS = 1e-6  # degrees; double-precision allowance, always against admission

CHECK_HALO_M = 1000.0  # >= clip margin + first-stage cell diagonal (200 + 100*sqrt 2)
CLIP_MARGIN_M = 200.0  # >= native 10 m pixel extent in MX-AEA (<= 14.4 m inside D)
FOOTPRINT_STEP_M = 500.0  # footprint boundary sample spacing
FOOTPRINT_MARGIN_M = 1000.0  # >= 2.45 * sqrt 2 * 250 m (+ series/rounding)
FOOTPRINT_BAND = (0.1, 49.9, 9.9)  # sample latitude range and |omega| where the margin holds
AOI_STEP_DEG = 0.01  # AOI edge densification in longitude/latitude
AOI_MARGIN_M = 1000.0  # >= 0.005 deg chord deviation (~0.6 km)


def _q(phi):
    s = math.sin(phi)
    return (1 - E2) * (s / (1 - E2 * s * s) + math.atanh(E * s) / E)


def _m(phi):
    s = math.sin(phi)
    return math.cos(phi) / math.sqrt(1 - E2 * s * s)


_M1, _M2 = _m(math.radians(LAT1)), _m(math.radians(LAT2))
_Q1, _Q2 = _q(math.radians(LAT1)), _q(math.radians(LAT2))
N = (_M1 * _M1 - _M2 * _M2) / (_Q2 - _Q1)
C = _M1 * _M1 + N * _Q1
RHO0 = A * math.sqrt(C - N * _q(math.radians(LAT0))) / N


def aea_forward(lon, lat):
    """MX-AEA (x, y) metres of a geodetic point (degrees); closed form (Snyder 1987)."""
    rho = A * math.sqrt(C - N * _q(math.radians(lat))) / N
    theta = N * math.radians(lon - LON0)
    return FALSE_EASTING + rho * math.sin(theta), RHO0 - rho * math.cos(theta)


def _lat_from_rho(rho):
    """Latitude (degrees) on the Albers circle of radius rho: q is increasing in phi,
    so bisection on q(phi) = (C - (rho n/a)^2)/n converges to double precision."""
    target = (C - (rho * N / A) ** 2) / N
    lo, hi = -math.pi / 2 + 1e-12, math.pi / 2 - 1e-12
    for _ in range(200):
        mid = (lo + hi) / 2
        if _q(mid) < target:
            lo = mid
        else:
            hi = mid
    return math.degrees((lo + hi) / 2)


def rectangle_extremes(x0, y0, x1, y1):
    """Exact geodetic extremes (lat_min, lat_max, lon_min, lon_max) of every point of an
    MX-AEA rectangle, curved edges included.

    In the Albers plane, meridians are rays from the apex (FE, RHO0) and parallels are
    circles about it, with latitude decreasing as the apex distance rho grows. So:
    longitude is extreme at the extreme polar angles, attained at vertices (the apex is
    outside the rectangle); latitude is minimal at the farthest vertex and maximal at
    the point nearest the apex, which may lie inside an edge."""
    px, py = FALSE_EASTING, RHO0
    if x0 <= px <= x1 and y0 <= py <= y1:
        raise ValueError("rectangle contains the Albers apex")
    corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    rho_max = max(math.hypot(x - px, y - py) for x, y in corners)
    rho_min = math.hypot(max(x0 - px, 0.0, px - x1), max(y0 - py, 0.0, py - y1))
    thetas = [math.atan2(x - px, py - y) for x, y in corners]
    lons = [LON0 + math.degrees(t / N) for t in thetas]
    return _lat_from_rho(rho_max), _lat_from_rho(rho_min), min(lons), max(lons)


def domain_failure(extremes, zone):
    """None if every point satisfies the G1 domain and the zone offset, else a reason."""
    lat_min, lat_max, lon_min, lon_max = extremes
    if lat_min - ANGLE_EPS < LAT_RANGE[0] or lat_max + ANGLE_EPS > LAT_RANGE[1] or \
            lon_min - ANGLE_EPS < LON_RANGE[0] or lon_max + ANGLE_EPS > LON_RANGE[1]:
        return "national_domain"
    if zone is not None:
        centre = -183.0 + 6 * zone
        if max(abs(lon_min - centre), abs(lon_max - centre)) + ANGLE_EPS > ZONE_OFFSET:
            return "zone_offset"
    return None


# ------------------------------------------------------------------ UTM inverse (Krueger)

_n = F / (2 - F)
_AA = A / (1 + _n) * (1 + _n ** 2 / 4 + _n ** 4 / 64 + _n ** 6 / 256)
_BETA = (
    _n / 2 - 2 * _n ** 2 / 3 + 37 * _n ** 3 / 96 - _n ** 4 / 360 - 81 * _n ** 5 / 512 + 96199 * _n ** 6 / 604800,
    _n ** 2 / 48 + _n ** 3 / 15 - 437 * _n ** 4 / 1440 + 46 * _n ** 5 / 105 - 1118711 * _n ** 6 / 3870720,
    17 * _n ** 3 / 480 - 37 * _n ** 4 / 840 - 209 * _n ** 5 / 4480 + 5569 * _n ** 6 / 90720,
    4397 * _n ** 4 / 161280 - 11 * _n ** 5 / 504 - 830251 * _n ** 6 / 7257600,
    4583 * _n ** 5 / 161280 - 108847 * _n ** 6 / 3991680,
    20648693 * _n ** 6 / 638668800,
)


def utm_inverse(zone, easting, northing):
    """Geodetic (lon, lat) degrees of a northern-hemisphere UTM point (Karney 2011,
    sixth-order series; published position error below 5 nm near the zone)."""
    xi = northing / (K0 * _AA)
    eta = (easting - 500000.0) / (K0 * _AA)
    xip, etap = xi, eta
    for j, beta in enumerate(_BETA, start=1):
        xip -= beta * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        etap -= beta * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    taup = math.sin(xip) / math.hypot(math.sinh(etap), math.cos(xip))
    lam = math.atan2(math.sinh(etap), math.cos(xip))
    tau = taup
    for _ in range(8):  # Newton for tau from the conformal tau' (Karney 2011, eqs 19-21)
        tau1 = math.hypot(1.0, tau)
        sig = math.sinh(E * math.atanh(E * tau / tau1))
        taupa = math.hypot(1.0, sig) * tau - sig * tau1
        tau += (taup - taupa) / math.hypot(1.0, taupa) * (1 + (1 - E2) * tau * tau) / ((1 - E2) * tau1)
    return -183.0 + 6 * zone + math.degrees(lam), math.degrees(math.atan(tau))


@lru_cache(maxsize=4096)
def footprint_cover(zone, c, f, width, height):
    """MX-AEA rectangle containing the image of the native UTM footprint
    [c, c + 10 width] x [f - 10 height, f], or a failure reason.

    Boundary samples every <= 500 m are mapped exactly enough (series error < 5 nm);
    between samples the image moves <= 2.45 * sqrt 2 * 250 m < 1 km while the samples
    stay in the band where that Lipschitz bound holds (admission.md section 3)."""
    x_lo, x_hi, y_lo, y_hi = c, c + 10.0 * width, f - 10.0 * height, f
    points = []
    for (ax, ay), (bx, by) in (((x_lo, y_lo), (x_hi, y_lo)), ((x_hi, y_lo), (x_hi, y_hi)),
                               ((x_hi, y_hi), (x_lo, y_hi)), ((x_lo, y_hi), (x_lo, y_lo))):
        steps = max(1, math.ceil(math.hypot(bx - ax, by - ay) / FOOTPRINT_STEP_M))
        points.extend((ax + (bx - ax) * i / steps, ay + (by - ay) * i / steps) for i in range(steps))
    lat_min, lat_max, omega_max = FOOTPRINT_BAND
    xs, ys = [], []
    for easting, northing in points:
        lon, lat = utm_inverse(zone, easting, northing)
        if not lat_min <= lat <= lat_max or abs(lon - (-183.0 + 6 * zone)) > omega_max:
            return None, "source_extent"
        x, y = aea_forward(lon, lat)
        xs.append(x)
        ys.append(y)
    m = FOOTPRINT_MARGIN_M
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m), None


def aoi_region(geometry):
    """MX-AEA region containing the AOI (longitude/latitude, straight edges): edges are
    densified to <= 0.01 degree, mapped by the Albers forward map and buffered by 1 km."""
    dense = shapely.segmentize(geometry, AOI_STEP_DEG)

    def to_aea(coords):
        return [aea_forward(lon, lat) for lon, lat in coords]

    mapped = shapely.transform(dense, lambda xy: np.array(to_aea(xy)))
    return mapped.buffer(AOI_MARGIN_M)


def aligned(bounds, size, origin_x, origin_y):
    """Outward alignment of (x0, y0, x1, y1) to the coarse grid of cell ``size``."""
    x0, y0, x1, y1 = bounds
    return (origin_x + math.floor((x0 - origin_x) / size) * size,
            origin_y + math.floor((y0 - origin_y) / size) * size,
            origin_x + math.ceil((x1 - origin_x) / size) * size,
            origin_y + math.ceil((y1 - origin_y) / size) * size)


def expanded(rect, margin):
    x0, y0, x1, y1 = rect
    return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)


def intersection_bounds(region, rect):
    """Bounds of region intersected with an axis-aligned rectangle, or None if empty."""
    part = region.intersection(box(*rect))
    return None if part.is_empty else part.bounds
