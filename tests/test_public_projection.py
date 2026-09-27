"""Bounded checks of the portable gate and public document/asset contract."""

import importlib.util
import os
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


def _report(gate, nodeid=None, *, when="setup", outcome="skipped", reason=None, **extra):
    from types import SimpleNamespace

    reason = gate.FIFO_REASON if reason is None else reason
    return SimpleNamespace(
        nodeid=gate.FIFO_TEST if nodeid is None else nodeid,
        when=when,
        outcome=outcome,
        passed=outcome == "passed",
        skipped=outcome == "skipped",
        longrepr=("tests/test_personal_cli.py", 1, "Skipped: " + reason) if outcome == "skipped" else None,
        **extra,
    )


def _policy(gate, windows, reports=(), *, collected=True, collection=()):
    from types import SimpleNamespace

    policy = gate.SkipPolicy(windows)
    if collected:
        policy.pytest_itemcollected(SimpleNamespace(nodeid=gate.FIFO_TEST))
    for report in collection:
        policy.pytest_collectreport(report)
    for report in reports:
        policy.pytest_runtest_logreport(report)
    return policy


def test_skip_policy_permits_only_the_named_windows_fifo_skip(gate):
    policy = _policy(gate, True, [_report(gate)])
    assert policy.verdict() == []
    assert "pending POSIX/Linux execution" in policy.summary()
    passed = _policy(gate, False, [_report(gate, when="call", outcome="passed")])
    assert passed.verdict() == [] and "executed and passed" in passed.summary()


@pytest.mark.parametrize(
    "case",
    [
        "other_node",
        "wrong_reason",
        "linux_skip",
        "call_phase_skip",
        "xfail",
        "second_skip",
        "not_collected",
        "linux_not_run",
        "windows_not_skipped",
        "collection_skip",
        "collection_error",
    ],
)
def test_skip_policy_rejects_everything_else(gate, case):
    from types import SimpleNamespace

    windows, reports, collected, collection = True, [_report(gate)], True, []
    if case == "other_node":
        reports.append(_report(gate, "tests/test_other.py::test_x"))
    elif case == "wrong_reason":
        reports = [_report(gate, reason="unsupported")]
    elif case == "linux_skip":
        windows = False
    elif case == "call_phase_skip":
        reports = [_report(gate, when="call")]
    elif case == "xfail":
        reports = [_report(gate, wasxfail="")]
    elif case == "second_skip":
        reports.append(_report(gate))
    elif case == "not_collected":
        collected = False
    elif case == "linux_not_run":
        windows, reports = False, []
    elif case == "windows_not_skipped":
        reports = [_report(gate, when="call", outcome="passed")]
    else:
        outcome = "skipped" if case == "collection_skip" else "failed"
        collection = [SimpleNamespace(nodeid="tests/test_x.py", outcome=outcome, passed=False)]
    assert _policy(gate, windows, reports, collected=collected, collection=collection).verdict()


FIXTURE_SUITE = '''
import pytest

@pytest.mark.skipif(SKIP, reason=REASON)
def test_posix_fifo_records_never_block():
    pass

def test_other():
    assert not FAIL
'''


@pytest.mark.parametrize(
    "windows,skip,extra,code",
    [
        (True, True, "", 0),
        (False, False, "", 0),
        (False, True, "", 1),
        (True, True, "wrong_reason", 1),
        (True, True, "extra_skip", 1),
        (True, True, "failure", 1),
    ],
)
def test_skip_policy_is_wired_into_the_test_stage(gate, tmp_path, windows, skip, extra, code):
    """Bounded child pytest over a two-test fixture suite, through run_tests()."""
    root = tmp_path / "pkg"
    (root / "tests").mkdir(parents=True)
    body = (
        FIXTURE_SUITE.replace("SKIP", str(skip))
        .replace("REASON", repr("unsupported" if extra == "wrong_reason" else gate.FIFO_REASON))
        .replace("FAIL", str(extra == "failure"))
    )
    if extra == "extra_skip":
        body += "\n@pytest.mark.skip(reason='other')\ndef test_extra():\n    pass\n"
    (root / "tests" / "test_personal_cli.py").write_text(body, encoding="utf-8")
    child = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "import verify_offline as g; "
        "raise SystemExit(g.run_tests(Path(sys.argv[2]), Path(sys.argv[3]), windows=sys.argv[4] == '1'))"
    )
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            child,
            str(gate.ROOT / "tools"),
            str(root),
            str(tmp_path / "basetemp"),
            "1" if windows else "0",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (result.returncode != 0) == bool(code), result.stdout[-2000:]
    if code == 0:
        expected = "Permitted platform skip" if windows else "executed and passed"
        assert expected in result.stdout
    elif extra != "failure":
        assert "skip policy violation" in result.stdout
