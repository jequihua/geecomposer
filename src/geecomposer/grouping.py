"""Grouped composition helpers.

Provides yearly grouping only in v0.1. Monthly, seasonal, and generic
grouping are deferred to later versions.
"""

from __future__ import annotations

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
        start = f"{year}-01-01"
        end = f"{year + 1}-01-01"
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
        if not isinstance(y, int):
            raise GeeComposerError(
                f"years[{i}] must be an integer, got {type(y).__name__}."
            )

    return year_list
