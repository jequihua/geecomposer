"""Exact locked-wheel test-tree pruning, only in the disposable builder venv.

Wheel artifacts and RECORD files are not rewritten. See container/README.md.
"""

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

LOCK_SHA256 = "84c29e6bcc93eaf8f76a0fd87458173ddfacb25a9a756540baaff35c7dbf45d0"
TREES = {
    "numpy": (
        "numpy/_core/tests",
        "numpy/_pyinstaller/tests",
        "numpy/distutils/tests",
        "numpy/f2py/tests",
        "numpy/fft/tests",
        "numpy/lib/tests",
        "numpy/linalg/tests",
        "numpy/ma/tests",
        "numpy/matrixlib/tests",
        "numpy/polynomial/tests",
        "numpy/random/tests",
        "numpy/testing/tests",
        "numpy/tests",
        "numpy/typing/tests",
    ),
    "pandas": ("pandas/tests",),
    "geopandas": ("geopandas/io/tests", "geopandas/tests", "geopandas/tools/tests"),
    "shapely": ("shapely/tests",),
    "pyogrio": ("pyogrio/tests",),
    "earthengine-api": ("ee/tests",),
    "certifi": ("certifi/tests",),
}
FIXTURES = ("ee/tests/algorithms.json", "ee/tests/cloud_api_discovery_document.json")
SUPPORT = (
    "ee/apitestcase.py",
    "numpy/testing/__init__.py",
    "numpy/testing/_private/utils.py",
    "cryptography/hazmat/bindings/_rust/test_support.pyi",
    "certifi/cacert.pem",
    "certifi/core.py",
    "certifi/py.typed",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def no_links(path):
    """Reject symlinks and Windows junction/reparse points, including ancestors."""
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"link/reparse path rejected: {part.name}")


def safe_path(root, relative):
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or PurePosixPath(relative).is_absolute()
        or "\\" in relative
        or ":" in relative
        or any(p in (".", "..") for p in relative.split("/"))
    ):
        raise ValueError(f"unsafe relative path: {relative!r}")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or path == root:
        raise ValueError("path escapes site-packages")
    no_links(path)
    return path


def versions(site, lock):
    no_links(site)
    no_links(lock)
    if sha(lock) != LOCK_SHA256:
        raise ValueError("runtime lock changed; review the removal inventory first")
    pins = dict(re.findall(r"^([\w.-]+)==([^\s\\]+)", lock.read_text(), re.M))
    actual = {}
    for dist in metadata.distributions(path=[str(site)]):
        name = re.sub(r"[-_.]+", "-", dist.metadata["Name"].lower())
        if name in actual or Path(dist.locate_file("")).resolve() != site.resolve():
            raise ValueError("duplicate or escaped distribution metadata")
        actual[name] = dist.version
    if actual != {**pins, "geecomposer": "0.1.0"}:
        raise ValueError("installed distributions differ from the reviewed runtime lock")
    return actual


def tree_files(site, relative):
    root = safe_path(site, relative)
    if not root.is_dir():
        raise ValueError(f"expected test directory: {relative}")
    files, directories = [], []
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            no_links(path)
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                directories.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise ValueError(f"unexpected special file in {relative}")
    return files, directories + [root]


def is_notice(path):
    return any(
        word in part.lower()
        for part in path.parts
        for word in ("license", "licence", "copyright", "copying", "notice", "authors")
    )


def residual_check(site):
    """Reject candidates for review; never infer permission to remove them."""
    no_links(site)
    exceptions = set(FIXTURES + SUPPORT)
    parents = {p.as_posix() for name in exceptions for p in PurePosixPath(name).parents}
    candidates, unsafe = [], []

    def walk_error(error):
        relative = Path(error.filename).relative_to(site).as_posix()
        raise ValueError(f"unreadable site-packages directory: {relative}") from None

    for current, dirs, files in os.walk(site, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + files):
            path = Path(current) / name
            relative = path.relative_to(site).as_posix()
            try:
                info = path.lstat()
            except OSError:
                unsafe.append(relative)
                if name in dirs:
                    dirs.remove(name)
                continue
            directory = stat.S_ISDIR(info.st_mode)
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not (directory or stat.S_ISREG(info.st_mode))
            ):
                unsafe.append(relative)
                if name in dirs:
                    dirs.remove(name)
            parts = PurePosixPath(relative).parts
            module = name.lower().split(".", 1)[0]
            test_module = (
                not directory
                and path.suffix.lower() in {".py", ".pyi", ".pyw", ".pyc", ".pyo", ".so", ".pyd"}
                and (module.startswith("test_") or module.endswith("_test"))
            )
            candidate = any(p.lower() in ("test", "tests") for p in parts) or test_module
            # Only required directory ancestors are permitted, never their siblings.
            if candidate and relative not in exceptions and not (directory and relative in parents):
                candidates.append(relative)
    if candidates or unsafe:
        raise ValueError(
            "forbidden/unreviewed test content: "
            + json.dumps(sorted(candidates))
            + "; unsafe site-packages entries: "
            + json.dumps(sorted(unsafe))
        )


