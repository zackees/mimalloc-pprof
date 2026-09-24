#!/usr/bin/env python3
"""Exercise the exact library member recorded in a dry-run release preflight."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from release import ReleaseError, archive_members

HOSTS = {
    "macos-arm64": ("darwin", "arm64"),
    "macos-x86_64": ("darwin", "x86_64"),
    "windows-x64-gnu": ("win32", "amd64"),
    "windows-x64-msvc": ("win32", "amd64"),
}


def verify_and_smoke(dist: Path, asset: str, candidate_sha: str) -> None:
    if asset not in HOSTS:
        raise ReleaseError(f"unknown smoke target {asset}")
    if (sys.platform, platform.machine().lower()) != HOSTS[asset]:
        raise ReleaseError(f"{asset} cannot run on {sys.platform}/{platform.machine()}")
    info = json.loads((dist / "info.json").read_text(encoding="utf-8"))
    if info.get("candidate_sha") != candidate_sha:
        raise ReleaseError("preflight candidate SHA differs from checkout")
    records = [
        row for row in info["artifacts"] if row["name"].startswith(f"mimalloc-pprof-{asset}-")
    ]
    if len(records) != 1:
        raise ReleaseError(f"expected one {asset} archive in info.json")
    record = records[0]
    archive = dist / record["name"]
    raw = archive.read_bytes()
    if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ReleaseError(f"{asset} archive differs from info.json")
    members = archive_members(archive)
    validated = record["validated"]
    binary_name = validated["binary"]
    binary = members[binary_name]
    digest = hashlib.sha256(binary).hexdigest()
    if digest != validated["binary_sha256"]:
        raise ReleaseError(f"{asset} library differs from info.json")
    with tempfile.TemporaryDirectory(prefix="mimalloc-release-smoke-") as temp:
        directory = Path(temp)
        library_path = directory / Path(binary_name).name
        library_path.write_bytes(binary)
        if asset == "windows-x64-gnu":
            runtime = "bin/libgcc_s_seh-1.dll"
            (directory / Path(runtime).name).write_bytes(members[runtime])
        if sys.platform == "win32":
            with os.add_dll_directory(str(directory)):
                library = ctypes.CDLL(str(library_path))
                allocation_smoke(library)
        else:
            library = ctypes.CDLL(str(library_path))
            allocation_smoke(library)
    print(f"{asset}: allocated and freed via packaged {binary_name}, sha256={digest}")


def allocation_smoke(library: ctypes.CDLL) -> None:
    library.mi_malloc.argtypes = [ctypes.c_size_t]
    library.mi_malloc.restype = ctypes.c_void_p
    library.mi_free.argtypes = [ctypes.c_void_p]
    library.mi_free.restype = None
    pointer = library.mi_malloc(4096)
    if not pointer:
        raise ReleaseError("mi_malloc returned NULL")
    ctypes.memset(pointer, 0xA5, 4096)
    library.mi_free(pointer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args()
    try:
        verify_and_smoke(args.dist, args.asset, args.candidate_sha)
    except (ReleaseError, KeyError, OSError, ValueError) as error:
        parser.exit(1, f"release archive smoke refused: {error}\n")
