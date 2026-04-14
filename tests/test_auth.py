"""Tests for Earth Engine initialization helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

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
    def test_initialization_failure_raises_package_error(self, mock_ee: MagicMock) -> None:
        from geecomposer.auth import initialize

        mock_ee.Initialize.side_effect = Exception("network error")

        with pytest.raises(GeeComposerError, match="Earth Engine initialization failed"):
            initialize()

    @patch("geecomposer.auth.ee")
    def test_authenticate_failure_raises_package_error(self, mock_ee: MagicMock) -> None:
        from geecomposer.auth import initialize

        mock_ee.Authenticate.side_effect = Exception("auth failed")

        with pytest.raises(GeeComposerError, match="Earth Engine initialization failed"):
            initialize(authenticate=True)
