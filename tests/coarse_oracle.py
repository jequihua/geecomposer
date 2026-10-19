"""Independent exact arithmetic for the adopted M005-S01 contract (tests only).

A deliberately small Fraction model of the contract's equations: the section 6
value/mask construction, equal-observation temporal statistics, the equal-pixel
Rao formula and exact interval counts. It is not an Earth Engine emulator and
says nothing about server masks, projections or reducers; graph tests check that
production builds these operations, this module supplies the expected numbers.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import combinations
import math


def q(value):
    if value is None:
        return None
    if value in ("nan", "inf", "-inf"):
        return float(value)
    return Fraction(value)


def finite(value):
    return value is not None and not (isinstance(value, float) and not math.isfinite(value))


def construct(values, masks):
    """Per-pixel ((numerator, mask), (weight, mask)) in the section 6 order."""
    out = []
    for x, mu in zip(values, masks):
        x, mu = q(x), q(mu)
        if mu is None or mu == 0:
            out.append(((Fraction(0), 1), (Fraction(0), 1)))
            continue
        flag = 1 if finite(x) else 0
        weight = mu * flag
        value = (x if flag else Fraction(0)) * weight
        out.append(((value if weight else Fraction(0), 1), (weight, 1)))
    return out


def spatial(values, masks, weights, theta):
    """W = sum(a*w), S = sum(a*n); mean only when W >= theta (final-only threshold)."""
    pixels = construct(values, masks)
    big_w = sum(Fraction(a) * w for a, (_, (w, _)) in zip(weights, pixels))
    big_s = sum(Fraction(a) * n for a, ((n, _), _) in zip(weights, pixels))
    return big_w, big_s, (big_s / big_w if big_w > 0 and big_w >= theta else None)


def temporal(values):
    v = sorted(values)
    n = len(v)
    if not n:
        return dict(count=0, mean=None, median=None, var_pop=None, min=None, max=None, range=None)
    mean = sum(v) / n
    median = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return dict(count=n, mean=mean, median=median, var_pop=sum((x - mean) ** 2 for x in v) / n,
                min=v[0], max=v[-1], range=v[-1] - v[0])


def exact_sqrt(value):
    n, d = math.isqrt(value.numerator), math.isqrt(value.denominator)
    return Fraction(n, d) if n * n == value.numerator and d * d == value.denominator else math.sqrt(value)


def rao(window, model=None):
    """(2/m^2) sum_{a<b} d / D over a complete window; None if any cell is missing."""
    if any(cell is None for cell in window):
        return None
    vectors = [[q(x) for x in cell] for cell in window]
    bound = 1
    if model:
        z = [[(x - q(mu)) / q(sc) for x, mu, sc in zip(v, model["means"], model["scales"])] for v in vectors]
        vectors = [[sum(q(w) * zi for w, zi in zip(row, v)) for row in model["loadings"]] for v in z]
        bound = 2 * q(model["n_sigmas"]) * exact_sqrt(Fraction(math.ceil(len(model["means"]) * q(model["retained_fraction"]))))
    total = sum(exact_sqrt(sum((x - y) ** 2 for x, y in zip(a, b))) for a, b in combinations(vectors, 2))
    return 2 * total / len(vectors) ** 2 / bound


def overlapping(origin, step, lo, hi):
    """Source pixels [origin + step*i, origin + step*(i+1)] meeting (lo, hi) with positive length."""
    lo, hi = Fraction(min(lo, hi)), Fraction(max(lo, hi))
    origin, step = Fraction(origin), Fraction(step)
    ends = sorted(((lo - origin) / step, (hi - origin) / step))
    found = []
    for i in range(math.floor(ends[0]) - 2, math.ceil(ends[1]) + 2):
        a, b = sorted((origin + step * i, origin + step * (i + 1)))
        if max(a, lo) < min(b, hi):
            found.append((a, b))
    return found