def prune(site, archive, lock):
    """All layout checks precede writes; caller supplies only disposable roots."""
    site, archive, lock = map(lambda p: Path(p).absolute(), (site, archive, lock))
    installed = versions(site, lock)
    no_links(archive.parent)
    if archive.exists() or archive.is_symlink() or archive.resolve().is_relative_to(site.resolve()):
        raise ValueError("notice archive must be fresh and outside site-packages")
    retained = {name: sha(safe_path(site, name)) for name in FIXTURES + SUPPORT}
    files, directories, notices = [], [], []
    for dist, roots in TREES.items():
        for relative in roots:
            found, dirs = tree_files(site, relative)
            if not found:
                raise ValueError(f"empty/unexpected test tree: {relative}")
            files.extend(found)
            directories.extend(dirs)
            for path in found:
                rel = path.relative_to(site).as_posix()
                if rel not in FIXTURES and is_notice(Path(rel)):
                    notices.append(
                        {
                            "distribution": dist,
                            "original": rel,
                            "preserved": dist + "/" + rel,
                            "sha256": sha(path),
                        }
                    )
    # Store attribution and byte-identical notices before deleting any source file.
    archive.mkdir()
    for item in notices:
        dest = archive / item["preserved"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(safe_path(site, item["original"]).read_bytes())
        if sha(dest) != item["sha256"]:
            raise ValueError("notice preservation failed")
    record = {
        "lock_sha256": LOCK_SHA256,
        "versions": installed,
        "retained": retained,
        "notices": notices,
        "trees": TREES,
    }
    (archive / "inventory.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for path in files:
        if path.relative_to(site).as_posix() not in FIXTURES:
            # Recheck containment immediately before each destructive operation.
            safe_path(site, path.relative_to(site).as_posix()).unlink()
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if path == site / "ee/tests":
            continue
        safe_path(site, path.relative_to(site).as_posix()).rmdir()
    return check(site, archive, lock)


def check(site, archive, lock):
    """Read-only installed-content contract, shared with later image qualification."""
    site, archive, lock = map(lambda p: Path(p).absolute(), (site, archive, lock))
    residual_check(site)
    installed = versions(site, lock)
    no_links(archive)
    record = json.loads(safe_path(archive, "inventory.json").read_text(encoding="utf-8"))
    if record["lock_sha256"] != LOCK_SHA256 or record["versions"] != installed:
        raise ValueError("pruning inventory does not bind the runtime lock/versions")
    if record["trees"] != {n: list(p) for n, p in TREES.items()}:
        raise ValueError("unexpected removal inventory")
    if set(record["retained"]) != set(FIXTURES + SUPPORT):
        raise ValueError("unexpected retained-support inventory")
    for name, digest in record["retained"].items():
        if sha(safe_path(site, name)) != digest:
            raise ValueError(f"retained fixture/support changed: {name}")
    for roots in TREES.values():
        for relative in roots:
            path = site / relative
            if relative == "ee/tests":
                files, dirs = tree_files(site, relative)
                if {p.relative_to(site).as_posix() for p in files} != set(FIXTURES) or len(
                    dirs
                ) != 1:
                    raise ValueError("forbidden executable suite or unexpected EE fixture content")
            elif path.exists() or path.is_symlink():
                raise ValueError(f"forbidden dependency test suite: {relative}")
    for item in record["notices"]:
        dist, original = item["distribution"], item["original"]
        if dist not in TREES or not any(original.startswith(p + "/") for p in TREES[dist]):
            raise ValueError("unexpected notice attribution")
        if item["preserved"] != dist + "/" + original:
            raise ValueError("unexpected preserved notice path")
        if sha(safe_path(archive, item["preserved"])) != item["sha256"]:
            raise ValueError("preserved notice changed")
    return {
        "versions": installed,
        "site_packages_origin": str(site),
        "retained": record["retained"],
        "preserved_notices": record["notices"],
        "forbidden_suites": [],
    }


if __name__ == "__main__":
    # No arbitrary-path cleanup CLI: this entry point is the Linux builder only.
    if sys.platform != "linux" or sys.prefix != "/opt/venv":
        raise SystemExit("cleanup requires the disposable /opt/venv builder interpreter")
    print(
        json.dumps(
            prune(
                Path("/opt/venv/lib/python3.11/site-packages"),
                Path("/opt/venv/pruned-notices"),
                Path("/locks/runtime-linux.lock"),
            )
        )
    )
