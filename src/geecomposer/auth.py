"""Earth Engine authentication and initialization helpers.

Provides a thin wrapper around ``ee.Authenticate()`` and ``ee.Initialize()``
for notebook and script usage. Does not build credential management systems.
"""

from __future__ import annotations

import ee
from ee.ee_exception import EEException

from .exceptions import GeeComposerError

_UNSET = object()


def initialize(
    project: str | None = None,
    authenticate: bool = False,
    *,
    credentials=_UNSET,
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

    credentials:
        Optional explicit credential object, forwarded unchanged. Supplying it
        excludes authenticate=True. Explicit None is rejected because EE would
        retain previously installed credentials. Omission retains EE legacy discovery; this
        helper does not verify institutional identity or refresh credentials.

    Raises
    ------
    GeeComposerError
        If Earth Engine initialization fails.
    """
    if credentials is not _UNSET and authenticate:
        raise GeeComposerError(
            "explicit credentials cannot be combined with authenticate=True"
        )
    if credentials is None:
        raise GeeComposerError("explicit credentials cannot be None")
    if isinstance(credentials, str):
        raise GeeComposerError(
            "credentials must be an explicit object, not a discovery string"
        )
    try:
        if authenticate:
            ee.Authenticate()
        options = {}
        if project is not None:
            options["project"] = project
        if credentials is not _UNSET:
            options["credentials"] = credentials
        ee.Initialize(**options)
    except Exception as exc:
        category = {
            EEException: "EEException",
            ValueError: "ValueError",
            TypeError: "TypeError",
        }.get(type(exc), "unknown")
        raise GeeComposerError(
            f"Earth Engine initialization failed ({category}); "
            "check supplied credentials, project access and EE registration"
        ) from None
