"""Bootstrap removal is exercised only on tiny synthetic stdlib trees and ZIPs."""

import importlib.util
import json
from pathlib import Path
import stat
import zipfile

import pytest


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[1] / "container/runtime_bootstrap.py"
    try:
        code = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pytest.skip("bootstrap helper absent from source/tests-only copy")
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("bootstrap", loader=None)
    )
    exec(compile(code, str(path), "exec"), module.__dict__)
    return module


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            if isinstance(name, str):
                entry = zipfile.ZipInfo("placeholder")
                entry.filename = entry.orig_filename = name
                name = entry
            archive.writestr(name, data)


@pytest.fixture
def installation(tmp_path, helper, monkeypatch):
    stdlib = tmp_path / "python3.11"
    target = stdlib / "ensurepip"
    (target / "_bundled").mkdir(parents=True)
    (target / "__pycache__").mkdir()
    for name in helper.MODULES:
        (target / name).write_bytes(b"synthetic bootstrap")
    (target / "__pycache__/__init__.cpython-311.pyc").write_bytes(b"synthetic bytecode")
    (stdlib / "LICENSE.txt").write_bytes(b"CPython notice\r\n")
    (stdlib / "unrelated.py").write_bytes(b"runtime stays")
    pins = {}
    for name in helper.WHEELS:
        info = name.split("-py3-", 1)[0] + ".dist-info/"
        path = target / "_bundled" / name
        write_zip(
            path,
            [
                (info + "LICENSE.txt", b"notice\x00\xff\r\n"),
                (info + "METADATA", b"Name: synthetic\nLicense: MIT\n"),
                ("vendor/LICENSE-MIT", b"vendor notice"),
                ("vendor/vendor.txt", b"attribution==1"),
                ("installer/__init__.py", b"do not preserve code"),
            ],
        )
        pins[name] = helper.sha(path.read_bytes())
    monkeypatch.setattr(helper, "WHEELS", pins)
    monkeypatch.setattr(helper, "MAIN_SHA256", helper.sha(b"synthetic bootstrap"))
    return stdlib, tmp_path / "notices"


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_remove_exact_tree_preserves_notice_bytes_and_runtime(helper, installation):
    stdlib, archive = installation
    before = snapshot(stdlib)
    originals = {}
    for name in helper.WHEELS:
        with zipfile.ZipFile(stdlib / "ensurepip/_bundled" / name) as wheel:
            originals.update(
                {(name, n): wheel.read(n) for n in wheel.namelist() if helper.notice_member(n)}
            )
    result = helper.remove_bootstrap(stdlib, archive)
    assert not (stdlib / "ensurepip").exists()
    assert snapshot(stdlib) == {n: b for n, b in before.items() if not n.startswith("ensurepip/")}
    assert result == json.loads((archive / "inventory.json").read_text())
    assert result["wheels"] == helper.WHEELS
    assert result["cpython_license_sha256"] == helper.sha(before["LICENSE.txt"])
    assert len(result["notices"]) == 8
    for item in result["notices"]:
        assert (archive / item["file"]).read_bytes() == originals[item["wheel"], item["member"]]
        assert item["sha256"] == helper.sha(originals[item["wheel"], item["member"]])
        assert (archive / item["file"]).suffix == ".txt"
    assert {p.name for p in archive.iterdir()} == {"inventory.json"} | {
        f"{n:04d}.txt" for n in range(8)
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing_module",
        "missing_wheel",
        "extra_module",
        "extra_wheel",
        "wrong_wheel_hash",
        "wrong_main",
        "wrong_stdlib",
        "missing_cpython_notice",
        "archive_exists",
        "archive_inside",
    ],
)
def test_invalid_layout_preserves_source(helper, installation, change):
    stdlib, archive = installation
    target = stdlib / "ensurepip"
    if change == "missing_module":
        (target / "_uninstall.py").unlink()
    elif change == "missing_wheel":
        (target / "_bundled" / next(iter(helper.WHEELS))).unlink()
    elif change == "extra_module":
        (target / "extra.py").write_text("extra")
    elif change == "extra_wheel":
        (target / "_bundled/other.whl").write_bytes(b"extra")
    elif change == "wrong_wheel_hash":
        (target / "_bundled" / next(iter(helper.WHEELS))).write_bytes(b"changed")
    elif change == "wrong_main":
        (target / "__main__.py").write_bytes(b"changed")
    elif change == "wrong_stdlib":
        moved = stdlib.with_name("python3.12")
        stdlib.rename(moved)
        stdlib = moved
    elif change == "missing_cpython_notice":
        (stdlib / "LICENSE.txt").unlink()
    elif change == "archive_exists":
        archive.mkdir()
    else:
        archive = stdlib.parent / "python3.11" / ".." / "python3.11" / "notices"
    before = snapshot(stdlib)
    with pytest.raises((ValueError, FileNotFoundError)):
        helper.remove_bootstrap(stdlib, archive)
    assert snapshot(stdlib) == before


