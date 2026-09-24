"""Tests for Earth Engine initialization helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from geecomposer.exceptions import GeeComposerError


class TestInitialize:
    @patch("geecomposer.auth.ee")
    def test_default_initializes_without_project(self, mock_ee: MagicMock) -> None:
        from geecomposer.auth import initialize

        initialize()

        mock_ee.Authenticate.assert_not_called()
        mock_ee.Initialize.assert_called_once_with()

    @patch("geecomposer.auth.ee")
    def test_project_passed_through(self, mock_ee: MagicMock) -> None:
        from geecomposer.auth import initialize

        initialize(project="my-ee-project")

        mock_ee.Initialize.assert_called_once_with(project="my-ee-project")

    @patch("geecomposer.auth.ee")
    def test_authenticate_called_before_initialize(self, mock_ee: MagicMock) -> None:
        from geecomposer.auth import initialize

        call_order = []
        mock_ee.Authenticate.side_effect = lambda: call_order.append("auth")
        mock_ee.Initialize.side_effect = lambda **kw: call_order.append("init")

        initialize(authenticate=True)

        assert call_order == ["auth", "init"]
        mock_ee.Authenticate.assert_called_once()
        mock_ee.Initialize.assert_called_once()

    @patch("geecomposer.auth.ee")
    def test_authenticate_with_project(self, mock_ee: MagicMock) -> None:
        from geecomposer.auth import initialize

        initialize(project="my-project", authenticate=True)

        mock_ee.Authenticate.assert_called_once()
        mock_ee.Initialize.assert_called_once_with(project="my-project")

    @patch("geecomposer.auth.ee")
    def test_initialization_failure_raises_package_error(
        self, mock_ee: MagicMock
    ) -> None:
        from geecomposer.auth import initialize

        mock_ee.Initialize.side_effect = Exception("network error")

        with pytest.raises(
            GeeComposerError, match="Earth Engine initialization failed"
        ):
            initialize()

    @patch("geecomposer.auth.ee")
    def test_authenticate_failure_raises_package_error(
        self, mock_ee: MagicMock
    ) -> None:
        from geecomposer.auth import initialize

        mock_ee.Authenticate.side_effect = Exception("auth failed")

        with pytest.raises(
            GeeComposerError, match="Earth Engine initialization failed"
        ):
            initialize(authenticate=True)


@pytest.mark.parametrize("credential", [object()])
@pytest.mark.parametrize("failure", [False, True])
def test_explicit_credentials_unchanged_no_fallback(monkeypatch, credential, failure):
    import ee
    import google.auth
    import traceback
    from geecomposer import initialize

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected credential discovery/authentication")

    monkeypatch.setattr(ee, "Authenticate", forbidden)
    monkeypatch.setattr(ee.data, "get_persistent_credentials", forbidden)
    monkeypatch.setattr(google.auth, "default", forbidden)
    calls = []

    def init(**kwargs):
        calls.append(kwargs)
        if failure:
            raise RuntimeError("PRIVATE_CREDENTIAL")

    monkeypatch.setattr(ee, "Initialize", init)
    if failure:
        with pytest.raises(GeeComposerError) as error:
            initialize("explicit-project", credentials=credential)
        assert "PRIVATE_CREDENTIAL" not in "".join(
            traceback.format_exception(error.value)
        )
    else:
        initialize("explicit-project", credentials=credential)
    assert len(calls) == 1
    assert calls[0]["credentials"] is credential
    assert calls[0]["project"] == "explicit-project"


@pytest.mark.parametrize("credential", [None, object(), "persistent"])
def test_explicit_auth_conflict_before_ee(monkeypatch, credential):
    from geecomposer import auth

    mock = MagicMock()
    monkeypatch.setattr(auth, "ee", mock)
    with pytest.raises(GeeComposerError):
        auth.initialize(authenticate=True, credentials=credential)
    assert mock.mock_calls == []


def test_discovery_string_rejected(monkeypatch):
    from geecomposer import auth

    mock = MagicMock()
    monkeypatch.setattr(auth, "ee", mock)
    with pytest.raises(GeeComposerError):
        auth.initialize(credentials="persistent")
    assert mock.mock_calls == []


def test_none_rejected_preserves_real_ee_identity(real_ee, monkeypatch):
    import ee
    import google.auth
    from geecomposer import initialize

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected discovery or refresh")

    class FakeCredential:
        quota_project_id = None
        refresh = forbidden

    monkeypatch.setattr(ee, "Authenticate", forbidden)
    monkeypatch.setattr(ee.data, "get_persistent_credentials", forbidden)
    monkeypatch.setattr(google.auth, "default", forbidden)
    prior, replacement = FakeCredential(), FakeCredential()
    # Both calls execute the installed Initialize/data.initialize code. The
    # fixture substitutes only cloud resource installation and offline metadata.
    ee.Initialize(credentials=prior, project="offline-tests")
    ee.Initialize(credentials=None, project="offline-tests")
    assert ee.data._get_state().credentials is prior
    with patch.object(ee, "Initialize", wraps=ee.Initialize) as native:
        initialize_spy = MagicMock(wraps=ee.data.initialize)
        monkeypatch.setattr(ee.data, "initialize", initialize_spy)
        with pytest.raises(
            GeeComposerError, match="explicit credentials cannot be None"
        ):
            initialize(project="offline-tests", credentials=None)
        native.assert_not_called()
        initialize_spy.assert_not_called()
        assert ee.data._get_state().credentials is prior
        initialize(project="offline-tests", credentials=replacement)
        native.assert_called_once_with(project="offline-tests", credentials=replacement)
        assert initialize_spy.call_args.kwargs["credentials"] is replacement
        assert ee.data._get_state().credentials is replacement
    # real_ee resets the EE state in fixture teardown.


@pytest.mark.parametrize("stage", ["Authenticate", "Initialize"])
@pytest.mark.parametrize("kind", ["EEException", "ValueError", "TypeError", "unknown"])
def test_auth_error_categories(monkeypatch, stage, kind):
    import ee
    import traceback
    from geecomposer import initialize

    error_type = {
        "EEException": ee.EEException,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "unknown": type("PRIVATE_CLASS", (Exception,), {}),
    }[kind]
    monkeypatch.setattr(
        ee, stage, MagicMock(side_effect=error_type("PRIVATE_CREDENTIAL"))
    )
    with pytest.raises(GeeComposerError) as error:
        initialize(authenticate=stage == "Authenticate")
    assert str(error.value) == (
        f"Earth Engine initialization failed ({kind}); "
        "check supplied credentials, project access and EE registration"
    )
    visible = "".join(traceback.format_exception(error.value))
    assert "PRIVATE_CREDENTIAL" not in visible
    assert "PRIVATE_CLASS" not in visible
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None
