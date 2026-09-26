#!/usr/bin/env python3
"""Require release archive libraries to match the native test bundle exactly."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import tarfile
from pathlib import Path
from typing import cast

from release import ReleaseError, archive_members


def verify(archive: Path, bundle: Path, asset: str) -> None:
    members = archive_members(archive)
    print(f"{asset}: release archive sha256={hashlib.sha256(archive.read_bytes()).hexdigest()}")
    print(f"{asset}: test bundle sha256={hashlib.sha256(bundle.read_bytes()).hexdigest()}")
    if asset.startswith("macos-"):
        shared = [
            name
            for name, data in members.items()
            if name.startswith("lib/libmimalloc.3.")
            and name.endswith(".dylib")
            and not data.startswith(b"SYMLINK:")
        ]
        if len(shared) != 1:
            raise ReleaseError(f"expected one versioned Mach-O library, got {shared}")
        static = [
            name for name in members if name.startswith("lib/") and name.endswith("libmimalloc.a")
        ]
        if len(static) != 1:
            raise ReleaseError(f"expected one static Mach-O library, got {static}")
        required = {shared[0]: Path(shared[0]).name, static[0]: "libmimalloc.a"}
    elif asset.startswith("windows-"):
        suffix = "libmimalloc.a" if asset.endswith("-gnu") else "mimalloc.lib"
        static = [name for name in members if name.startswith("lib/") and name.endswith(suffix)]
        if len(static) != 1:
            raise ReleaseError(f"expected one static Windows library, got {static}")
        required = {"bin/mimalloc.dll": "mimalloc.dll", static[0]: Path(static[0]).name}
    else:
        raise ReleaseError(f"unknown release asset {asset}")

    with tarfile.open(bundle, "r:gz") as tar:
        rows: dict[str, tarfile.TarInfo] = {}
        links: dict[str, str] = {}
        root_seen = False
        for row in tar.getmembers():
            raw = row.name
            if raw in (".", "./") and row.isdir():
                if root_seen:
                    raise ReleaseError(f"duplicate test bundle member {raw}")
                root_seen = True
                continue
            name = raw.removeprefix("./")
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or re.match(r"^[A-Za-z]:", name)
                or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                or not (row.isfile() or row.isdir() or row.issym())
            ):
                raise ReleaseError(f"unsafe test bundle member {raw}")
            if row.isfile():
                if name in rows or name in links:
                    raise ReleaseError(f"duplicate test bundle member {raw}")
                rows[name] = row
            elif row.issym():
                if (
                    name in links
                    or name in rows
                    or row.linkname.startswith("/")
                    or "\\" in row.linkname
                    or re.match(r"^[A-Za-z]:", row.linkname)
                ):
                    raise ReleaseError(f"unsafe test bundle member {raw}")
                links[name] = row.linkname
        for name in links:
            seen: set[str] = set()
            current = name
            while current in links:
                if current in seen:
                    raise ReleaseError(f"unsafe test bundle member {name}")
                seen.add(current)
                current = posixpath.normpath(
                    posixpath.join(posixpath.dirname(current), links[current])
                )
                if current == ".." or current.startswith("../"):
                    raise ReleaseError(f"unsafe test bundle member {name}")
            if current not in rows:
                raise ReleaseError(f"unsafe test bundle member {name}")
        manifest = rows.get("tests.json")
        if manifest is None:
            raise ReleaseError("test bundle has no tests.json")
        stream = tar.extractfile(manifest)
        if stream is None:
            raise ReleaseError("cannot read test manifest")
        payload = cast("dict[str, object]", json.load(stream))
        tests = payload.get("tests")
        if not isinstance(tests, list):
            raise ReleaseError("test manifest has no test list")
        names = {
            name
            for item in cast("list[object]", tests)
            if isinstance(item, dict)
            for name in [cast("dict[str, object]", item).get("name")]
            if isinstance(name, str)
        }
        if len(names) < 20 or "test-stress-dynamic" not in names:
            raise ReleaseError("release bundle lacks the expected C suite or dynamic-link test")
        for archive_name, bundle_name in required.items():
            shipped = members.get(archive_name)
            row = rows.get(bundle_name)
            if shipped is None or row is None:
                raise ReleaseError(
                    f"missing packaged or test library: {archive_name}, {bundle_name}"
                )
            stream = tar.extractfile(row)
            if stream is None or stream.read() != shipped:
                raise ReleaseError(f"test bundle library differs from shipped {archive_name}")
            print(f"{asset}: {archive_name} sha256={hashlib.sha256(shipped).hexdigest()}")
        print(f"{asset}: {len(names)} release-configuration C tests present")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--asset", required=True)
    args = parser.parse_args()
    try:
        verify(args.archive, args.bundle, args.asset)
    except (ReleaseError, OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"release test bundle refused: {error}\n")
