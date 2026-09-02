#!/usr/bin/env python3
"""Verify the publishable archive, not the maintainer checkout."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


def member_text(archive: tarfile.TarFile, suffix: str) -> str:
    matches = [member for member in archive.getmembers() if member.name.endswith(suffix)]
    if len(matches) != 1:
        raise AssertionError(f"expected one {suffix!r} member, found {len(matches)}")
    stream = archive.extractfile(matches[0])
    if stream is None:
        raise AssertionError(f"cannot read {matches[0].name}")
    return stream.read().decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()

    archive_path = args.archive
    if archive_path.is_dir():
        matches = list(archive_path.rglob("mimalloc-pprof-0.9.5.crate"))
        if len(matches) != 1:
            raise AssertionError(f"expected one packaged archive, found {len(matches)}")
        archive_path = matches[0]

    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = member_text(archive, "/Cargo.toml.orig")
        build = member_text(archive, "/build.rs")
        library = member_text(archive, "/src/lib.rs")
        native = member_text(archive, "/vendor/mimalloc-pprof-amalgamated.c")

    required_manifest = ('version = "0.9.5"', 'default = ["pprof"]', "pprof = []")
    for text in required_manifest:
        if text not in manifest:
            raise AssertionError(f"published manifest missing {text!r}")
    if 'var_os("CARGO_FEATURE_PPROF")' not in build or '"MI_PPROF"' not in build:
        raise AssertionError("published build script does not select MI_PPROF from the feature")
    if "pub mod dhat" not in library or "mi_dhat_start" not in native:
        raise AssertionError("published archive lost internal DHAT")

    print(f"verified publish archive: {archive_path}")


if __name__ == "__main__":
    main()
