"""Pruning uses tiny fake installs only; no host/dependency environment is mutated."""

import importlib.util
import json
from pathlib import Path
import re
import stat
from types import SimpleNamespace

import pytest


@pytest.fixture
def contract():
    path = Path(__file__).resolve().parents[1] / "container/runtime_content.py"
    try:
        code = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pytest.skip("container content helper absent from source/tests-only copy")
    spec = importlib.util.spec_from_loader("content_contract", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(code, str(path), "exec"), module.__dict__)
    return module


@pytest.fixture
def install(tmp_path, contract):
    site = tmp_path / "site-packages"
    site.mkdir()
    lock = Path(__file__).resolve().parents[1] / "container/runtime-linux.lock"
    pins = dict(re.findall(r"^([\w.-]+)==([^\s\\]+)", lock.read_text(), re.M))
    for name, version in {**pins, "geecomposer": "0.1.0"}.items():
        folder = site / (name.replace("-", "_") + "-" + version + ".dist-info")
        folder.mkdir()
        (folder / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
        (folder / "RECORD").write_text("upstream record unchanged\n")
        (folder / "LICENSE").write_bytes(b"upstream license\x00\xff")
    for roots in contract.TREES.values():
        for relative in roots:
            directory = site / relative
            directory.mkdir(parents=True)
            (directory / "test_suite.py").write_text("raise AssertionError('suite')")
            (directory / "__pycache__").mkdir()
            (directory / "__pycache__/test_suite.pyc").write_bytes(b"bytecode")
    for name in contract.FIXTURES + contract.SUPPORT:
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained exact bytes\x00\xff")
    for name in [
        "numpy/testing/api.py",
        "numpy/runtime.so",
        "pyproj/proj_dir/share/proj/proj.db",
        "unrelated/runtime.py",
        "geecomposer/__init__.py",
    ]:
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unrelated unchanged")
    return site, tmp_path / "pruned-notices", lock


def snapshot(root):
    # Only the tiny test-created directory, never site-packages on the host.
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_exact_trees_only_and_byte_preservation(contract, install):
    site, archive, lock = install
    # Keep the original deletion-boundary fixture: it survives pruning but
    # must now fail the final read-only content gate rather than be accepted.
    unrelated = site / "unrelated/tests/test_keep.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated unchanged")
    notices = [
        "numpy/_core/tests/data/LICENSE.txt",
        "pandas/tests/COPYRIGHT",
        "ee/tests/notices/attribution.txt",
        "certifi/tests/AUTHORS",
    ]
    for name in notices:
        p = site / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"license\r\n\x00\xff")
    before = snapshot(site)
    with pytest.raises(ValueError, match="unrelated/tests/test_keep.py"):
        contract.prune(site, archive, lock)
    expected = {
        n: b
        for n, b in before.items()
        if n in contract.FIXTURES
        or not any(n.startswith(root + "/") for roots in contract.TREES.values() for root in roots)
    }
    assert snapshot(site) == expected
    record = json.loads((archive / "inventory.json").read_text())
    result = {
        "versions": record["versions"],
        "preserved_notices": record["notices"],
        "retained": record["retained"],
    }
    assert len(result["versions"]) == 37
    assert len(result["preserved_notices"]) == 4
    for item in result["preserved_notices"]:
        assert (archive / item["preserved"]).read_bytes() == before[item["original"]]
        assert item["sha256"] == contract.sha(archive / item["preserved"])
    assert set(result["retained"]) == set(contract.FIXTURES + contract.SUPPORT)
    with pytest.raises(ValueError, match="unrelated/tests/test_keep.py"):
        contract.check(site, archive, lock)
    assert snapshot(site) == expected


@pytest.mark.parametrize(
    "bad", ["../escape", "/absolute", "C:/escape", "a\\b", "a/../b", "./x", ""]
)
def test_traversal_rejected(contract, install, bad):
    site, archive, lock = install
    before = snapshot(site)
    with pytest.raises(ValueError):
        contract.safe_path(site, bad)
    assert snapshot(site) == before
    assert not archive.exists()


