"""Bounded checks of the portable gate and public document/asset contract."""

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / "tools/verify_offline.py"
    if not path.exists():
        pytest.skip("public verification tool absent from source/tests-only copy")
    spec = importlib.util.spec_from_file_location("public_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_denies_network_and_credentials(gate):
    code = (
        gate.GUARD
        + """
for call in (lambda: socket.getaddrinfo("localhost", 1),
             lambda: socket.create_connection(("localhost", 1)),
             lambda: socket.socket().connect(("localhost", 1)),
             lambda: socket.socket().connect_ex(("localhost", 1)),
             google.auth.default, ee.Authenticate, ee.data.get_persistent_credentials):
    try:
        call()
    except RuntimeError:
        pass
    else:
        raise AssertionError("guard bypass")
"""
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_environment_removes_credentials(gate, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "private")
    env = gate.environment(tmp_path)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["PYTHONPATH"] == ""
    assert env["PIP_NO_INDEX"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_stage_failure_is_bounded(gate, monkeypatch, tmp_path):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 7, "PRIVATE", "PRIVATE"),
    )
    with pytest.raises(
        RuntimeError, match="^offline verification stage failed: exit 7; raw diagnostics withheld$"
    ):
        gate.run([], cwd=tmp_path, env={})


def test_public_document_links_and_required_assets(gate):
    root = gate.ROOT
    for name in ("deploy/cloud_run/job.yaml", "examples/rao_gcs_unstarted.py"):
        assert (root / name).is_file()
    docs = [root / "README.md", root / "container/README.md"]
    docs += [
        root / "docs" / (name + ".md")
        for name in (
            "rao",
            "runner",
            "qualification",
            "verification",
            "cloud_run_operator",
            "institutional_admin_request",
        )
    ]
    for path in docs:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            parsed = urlsplit(link)
            if parsed.scheme or not parsed.path:
                continue
            target = (path.parent / unquote(parsed.path)).resolve()
            assert target.is_relative_to(root), (path.name, link)
            assert target.exists(), (path.name, link)
