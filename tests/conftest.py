"""Test configuration for local package imports."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


import pytest  # noqa: E402


@pytest.fixture
def runner_offline(monkeypatch):
    """Runner tests never discover credentials or access any network, standalone too."""
    import socket
    import google.auth
    import ee

    def deny(*args, **kwargs):
        pytest.fail("runner test attempted live credentials or network")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(google.auth, "default", deny)
    monkeypatch.setattr(ee, "Authenticate", deny)
    monkeypatch.setattr(ee.data, "get_persistent_credentials", deny)


@pytest.fixture
def real_ee(monkeypatch):
    """Use the installed, development-locked EE algorithm fixture without I/O."""
    import ee
    from ee import apitestcase

    ee.Reset()
    monkeypatch.setattr(ee.data, "_install_cloud_api_resource", lambda: None)
    monkeypatch.setattr(ee.data, "getAlgorithms", apitestcase.GetAlgorithms)
    monkeypatch.setattr(
        ee.data, "computeValue", lambda *_: pytest.fail("unexpected evaluation")
    )
    monkeypatch.setattr(ee.deprecation, "_FetchDataCatalogStac", lambda: {})
    ee.Initialize(None, "", project="offline-tests")
    yield
    ee.Reset()