@pytest.mark.parametrize(
    "case", ["version", "lock", "missing", "empty", "file", "archive", "fixture", "inside"]
)
def test_layout_failure_before_mutation(contract, install, tmp_path, case):
    site, archive, lock = install
    if case == "version":
        p = site / "numpy-2.4.6.dist-info/METADATA"
        p.write_text("Name: numpy\nVersion: 0.0\n")
    elif case == "lock":
        lock = tmp_path / "changed.lock"
        lock.write_text("numpy==0.0\n")
    elif case in ("missing", "empty", "file"):
        p = site / "certifi/tests"
        for f in [p / "test_suite.py", p / "__pycache__/test_suite.pyc"]:
            f.unlink()
        (p / "__pycache__").rmdir()
        if case != "empty":
            p.rmdir()
        if case == "file":
            p.write_text("unexpected file")
    elif case == "archive":
        archive.mkdir()
    elif case == "fixture":
        (site / contract.FIXTURES[0]).unlink()
    else:
        archive = site / "forbidden-output"
    before = snapshot(site)
    with pytest.raises((ValueError, FileNotFoundError)):
        contract.prune(site, archive, lock)
    assert snapshot(site) == before
    assert not (archive / "inventory.json").exists()


def test_notice_write_failure_does_not_prune(contract, install, monkeypatch):
    site, archive, lock = install
    (site / "pandas/tests/LICENSE").write_text("preserve before pruning")
    before = snapshot(site)
    real_write = Path.write_bytes

    def fail_archive(path, data):
        if path.is_relative_to(archive):
            raise PermissionError("synthetic archive write failure")
        return real_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_archive)
    with pytest.raises(PermissionError, match="synthetic"):
        contract.prune(site, archive, lock)
    assert snapshot(site) == before


@pytest.mark.parametrize("location", ["parent", "tree", "child"])
@pytest.mark.parametrize("kind", ["symlink", "reparse"])
def test_links_rejected_portably_without_host_privileges(
    contract, install, monkeypatch, location, kind
):
    site, archive, lock = install
    suspect = {
        "parent": site.parent,
        "tree": site / "numpy/tests",
        "child": site / "numpy/tests/test_suite.py",
    }[location]
    real_stat = Path.lstat

    def lstat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if path == suspect:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if kind == "symlink" else value.st_mode,
                st_file_attributes=0x400 if kind == "reparse" else 0,
            )
        return value

    before = snapshot(site)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="link/reparse"):
        contract.prune(site, archive, lock)
    monkeypatch.undo()
    assert snapshot(site) == before
    assert not archive.exists()


@pytest.mark.parametrize(
    "bad",
    [
        "numpy/tests/test_again.py",
        "ee/tests/unexpected.json",
        "ee/tests/test_again.py",
        "certifi/tests/test_certify.py",
    ],
)
def test_checker_rejects_confirmed_suites_not_support(contract, install, bad):
    site, archive, lock = install
    contract.prune(site, archive, lock)
    path = site / bad
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("forbidden content")
    with pytest.raises(ValueError, match="forbidden"):
        contract.check(site, archive, lock)


@pytest.mark.parametrize("which", ["fixture", "support", "notice", "notice-path"])
def test_checker_detects_retention_corruption(contract, install, which):
    site, archive, lock = install
    (site / "pandas/tests/LICENSE").write_text("keep bytes")
    contract.prune(site, archive, lock)
    if which in ("fixture", "support"):
        path = site / (contract.FIXTURES[0] if which == "fixture" else contract.SUPPORT[0])
        path.write_text("changed")
    elif which == "notice":
        (archive / "pandas/pandas/tests/LICENSE").write_text("changed")
    else:
        p = archive / "inventory.json"
        record = json.loads(p.read_text())
        record["notices"][0]["preserved"] = "../outside"
        p.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        contract.check(site, archive, lock)


def test_exact_exceptions_and_support_apis_pass(contract, install):
    site, archive, lock = install
    result = contract.prune(site, archive, lock)
    before = snapshot(site)
    assert result["forbidden_suites"] == []
    assert set(result["retained"]) == set(contract.FIXTURES + contract.SUPPORT)
    assert contract.check(site, archive, lock) == result
    assert snapshot(site) == before


