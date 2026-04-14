"""Earth Engine authentication and initialization helpers.

Provides a thin wrapper around ``ee.Authenticate()`` and ``ee.Initialize()``
for notebook and script usage. Does not build credential management systems.
"""

from __future__ import annotations

import ee

from .exceptions import GeeComposerError


def initialize(
    project: str | None = None,
    authenticate: bool = False,
) -> None:
    """Initialize Earth Engine for geecomposer usage.

    Parameters
    ----------
    project:
        Google Cloud project ID to pass to ``ee.Initialize()``.  If ``None``,
        ``ee.Initialize()`` is called without a project argument.
    authenticate:
        If ``True``, call ``ee.Authenticate()`` before initialization.
        Useful for first-time or interactive notebook use.

    Raises
    ------
    GeeComposerError
        If Earth Engine initialization fails.
    """
    try:
        if authenticate:
            ee.Authenticate()
        if project is not None:
            ee.Initialize(project=project)
        else:
            ee.Initialize()
    except Exception as exc:
        raise GeeComposerError(
            f"Earth Engine initialization failed: {exc}"
        ) from exc
