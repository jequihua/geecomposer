"""Grouped composition helpers.

``compose_yearly`` groups the existing composition by calendar year. Named
periods (``monthly_periods``/``validate_periods``) are the shared half-open UTC
period core used by ``geecomposer.coarse``; they build no Earth Engine objects.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Iterable

import ee

from .compose import compose
from .exceptions import GeeComposerError


def compose_yearly(
    years: list[int] | range | Iterable[int],
    **compose_kwargs: Any,
) -> dict[int, ee.Image]:
    """Compose one image per year by delegating to ``compose()``.

    For each year in *years*, calls ``compose()`` with ``start`` and ``end``
    set to that calendar year (``{year}-01-01`` to ``{year+1}-01-01``).  All
    other keyword arguments are forwarded to ``compose()`` unchanged.

    Parameters
    ----------
    years:
        An iterable of integer years (e.g. ``[2020, 2021, 2022]``,
        ``range(2020, 2025)``, or a generator).  The iterable is consumed
        once and normalized to a list internally.
    **compose_kwargs:
        All keyword arguments accepted by ``compose()``, except ``start``
        and ``end`` which are set automatically per year.  Passing ``start``
        or ``end`` (even as ``None``) raises ``GeeComposerError`` to prevent
        ambiguity.

    Returns
    -------
    dict[int, ee.Image]
        A mapping from year to the composed ``ee.Image`` for that year.

    Raises
    ------
    GeeComposerError
        If *years* is empty or contains non-integer values, or if ``start``
        or ``end`` are passed in *compose_kwargs*.
    """
    year_list = _validate_and_normalize_years(years, compose_kwargs)

    results: dict[int, ee.Image] = {}
    for year in year_list:
        start = f"{year:04d}-01-01"
        end = f"{year + 1:04d}-01-01"
        results[year] = compose(start=start, end=end, **compose_kwargs)

    return results


def _validate_and_normalize_years(
    years: Any,
    compose_kwargs: dict,
) -> list[int]:
    """Validate and normalize *years* to a list of integers.

    Returns the normalized list so the caller iterates the same object that
    was validated (avoiding generator-exhaustion bugs).
    """
    if "start" in compose_kwargs or "end" in compose_kwargs:
        raise GeeComposerError(
            "Do not pass 'start' or 'end' to compose_yearly(); "
            "dates are derived from the years list."
        )

    if not hasattr(years, "__iter__"):
        raise GeeComposerError(
            f"years must be an iterable of integers, got {type(years).__name__}."
        )

    year_list = list(years)
    if len(year_list) == 0:
        raise GeeComposerError("years must not be empty.")

    for i, y in enumerate(year_list):
        if isinstance(y, bool) or not isinstance(y, int):
            raise GeeComposerError(
                f"years[{i}] must be an integer, got {type(y).__name__}."
            )

    if any(y < 1 or y > 9998 for y in year_list):
        raise GeeComposerError("years must be between 1 and 9998")
    if len(set(year_list)) != len(year_list):
        raise GeeComposerError("years must not contain duplicates")
    return year_list


PERIOD_CEILING = 12
_LABEL = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,31}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def monthly_periods(year: int) -> tuple[tuple[str, str, str], ...]:
    """Twelve named UTC half-open months ``(label, start, end)``; the last ends on
    1 January of the next year. Labels are ``YYYY-MM`` in chronological order."""
    if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9998:
        raise GeeComposerError("year must be an integer between 1 and 9998")
    months = []
    for month in range(1, 13):
        start = date(year, month, 1)
        end = date(year + month // 12, month % 12 + 1, 1)
        months.append((f"{year:04d}-{month:02d}", start.isoformat(), end.isoformat()))
    return tuple(months)


def validate_periods(periods: Iterable[Any]) -> tuple[tuple[str, str, str], ...]:
    """Validate named periods and return them as an immutable tuple.

    Each period is ``(label, start, end)`` with ISO ``YYYY-MM-DD`` dates meaning
    00:00 UTC and the half-open interval ``[start, end)``. Periods must be unique,
    strictly increasing and non-overlapping (gaps allowed), 1..12 per call.
    Failures raise ``GeeComposerError`` naming one code: ``empty_list``,
    ``too_many``, ``bad_label``, ``invalid_date``, ``empty_interval``,
    ``duplicate_label``, ``unsorted`` or ``overlap``.
    """
    if isinstance(periods, (str, bytes)) or not hasattr(periods, "__iter__"):
        raise GeeComposerError("invalid periods: empty_list")
    items = list(periods)
    if not items:
        raise GeeComposerError("invalid periods: empty_list")
    if len(items) > PERIOD_CEILING:
        raise GeeComposerError("invalid periods: too_many")
    result, labels, previous = [], set(), None
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise GeeComposerError("invalid periods: bad_label")
        label, start, end = item
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise GeeComposerError("invalid periods: bad_label")
        first, last = _iso_date(start), _iso_date(end)
        if first is None or last is None:
            raise GeeComposerError("invalid periods: invalid_date")
        if first >= last:
            raise GeeComposerError("invalid periods: empty_interval")
        if label in labels:
            raise GeeComposerError("invalid periods: duplicate_label")
        if previous and first < previous[0]:
            raise GeeComposerError("invalid periods: unsorted")
        if previous and first < previous[1]:
            raise GeeComposerError("invalid periods: overlap")
        labels.add(label)
        previous = (first, last)
        result.append((label, start, end))
    return tuple(result)


def _iso_date(value):
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