@pytest.mark.parametrize(
    "relative",
    [
        "numpy/unlisted/tests/test_case.py",
        "other/test/data.json",
        "other/test_extra.py",
        "other/extra_test.py",
        "other/test_extra.pyi",
        "other/test_extra.pyw",
        "other/__pycache__/test_extra.cpython-311.pyc",
        "other/__pycache__/extra_test.cpython-311.opt-1.pyc",
        "other/extra_test.pyo",
        "other/test_extra.cpython-311-x86_64-linux-gnu.so",
        "other/extra_test.cp311-win_amd64.pyd",
        "ee/tests/algorithms.json.bak",
        "ee/tests/sibling/data.json",
        "cryptography/hazmat/bindings/_rust/test_support.py",
        "cryptography/hazmat/bindings/_rust/__pycache__/test_support.cpython-311.pyc",
        "numpy/testing/test_unreviewed.py",
    ],
)
def test_residual_modules_and_siblings_are_read_only_failures(contract, install, relative):
    site, archive, lock = install
    contract.prune(site, archive, lock)
    path = site / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PRIVATE contents never in diagnostics")
    before, notices = snapshot(site), snapshot(archive)
    with pytest.raises(ValueError) as error:
        contract.check(site, archive, lock)
    assert relative in str(error.value)
    assert str(site) not in str(error.value)
    assert "PRIVATE" not in str(error.value)
    assert snapshot(site) == before
    assert snapshot(archive) == notices


def test_residual_reports_all_paths_in_order_including_empty_directories(contract, install):
    site, archive, lock = install
    contract.prune(site, archive, lock)
    for directory in ("z/tests", "a/test"):
        (site / directory).mkdir(parents=True)
    (site / "z/tests/payload.dat").write_text("not read")
    (site / "b_test.pyc").write_bytes(b"compiled")
    before = snapshot(site)
    expected = 'forbidden/unreviewed test content: ["a/test", "b_test.pyc", "z/tests", "z/tests/payload.dat"]; unsafe site-packages entries: []'
    with pytest.raises(ValueError) as error:
        contract.check(site, archive, lock)
    assert str(error.value) == expected
    assert (site / "a/test").is_dir()
    assert snapshot(site) == before


@pytest.mark.parametrize("kind", ["symlink", "reparse", "special"])
def test_residual_does_not_follow_unsafe_entries(contract, install, monkeypatch, kind):
    site, archive, lock = install
    contract.prune(site, archive, lock)
    suspect = site / "unlisted-link"
    suspect.mkdir()
    (suspect / "must-not-enumerate").write_text("PRIVATE")
    before = snapshot(site)
    real_lstat, real_scandir = Path.lstat, contract.os.scandir

    def lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if path == suspect:
            return SimpleNamespace(
                st_mode={
                    "symlink": stat.S_IFLNK,
                    "reparse": result.st_mode,
                    "special": stat.S_IFIFO,
                }[kind],
                st_file_attributes=0x400 if kind == "reparse" else 0,
            )
        return result

    def scandir(path):
        assert Path(path) != suspect, "unsafe entry traversed"
        return real_scandir(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(contract.os, "scandir", scandir)
    with pytest.raises(ValueError) as error:
        contract.check(site, archive, lock)
    assert (
        str(error.value)
        == 'forbidden/unreviewed test content: []; unsafe site-packages entries: ["unlisted-link"]'
    )
    monkeypatch.undo()
    assert snapshot(site) == before


def test_residual_unreadable_directory_fails_without_host_path(contract, install, monkeypatch):
    site, archive, lock = install
    contract.prune(site, archive, lock)
    suspect = site / "unreadable"
    suspect.mkdir()
    real_scandir = contract.os.scandir

    def scandir(path):
        if Path(path) == suspect:
            raise PermissionError(13, "PRIVATE", str(suspect))
        return real_scandir(path)

    monkeypatch.setattr(contract.os, "scandir", scandir)
    with pytest.raises(ValueError) as error:
        contract.check(site, archive, lock)
    assert str(error.value) == "unreadable site-packages directory: unreadable"
