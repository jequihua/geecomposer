"""Preserve pinned bootstrap notices before removing the exact runtime ensurepip tree."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import sysconfig
import zipfile

WHEELS = {
    "pip-24.0-py3-none-any.whl": "ba0d021a166865d2265246961bec0152ff124de910c5cc39f1156ce3fa7c69dc",
    "setuptools-79.0.1-py3-none-any.whl": "e147c0549f27767ba362f9da434eab9c5dc0045d5304feb602a0af001089fc51",
}
MODULES = {"__init__.py", "__main__.py", "_uninstall.py"}
MAIN_SHA256 = "ee735f518d0fc4dfec81f7aa3da1e052372ed4202c0da4eddd2587840beaecd7"
MAX_WHEEL = 4 * 1024 * 1024
MAX_NOTICES = 4 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def no_links(path):
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("link/reparse path rejected")


def notice_member(name):
    # Preserve notice texts, never executable vendor support or entire directories.
    leaf = PurePosixPath(name).name.lower()
    return leaf in {"metadata", "vendor.txt", "vendored.txt"} or bool(
        re.match(r"^(licen[cs]e|copying|copyright|notice|authors)([._-].*)?$", leaf)
    )


def wheel_notices(path, expected):
    no_links(path)
    if not path.is_file() or path.stat().st_size > MAX_WHEEL:
        raise ValueError("unexpected bootstrap wheel size/type")
    if sha(path.read_bytes()) != expected:
        raise ValueError("bootstrap wheel hash changed")
    notices, seen, total = {}, set(), 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 4096:
            raise ValueError("unexpected bootstrap ZIP entry count")
        for item in members:
            parts = item.filename.rstrip("/").split("/")
            if (
                not item.filename
                or item.orig_filename != item.filename
                or "\x00" in item.orig_filename
                or "\\" in item.filename
                or ":" in item.filename
                or any(p in ("", ".", "..") for p in parts)
                or item.filename.casefold() in seen
                or item.flag_bits & 1
            ):
                raise ValueError("unsafe/duplicate bootstrap ZIP member")
            seen.add(item.filename.casefold())
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("unsafe bootstrap ZIP member type")
            if item.is_dir() or not notice_member(item.filename):
                continue
            if PurePosixPath(item.filename).suffix.lower() in {
                ".py",
                ".pyc",
                ".pyo",
                ".pyi",
                ".so",
                ".pyd",
                ".exe",
                ".whl",
                ".zip",
            }:
                raise ValueError("executable/archive named as a notice")
            total += item.file_size
            if item.file_size <= 0 or total > MAX_NOTICES:
                raise ValueError("unexpected bootstrap notice size")
            with archive.open(item) as stream:
                data = stream.read(item.file_size + 1)
            if len(data) != item.file_size:
                raise ValueError("incomplete bootstrap notice")
            notices[item.filename] = data
    dist_info = path.name.split("-py3-", 1)[0] + ".dist-info/"
    if not any(
        n.startswith(dist_info)
        and PurePosixPath(n).name.lower() not in {"metadata", "vendor.txt", "vendored.txt"}
        for n in notices
    ):
        raise ValueError("bootstrap distribution notice missing")
    return notices


def remove_bootstrap(stdlib, archive):
    """Only fixed children of the supplied stdlib; tests supply tiny synthetic trees."""
    stdlib, archive = Path(stdlib).absolute(), Path(archive).absolute()
    no_links(stdlib)
    no_links(archive.parent)
    if stdlib.name != "python3.11" or not stdlib.is_dir():
        raise ValueError("unexpected stdlib identity")
    if (
        archive.exists()
        or archive.is_symlink()
        or archive.resolve().is_relative_to(stdlib.resolve())
    ):
        raise ValueError("notice archive must be fresh and outside stdlib")
    target = stdlib / "ensurepip"
    no_links(target)
    if not target.is_dir() or target.resolve().parent != stdlib.resolve():
        raise ValueError("unexpected bootstrap target")
    license_path = stdlib / "LICENSE.txt"
    no_links(license_path)
    license_hash = sha(license_path.read_bytes())
    files, directories = [], []

    def unreadable(error):
        raise error

    for current, dirs, names in os.walk(target, followlinks=False, onerror=unreadable):
        for name in dirs + names:
            path = Path(current) / name
            no_links(path)
            rel = path.relative_to(target).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode) and rel in {"_bundled", "__pycache__"}:
                directories.append(path)
            elif stat.S_ISREG(mode) and (
                rel in MODULES
                or rel in {"_bundled/" + n for n in WHEELS}
                or re.fullmatch(
                    r"__pycache__/(__init__|__main__|_uninstall)\.cpython-311(?:\.opt-[12])?\.pyc",
                    rel,
                )
            ):
                files.append(path)
            else:
                raise ValueError("unexpected bootstrap layout: " + rel)
    required = MODULES | {"_bundled/" + n for n in WHEELS}
    if not required <= {p.relative_to(target).as_posix() for p in files}:
        raise ValueError("incomplete bootstrap layout")
    if sha((target / "__main__.py").read_bytes()) != MAIN_SHA256:
        raise ValueError("bootstrap entry point changed")
    # All ZIP validation and reads complete before any write or removal.
    preserved = {
        name: wheel_notices(target / "_bundled" / name, digest) for name, digest in WHEELS.items()
    }
    record = {
        "removed": "ensurepip",
        "wheels": WHEELS,
        "cpython_license_sha256": license_hash,
        "notices": [],
    }
    archive.mkdir()
    for wheel, notices in preserved.items():
        for member, data in notices.items():
            # Member paths are attribution only: numeric .txt names cannot traverse or execute.
            dest = archive / f"{len(record['notices']):04d}.txt"
            dest.write_bytes(data)
            if dest.read_bytes() != data:
                raise ValueError("bootstrap notice preservation failed")
            record["notices"].append(
                {"wheel": wheel, "member": member, "file": dest.name, "sha256": sha(data)}
            )
    inventory = archive / "inventory.json"
    inventory.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if json.loads(inventory.read_text(encoding="utf-8")) != record:
        raise ValueError("bootstrap inventory preservation failed")
    for path in archive.iterdir():
        path.chmod(0o444)
    archive.chmod(0o555)
    # Recheck exact containment/type immediately before deleting each recorded entry.
    for path in files:
        no_links(path)
        if not path.resolve().is_relative_to(target.resolve()) or not path.is_file():
            raise ValueError("bootstrap removal escaped target")
        path.unlink()
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        no_links(path)
        if not path.resolve().is_relative_to(target.resolve()):
            raise ValueError("bootstrap directory escaped target")
        path.rmdir()
    target.rmdir()
    if target.exists() or sha(license_path.read_bytes()) != license_hash:
        raise ValueError("bootstrap removal/CPython notice check failed")
    return record


if __name__ == "__main__":
    if (
        sys.platform != "linux"
        or sys.version_info[:3] != (3, 11, 16)
        or sys.prefix != "/usr/local"
        or sysconfig.get_path("stdlib") != "/usr/local/lib/python3.11"
    ):
        raise SystemExit("requires pinned disposable runtime system Python")
    print(
        json.dumps(
            remove_bootstrap(
                Path(sysconfig.get_path("stdlib")),
                Path("/usr/share/doc/geecomposer/bootstrap-notices"),
            )
        )
    )