@pytest.mark.parametrize(
    "kind",
    [
        "traversal",
        "absolute",
        "backslash",
        "duplicate",
        "symlink",
        "missing_notice",
        "executable_notice",
        "oversize_notice",
        "oversize_wheel",
        "too_many_members",
    ],
)
def test_zip_validation_before_any_removal(helper, installation, monkeypatch, kind):
    stdlib, archive = installation
    name = next(iter(helper.WHEELS))
    path = stdlib / "ensurepip/_bundled" / name
    notice = name.split("-py3-", 1)[0] + ".dist-info/LICENSE.txt"
    entries = [(notice, b"notice")]
    bad = {
        "traversal": "../LICENSE",
        "absolute": "/LICENSE",
        "backslash": "a\\LICENSE",
        "duplicate": notice.upper(),
        "executable_notice": "vendor/LICENSE.py",
    }
    if kind in bad:
        entries.append((bad[kind], b"bad"))
    elif kind == "symlink":
        link = zipfile.ZipInfo("vendor/LICENSE")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        entries.append((link, b"outside"))
    elif kind == "missing_notice":
        entries = [("installer/__init__.py", b"code")]
    elif kind == "oversize_notice":
        monkeypatch.setattr(helper, "MAX_NOTICES", 2)
    elif kind == "oversize_wheel":
        monkeypatch.setattr(helper, "MAX_WHEEL", 2)
    else:
        entries += [(f"code/{i}.py", b"") for i in range(4096)]
    write_zip(path, entries)
    helper.WHEELS[name] = helper.sha(path.read_bytes())
    before = snapshot(stdlib)
    with pytest.raises(ValueError):
        helper.remove_bootstrap(stdlib, archive)
    assert snapshot(stdlib) == before
    assert not archive.exists()


@pytest.mark.parametrize("location", ["target", "wheel", "archive_parent"])
def test_link_rejection_without_traversal(helper, installation, monkeypatch, location):
    # Inject lstat reparse metadata portably; no Windows privilege or real link required.
    stdlib, archive = installation
    selected = {
        "target": stdlib / "ensurepip",
        "wheel": stdlib / "ensurepip/_bundled" / next(iter(helper.WHEELS)),
        "archive_parent": archive.parent,
    }[location]
    original = Path.lstat
    from types import SimpleNamespace

    def linked(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    before = snapshot(stdlib)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", linked)
        with pytest.raises(ValueError, match="link/reparse"):
            helper.remove_bootstrap(stdlib, archive)
    assert snapshot(stdlib) == before
    assert not archive.exists()


def test_notice_write_failure_preserves_bootstrap(helper, installation, monkeypatch):
    stdlib, archive = installation
    before = snapshot(stdlib)
    original = Path.write_bytes

    def failed(path, data):
        if path.parent == archive:
            raise OSError("synthetic copy failure")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", failed)
    with pytest.raises(OSError, match="synthetic copy failure"):
        helper.remove_bootstrap(stdlib, archive)
    assert snapshot(stdlib) == before
