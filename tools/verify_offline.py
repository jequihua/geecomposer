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


# The only permitted skip: this named test, skipped by its own skipif mark on
# Windows, which has no POSIX FIFOs. Everywhere else the suite has zero skips and
# this test must actually pass.
FIFO_TEST = "tests/test_personal_cli.py::test_posix_fifo_records_never_block"
FIFO_REASON = "POSIX FIFOs are unsupported on this platform"

# The exact public example inventory checked by the installed-wheel smoke: twelve
# recipes (the policy example excluded), three personal and the three coarse ones.
INVENTORY = """
recipes = sorted((root / "examples/recipes").glob("*.json"))
recipes = [p for p in recipes if p.name != "policy.local.example.json"]
assert len(recipes) == 12
assert sum(p.name.startswith("personal_") for p in recipes) == 3
assert sorted(p.name for p in recipes if p.name.startswith("coarse_")) == [
    "coarse_rao_monthly_2025.json",
    "coarse_s1_monthly_2025.json",
    "coarse_s2_monthly_2025.json",
]
"""


def fifo_unsupported():
    return os.name == "nt" and not hasattr(os, "mkfifo")


class SkipPolicy:
    """pytest plugin: decide skips from test reports, never from printed output."""

    def __init__(self, windows):
        self.windows = windows
        self.problems = []
        self.collected = False
        self.skipped = False
        self.passed = False

    def pytest_itemcollected(self, item):
        self.collected |= item.nodeid == FIFO_TEST

    def pytest_collectreport(self, report):
        if not report.passed:
            self.problems.append(f"collection {report.outcome}: {report.nodeid}")

    def pytest_runtest_logreport(self, report):
        if report.nodeid == FIFO_TEST and report.when == "call" and report.passed:
            self.passed = True
        if not report.skipped:
            return
        longrepr = report.longrepr
        reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else None
        if (
            self.windows
            and not self.skipped
            and report.nodeid == FIFO_TEST
            and report.when == "setup"
            and not hasattr(report, "wasxfail")
            and reason == "Skipped: " + FIFO_REASON
        ):
            self.skipped = True
        else:
            self.problems.append(f"unexpected skip: {report.nodeid} ({report.when})")

    def verdict(self):
        problems = list(self.problems)
        if not self.collected:
            problems.append(f"required test not collected: {FIFO_TEST}")
        elif self.windows and not self.skipped:
            problems.append(f"expected Windows platform skip not observed: {FIFO_TEST}")
        elif not self.windows and not self.passed:
            problems.append(f"required test did not pass: {FIFO_TEST}")
        return problems

    def summary(self):
        if self.windows:
            return (
                f"Permitted platform skip: {FIFO_TEST} ({FIFO_REASON}); "
                "the real FIFO regression is pending POSIX/Linux execution."
            )
        return f"No skipped tests; {FIFO_TEST} executed and passed."


def run_tests(root, basetemp, *, windows=None):
    """Run the public suite under SkipPolicy; failures and violations are fatal."""
    import pytest

    policy = SkipPolicy(fifo_unsupported() if windows is None else windows)
    result = pytest.main(
        ["tests", "-q", "-p", "no:cacheprovider", "--rootdir", str(root)]
        + [f"--ignore=tests/{name}" for name in DEV_TESTS]
        + ["--basetemp", str(basetemp)],
        plugins=[policy],
    )
    problems = policy.verdict()
    for problem in problems:
        print("skip policy violation:", problem)
    if not problems:
        print(policy.summary())
    return int(result) or int(bool(problems))


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
sys.path.insert(0, str(root / "tools"))
import verify_offline
result = verify_offline.run_tests(root, scratch / "pytest")
for name, module in tuple(sys.modules.items()):
    if name == "geecomposer" or name.startswith("geecomposer."):
        assert Path(module.__file__).resolve().is_relative_to(root / "src")
raise SystemExit(result)
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
"""
        + INVENTORY
        + """
checks = 0
for args in [["--help"]] + [args for p in recipes for args in
        (["validate", str(p)], ["run", "--recipe", str(p), "--dry-run"])]:
    sys.argv = [str(entry), *args]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            runpy.run_path(str(entry), run_name="__main__")
        except SystemExit as exc:
            assert exc.code in (None, 0)
    if args == ["--help"]:
        assert all(o in output.getvalue() for o in ("--project", "--credentials", "--state-dir"))
    else:
        value = json.loads(output.getvalue())
        assert value["status"] == ("validated" if args[0] == "validate" else "dry_run")
        if any(Path(a).name.startswith("personal_") for a in args):
            assert set(value["destination"]) == {"drive_folder"}
        checks += 1
assert checks == 2 * len(recipes) == 24
for name, module in tuple(sys.modules.items()):
    if name == "geecomposer" or name.startswith("geecomposer."):
        assert Path(module.__file__).resolve().is_relative_to(installed)
assert not ee.data.is_initialized()
print(f"Installed wheel origins and CLI: help + {checks} recipe checks passed")
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
