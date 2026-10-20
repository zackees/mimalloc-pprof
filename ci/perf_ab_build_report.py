#!/usr/bin/env python3
"""Preserve perf-ab linked binaries for build/layout attribution, without measuring (#585)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import perf_ab


def capture(exe: Path, destination: Path) -> dict[str, Any]:
    """Copy the actual linked children and their build evidence; never execute them."""
    destination.mkdir(parents=True)
    files: dict[str, Any] = {}
    for source in (exe, exe.with_name("perf_ab_latency"), *sorted(exe.parent.glob("*.a"))):
        target = destination / source.name
        shutil.copy2(source, target)
        files[source.name] = {
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size,
        }
        if source.suffix != ".a":
            (destination / f"{source.name}.symbols.txt").write_text(
                perf_ab.run(["nm", "-n", "--defined-only", str(source)])
            )
            (destination / f"{source.name}.disassembly.txt").write_text(
                perf_ab.run(["objdump", "-drwC", str(source)])
            )
    shutil.copy2(exe.parent / "CMakeCache.txt", destination / "CMakeCache.txt")
    return files


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "complete": False,
        "diagnostic": "build-only; no measurements or regression verdict",
        "harness_sha": perf_ab.run(["git", "rev-parse", "HEAD"], cwd=perf_ab.ROOT).strip(),
        "compiler": perf_ab.run(["cc", "--version"]),
        "linker": perf_ab.run(["ld", "--version"]),
        "cmake": perf_ab.run(["cmake", "--version"]),
        "runner": {
            key: os.environ.get(key, "")
            for key in ("ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH")
        },
        "flags": [*perf_ab.FLAGS, *perf_ab.BUILDS["plain"][0]],
        "arms": {},
    }
    metadata = args.output / "build-report.json"
    metadata.write_text(json.dumps(report, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="perf-ab-build-report-") as tmp:
        work = Path(tmp)
        try:
            for arm, ref in (("base", args.base), ("head", args.head)):
                sha = perf_ab.run(
                    ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
                    cwd=perf_ab.ROOT,
                ).strip()
                exe = perf_ab.build(arm, sha, work, "plain", [])
                report["arms"][arm] = {
                    "ref": ref,
                    "sha": sha,
                    "kind": "plain",
                    "files": capture(exe, args.output / arm),
                }
                metadata.write_text(json.dumps(report, indent=2) + "\n")
        finally:
            for tree in work.glob("src-*"):
                perf_ab.run(["git", "worktree", "remove", "--force", str(tree)], cwd=perf_ab.ROOT)
    summary = (
        "Build-only diagnostic: no workloads were run and no performance verdict was produced.\n\n"
        f"Baseline: `{report['arms']['base']['sha']}`\n\n"
        f"Head: `{report['arms']['head']['sha']}`\n\n"
        "The build artifact contains linked timing/latency binaries, static libraries, SHA-256 "
        "digests, symbols, disassembly, compiler/linker versions and CMake settings.\n"
    )
    if args.summary:
        args.summary.write_text(summary)
    print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
