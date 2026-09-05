#!/usr/bin/env python3
"""Decide whether a PR gets the selective macOS Recovery lane (#339).

The full Recovery execution stays manual (owner decision 2026-09-03: 25-90 min cannot
gate a PR). This picks the PRs that get a *selective* run -- boot the guest, execute only
the tests labelled `macos` -- when either:

  * the diff touches a Darwin-specific path (the same list CLAUDE.md tells humans to run
    the manual lane for: `src/prim/osx`, interpose, the TLS slots), or
  * the PR carries the `needs-macos` label (manual opt-in).

Both are dependency-free: the diff comes from `git diff --name-only`, the labels from
the event payload. Prints `run=true|false` and `reason=...`, and appends them to
`$GITHUB_OUTPUT` when set.

    python3 ci/macos_lane_decide.py --base <sha> --head <sha> [--labels a,b]
    python3 ci/macos_lane_decide.py --selftest
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

OPT_IN_LABEL = "needs-macos"

#: Paths whose change means "this is a macOS change". Globs are matched against the
#: repo-relative path with `fnmatch` (so `*` crosses directory separators).
MACOS_PATHS: tuple[str, ...] = (
    "src/prim/osx/*",
    "test/test-osx-*",
    "include/mimalloc/prim-tls.h",
    "cmake/toolchains/*apple*",
    ".github/workflows/macos-bundles.yml",
    "ci/recovery_expected_failures.py",
    "ci/check_macos_labels.py",
    "ci/macos_lane_decide.py",
)

#: Substrings that make a CMakeLists.txt change count (the zone/interpose blocks).
CMAKE_MARKERS: tuple[str, ...] = ("MI_OSX_ZONE", "MI_OSX_INTERPOSE", "alloc-override-zone")


def touched_macos_paths(files: list[str]) -> list[str]:
    return sorted(f for f in files if any(fnmatch.fnmatch(f, g) for g in MACOS_PATHS))


def cmake_touches_macos(diff_text: str) -> bool:
    """True if a changed CMakeLists.txt line mentions the zone/interpose machinery."""
    for line in diff_text.splitlines():
        changed = line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        if changed and any(marker in line for marker in CMAKE_MARKERS):
            return True
    return False


def decide(files: list[str], labels: list[str], cmake_diff: str = "") -> tuple[bool, str]:
    if OPT_IN_LABEL in labels:
        return True, f"label `{OPT_IN_LABEL}`"
    hits = touched_macos_paths(files)
    if hits:
        return True, f"macOS paths changed: {hits}"
    if "CMakeLists.txt" in files and cmake_touches_macos(cmake_diff):
        return True, "CMakeLists.txt zone/interpose block changed"
    return False, "no macOS path or label"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def selftest() -> int:
    assert decide([], [OPT_IN_LABEL]) == (True, f"label `{OPT_IN_LABEL}`")
    ok, why = decide(["src/prim/osx/alloc-override-zone.c"], [])
    assert ok and "src/prim/osx" in why, why
    assert decide(["test/test-osx-zone-introspect.c"], [])[0]
    assert decide(["include/mimalloc/prim-tls.h"], [])[0]
    assert not decide(["src/alloc.c", "rust/x.rs"], ["bug"])[0]
    assert not decide(["CMakeLists.txt"], [], "+set(FOO 1)\n")[0]
    assert decide(["CMakeLists.txt"], [], "+  list(APPEND x src/prim/osx/alloc-override-zone.c)")[0]
    assert not decide(["CMakeLists.txt"], [], "--- a\n+++ b\n MI_OSX_ZONE context line")[0]
    print("macos_lane_decide selftest OK")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--labels", default="", help="comma-separated PR labels")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.base:
        parser.error("--base is required")
    base = str(args.base)
    head = str(args.head)
    files = [f for f in _git("diff", "--name-only", f"{base}...{head}").splitlines() if f]
    cmake_diff = (
        _git("diff", f"{base}...{head}", "--", "CMakeLists.txt")
        if "CMakeLists.txt" in files
        else ""
    )
    labels = [x.strip() for x in str(args.labels).split(",") if x.strip()]
    run, reason = decide(files, labels, cmake_diff)
    print(f"run={'true' if run else 'false'}")
    print(f"reason={reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with Path(out).open("a", encoding="utf-8") as fh:
            fh.write(f"run={'true' if run else 'false'}\nreason={reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
