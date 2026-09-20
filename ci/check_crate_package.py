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
    required_manifest = (
        f'version = "{version}"',
        # #414 (owner decision 2026-09-19): EVERY observability subsystem is opt-in, so
        # the published default feature set must be empty -- a client that asks for the
        # allocator gets the allocator and pays for nothing else. Each subsystem must
        # still exist as a feature (otherwise opting in is impossible), `dhat` must imply
        # `memory-events` (it dispatches through those hook sites), and `full` must name
        # all five (it is the documented way to restore the pre-0.12 behaviour).
        "default = []",
        "pprof = []",
        "memory-events = []",
        "diagnostics = []",
        'dhat = ["memory-events"]',
        "owner-gate = []",
        'full = ["pprof", "memory-events", "diagnostics", "dhat", "owner-gate"]',
    )
    for text in required_manifest:
        if text not in manifest:
            raise AssertionError(f"published manifest missing {text!r}")
    # Every one of the five must reach the C compiler from its cargo feature. A hard-coded
    # define would either compile the subsystem into every client (defeating opt-in) or
    # make opting in a silent no-op.
    for feature_env, define in (
        ("CARGO_FEATURE_PPROF", "MI_PPROF"),
        ("CARGO_FEATURE_MEMORY_EVENTS", "MI_MEMEVT"),
        ("CARGO_FEATURE_DIAGNOSTICS", "MI_DIAGNOSTICS"),
        ("CARGO_FEATURE_DHAT", "MI_DHAT"),
        ("CARGO_FEATURE_OWNER_GATE", "MI_OWNER_GATE"),
    ):
        if f'var_os("{feature_env}")' not in build or f'"{define}"' not in build:
            raise AssertionError(
                f"published build script does not select {define} from its cargo feature"
            )
    if "pub mod dhat" not in library or "mi_dhat_start" not in native:
        raise AssertionError("published archive lost internal DHAT")
    # #414: the API must be present in EVERY configuration, so the published sources must
    # still carry the stub halves the compiled-out builds link against.
    if "pub mod memory_events" not in library or "mi_memory_tracking_set_enabled" not in native:
        raise AssertionError("published archive lost the memory-events surface")
    if "pub fn heap_snapshot_to_file" not in library or "mi_heap_dump_json" not in native:
        raise AssertionError("published archive lost the diagnostics surface")

    print(f"verified publish archive: {archive_path}")


if __name__ == "__main__":
    main()
