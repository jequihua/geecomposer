"""Portable offline package gate; no dependency installation or live credentials."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV_TESTS = ("test_live_diagnostics.py", "test_verification_gate.py")
GUARD = """
import socket
import google.auth
import ee
def deny(*args, **kwargs):
    raise RuntimeError("offline verification forbids network and credentials")
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
google.auth.default = deny
ee.Authenticate = deny
ee.data.get_persistent_credentials = deny
"""


def run(argv, *, cwd, env):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(
            f"offline verification stage failed: exit {result.returncode}; raw diagnostics withheld"
        )
    return result.stdout


def environment(scratch):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GOOGLE_", "GCLOUD_", "CLOUDSDK_", "EARTHENGINE_"))
    }
    for key in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "CLOUDSDK_CONFIG",
        "TEMP",
        "TMP",
    ):
        env[key] = str(scratch)
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PIP_NO_INDEX="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONPATH="",
    )
    return env


def verify(root, scratch):
    env = environment(scratch)
    for name in ("deploy/cloud_run/job.yaml", "examples/rao_gcs_unstarted.py"):
        if not (root / name).is_file():
            raise RuntimeError(f"missing public asset: {name}")
    print("Public package tests (two development-only modules excluded):", flush=True)
    tests = (
        GUARD
        + """
import sys
from pathlib import Path
import pytest
root, scratch = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src"))
import geecomposer
assert Path(geecomposer.__file__).resolve().is_relative_to(root / "src")
class NoSkips:
    skipped = False
    def pytest_runtest_logreport(self, report):
        self.skipped |= report.skipped
    def pytest_collectreport(self, report):
        self.skipped |= report.skipped
plugin = NoSkips()
result = pytest.main(["tests", "-q", "-p", "no:cacheprovider",
    "--ignore=tests/test_live_diagnostics.py", "--ignore=tests/test_verification_gate.py",
    "--basetemp", str(scratch / "pytest")], plugins=[plugin])
for name, module in tuple(sys.modules.items()):
    if name == "geecomposer" or name.startswith("geecomposer."):
        assert Path(module.__file__).resolve().is_relative_to(root / "src")
raise SystemExit(result or int(plugin.skipped))
"""
    )
    print(
        run([sys.executable, "-I", "-B", "-c", tests, str(root), str(scratch)], cwd=root, env=env),
        flush=True,
    )
    print(
        run(
            [
                sys.executable,
                "-I",
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--target-version",
                "py311",
                "--select",
                "E4,E7,E9,F",
                "--no-cache",
                "src",
                "tests",
                "examples",
                "tools",
            ],
            cwd=root,
            env=env,
        ),
        flush=True,
    )
    build = scratch / "wheel-source"
    build.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(root / name, build / name)
    shutil.copytree(
        root / "src", build / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info")
    )
    run(
        [sys.executable, "-I", "-m", "build", "--wheel", "--no-isolation", str(build)],
        cwd=scratch,
        env=env,
    )
    (wheel,) = (build / "dist").glob("*.whl")
    installed = scratch / "installed"
    run(
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=scratch,
        env=env,
    )
    smoke = (
        GUARD
        + """
import contextlib
import io
import json
import runpy
import sys
from pathlib import Path
installed, root = map(Path, sys.argv[1:])
sys.path.insert(0, str(installed))
import geecomposer
assert Path(geecomposer.__file__).resolve().is_relative_to(installed)
assert not ee.data.is_initialized()
entry = installed / "bin" / ("geecomposer.exe" if sys.platform == "win32" else "geecomposer")
assert entry.is_file()
recipes = sorted((root / "examples/recipes").glob("*.json"))
recipes = [p for p in recipes if p.name != "policy.local.example.json"]
assert len(recipes) == 6
for args in [["--help"]] + [args for p in recipes for args in
        (["validate", str(p)], ["run", "--recipe", str(p), "--dry-run"])]:
    sys.argv = [str(entry), *args]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            runpy.run_path(str(entry), run_name="__main__")
        except SystemExit as exc:
            assert exc.code in (None, 0)
    if args != ["--help"]:
        value = json.loads(output.getvalue())
        assert value["status"] == ("validated" if args[0] == "validate" else "dry_run")
for name, module in tuple(sys.modules.items()):
    if name == "geecomposer" or name.startswith("geecomposer."):
        assert Path(module.__file__).resolve().is_relative_to(installed)
assert not ee.data.is_initialized()
print("Installed wheel origins and CLI: help + 12 recipe checks passed")
"""
    )
    print(
        run(
            [sys.executable, "-I", "-B", "-c", smoke, str(installed), str(root)],
            cwd=scratch,
            env=env,
        ),
        flush=True,
    )


def main():
    with tempfile.TemporaryDirectory(prefix="geecomposer-offline-") as name:
        scratch = Path(name).resolve()
        if scratch.is_relative_to(ROOT):
            raise RuntimeError("verification scratch must be outside the package")
        verify(ROOT, scratch)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
