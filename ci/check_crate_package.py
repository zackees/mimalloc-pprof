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


def crate_version(manifest: Path) -> str:
    in_package = False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_package = stripped == "[package]"
            continue
        if in_package and stripped.startswith("version"):
            return stripped.split("=", 1)[1].strip().strip('"')
    raise AssertionError(f"no [package] version in {manifest}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()

    archive_path = args.archive
    if archive_path.is_dir():
        # Resolve the crate version from the manifest instead of hard-coding it, so a
        # release bump cannot make this check look for an archive that no longer exists.
        version = crate_version(
            Path(__file__).resolve().parent.parent / "rust" / "mimalloc-pprof" / "Cargo.toml"
        )
        matches = list(archive_path.rglob(f"mimalloc-pprof-{version}.crate"))
        if len(matches) != 1:
            raise AssertionError(f"expected one packaged archive, found {len(matches)}")
        archive_path = matches[0]

    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = member_text(archive, "/Cargo.toml.orig")
        build = member_text(archive, "/build.rs")
        library = member_text(archive, "/src/lib.rs")
        native = member_text(archive, "/vendor/mimalloc-pprof-amalgamated.c")

    version = crate_version(
        Path(__file__).resolve().parent.parent / "rust" / "mimalloc-pprof" / "Cargo.toml"
    )
    required_manifest = (f'version = "{version}"', 'default = ["pprof"]', "pprof = []")
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
