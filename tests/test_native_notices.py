"""Offline integrity of repository-only static notices, not image qualification."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from urllib.parse import urlsplit

import pytest


DESTINATION = "/usr/share/doc/geecomposer/third-party-notices/"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def text(value):
    assert isinstance(value, str) and value.strip(), "empty attribution field"


def digest(value):
    assert isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid SHA-256"


def url(value):
    text(value)
    parsed = urlsplit(value)
    assert parsed.scheme == "https" and parsed.hostname, "invalid source URL"
    assert not parsed.username and not parsed.password, "source URL credentials"


def image_path(value):
    text(value)
    path = PurePosixPath(value)
    assert value.startswith("/") and path.as_posix() == value, "invalid image path"
    assert ".." not in path.parts and "\\" not in value and ":" not in value, "unsafe image path"


def validate(root):
    assert root.is_dir() and not root.is_symlink(), "notice root must be a directory"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["destination"] == DESTINATION
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["image"])
    digest(manifest["runtime_lock_sha256"])
    assert manifest["evidence"] and manifest["sources"] and manifest["components"]
    for value in manifest["evidence"].values():
        digest(value)
    for record in manifest["sources"].values():
        url(record["url"])
        digest(record["sha256"])
    for path, value in manifest["binaries"].items():
        image_path(path)
        digest(value)
    for path, record in manifest["existing"].items():
        image_path(path)
        text(record["role"])
        assert record["source_image"] == manifest["image"]
        digest(record["source_sha256"])
        assert record["shipped_sha256"] == record["source_sha256"]

    assets = manifest["assets"]
    assert assets, "no notice assets"
    for name, record in assets.items():
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.txt", name), "unsafe asset path"
        assert record["destination"] == DESTINATION + name
        digest(record["shipped_sha256"])
    expected = {".gitattributes", "manifest.json", "coverage.md", *assets}
    assert {p.name for p in root.iterdir()} == expected, "asset membership mismatch"
    for path in root.iterdir():
        assert path.is_file() and not path.is_symlink(), "asset must be a regular file"
        assert path.read_bytes().strip(), "empty asset"
    for name, record in assets.items():
        data = (root / name).read_bytes()
        assert sha(data) == record["shipped_sha256"], "asset hash mismatch"
        assert len(data) == record["bytes"], "asset length mismatch"

    seen = set()
    referenced = set()
    for component in manifest["components"]:
        for key in ("id", "component", "distribution", "distribution_version", "kind", "note"):
            text(component[key])
        assert component["id"] not in seen, "duplicate component"
        seen.add(component["id"])
        assert component["status"] in {"mapped", "gap"}, "invalid coverage status"
        if component["version"] is not None:
            text(component["version"])
        if component["status"] == "gap":
            text(component["gap"])
        else:
            text(component["version"])
            assert component["notices"], "mapped component lacks notices"
        if component["release_url"] is None:
            assert component["distribution"] == "geecomposer"
            assert re.fullmatch(r"[0-9a-f]{40}", component["source_revision"])
        else:
            url(component["release_url"])
        assert set(component["binaries"]) <= manifest["binaries"].keys(), "unknown binary"
        assert set(component["evidence"]) <= manifest["sources"].keys(), "unknown source"
        if "package_observation" in component:
            observation = component["package_observation"]
            assert component["kind"] == "system", "package observation requires system provider"
            assert observation["image"] == manifest["image"], "package observation image mismatch"
            assert re.fullmatch(r"[A-Za-z0-9_.-]+\.json", observation["evidence_record"])
            for key in ("provider", "version", "package", "package_version", "architecture"):
                text(observation[key])
            for key in ("evidence_sha256", "dpkg_status_sha256", "package_stanza_sha256"):
                digest(observation[key])
            path = observation["copyright_path"]
            assert {"existing": path} in component["notices"], "unbound package copyright"
            assert (
                observation["copyright_sha256"] == manifest["existing"][path]["shipped_sha256"]
            ), "package copyright hash mismatch"
        for notice in component["notices"]:
            assert ("asset" in notice) != ("existing" in notice), "ambiguous notice location"
            if "existing" in notice:
                assert notice["existing"] in manifest["existing"], "unknown existing notice"
                continue
            name = notice["asset"]
            assert name in assets, "unknown asset"
            referenced.add(name)
            url(notice["source_url"])
            for key in ("source_sha256", "shipped_sha256"):
                digest(notice[key])
            assert notice["shipped_sha256"] == assets[name]["shipped_sha256"]
            text(notice["role"])
            text(notice["transformation"])
            if notice["transformation"] == "none":
                assert notice["source_sha256"] == notice["shipped_sha256"]
            if "source_member" in notice:
                member = PurePosixPath(notice["source_member"])
                assert not member.is_absolute() and ".." not in member.parts
                assert "\\" not in notice["source_member"] and ":" not in notice["source_member"]
                digest(notice["archive_sha256"])
    assert referenced == assets.keys(), "unreferenced asset"
    return manifest


@pytest.fixture
def notice_root():
    root = Path(__file__).resolve().parents[1] / "container/third_party_notices"
    if not root.exists() and not root.is_symlink():
        pytest.skip("repository-only notice asset root absent from standalone source/tests copy")
    validate(root)
    return root


def test_shipped_notices(notice_root):
    manifest = validate(notice_root)
    lock = notice_root.parent / "runtime-linux.lock"
    assert sha(lock.read_bytes()) == manifest["runtime_lock_sha256"]


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("missing", "asset membership mismatch"),
        ("changed", "asset hash mismatch"),
        ("extra", "asset membership mismatch"),
        ("empty", "empty asset"),
        ("unsafe", "unsafe asset path"),
        ("hash", "invalid SHA-256"),
        ("attribution", "empty attribution field"),
        ("source", "invalid source URL"),
        ("unresolved", "empty attribution field"),
        ("package-image", "package observation image mismatch"),
        ("package-version", "empty attribution field"),
        ("package-hash", "invalid SHA-256"),
        ("package-copyright", "package copyright hash mismatch"),
    ],
)
def test_rejects_corrupt_assets(notice_root, tmp_path, mutation, message):
    root = tmp_path / "notices"
    shutil.copytree(notice_root, root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    name = next(iter(manifest["assets"]))
    notice = next(n for c in manifest["components"] for n in c["notices"] if "asset" in n)
    if mutation == "missing":
        (root / name).unlink()
    elif mutation == "changed":
        with (root / name).open("ab") as stream:
            stream.write(b"altered")
    elif mutation == "extra":
        (root / "unexpected.txt").write_bytes(b"unreviewed")
    elif mutation == "empty":
        (root / name).write_bytes(b"")
    elif mutation == "unsafe":
        manifest["assets"]["../escape.txt"] = manifest["assets"].pop(name)
    elif mutation == "hash":
        manifest["assets"][name]["shipped_sha256"] = "not a hash"
    elif mutation == "attribution":
        notice["role"] = ""
    elif mutation == "source":
        notice["source_url"] = "file:///private"
    elif mutation == "unresolved":
        # Keep gap validation meaningful even when the shipped inventory has no gaps.
        manifest["components"][0].update(status="gap", version=None, gap="")
    elif mutation.startswith("package-"):
        observation = next(
            c["package_observation"] for c in manifest["components"] if "package_observation" in c
        )
        key, value = {
            "package-image": ("image", "sha256:" + "0" * 64),
            "package-version": ("package_version", ""),
            "package-hash": ("evidence_sha256", "invalid"),
            "package-copyright": ("copyright_sha256", "0" * 64),
        }[mutation]
        observation[key] = value
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match=message):
        validate(root)


@pytest.mark.parametrize("shape", ["file", "empty-directory", "invalid-json"])
def test_present_malformed_root_fails(tmp_path, shape):
    root = tmp_path / "notices"
    if shape == "file":
        root.write_bytes(b"not a directory")
    else:
        root.mkdir()
        if shape == "invalid-json":
            (root / "manifest.json").write_bytes(b"{")
    with pytest.raises((AssertionError, FileNotFoundError, json.JSONDecodeError)):
        validate(root)
