#!/usr/bin/env python3
"""Decide whether a PR gets `c-unit.yml`'s minimal-lane memory gate (#518).

#501 raised the memory gate's peak from 58.2 to 61-63.7 MB and nobody saw it until after
merge (#514): internal PRs run the minimal lane, and the gate only ran in the `ci-test` /
`ci-full` DAG. So every PR whose diff can move the allocator's memory -- `src/`,
`include/`, `CMakeLists.txt`, or the gate's own machinery -- runs the gate whatever its
labels. The gate is ~2 s a run; building its one target is the whole cost.

`run=true` means "the minimal `memory-gate` job must run". It is `false` when the full
lane is selected (a literal `ci-test` or `ci-full` label, or an external author): that
lane's `run-linux` already runs the same gate plus its leak control, so a second copy
would only cost a runner.

The path rule mirrors `ci/macos_lane_decide.py` (#339): the diff comes from
`git diff --name-only base...head`, the labels from the event payload. Prints `run=` and
`reason=`, and appends them to `$GITHUB_OUTPUT` when set.

    python3 ci/memory_gate_lane_decide.py --base <sha> --head <sha> \
        [--labels a,b] [--external true|false]
    python3 ci/memory_gate_lane_decide.py --selftest
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

#: Labels that select the full `c-unit` DAG, whose `run-linux` runs the gate already.
FULL_LANE_LABELS: tuple[str, ...] = ("ci-test", "ci-full")

#: Paths whose change can move the allocator's memory, or the gate that measures it.
#: Matched with `fnmatch` against repo-relative paths (`*` crosses directory separators).
MEMORY_GATE_PATHS: tuple[str, ...] = (
    "src/*",
    "include/*",
    "CMakeLists.txt",
    "test/test-memory-gate.c",
    "ci/memory_gate.py",
    "ci/memory-baselines/*",
    "ci/memory_gate_lane_decide.py",
    ".github/workflows/c-unit.yml",
)


def touched_gate_paths(files: list[str]) -> list[str]:
    return sorted(f for f in files if any(fnmatch.fnmatch(f, g) for g in MEMORY_GATE_PATHS))


def decide(files: list[str], labels: list[str], external: bool) -> tuple[bool, str]:
    full = [label for label in FULL_LANE_LABELS if label in labels]
    if full or external:
        why = f"label `{full[0]}`" if full else "external author"
        return False, f"{why} selects the full lane; its run-linux runs the memory gate"
    hits = touched_gate_paths(files)
    if hits:
        return True, f"memory-gate paths changed: {hits}"
    return False, "no src/, include/, CMakeLists.txt or memory-gate path changed"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def selftest() -> int:
    assert decide(["src/arena.c"], [], False)[0]
    assert decide(["include/mimalloc/types.h"], [], False)[0]
    assert decide(["CMakeLists.txt"], [], False)[0]
    assert decide(["test/test-memory-gate.c"], [], False)[0]
    assert not decide(["docs/ci-gates.md", "README.md"], [], False)[0]
    assert not decide(["rust/mimalloc-pprof/src/lib.rs"], [], False)[0]
    assert not decide(["srcdoc/x.md", "includes.txt"], [], False)[0]
    assert not decide(["src/arena.c"], ["ci-test"], False)[0]
    assert not decide(["src/arena.c"], ["ci-full"], False)[0]
    assert not decide(["src/arena.c"], [], True)[0]
    print("memory_gate_lane_decide selftest OK")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--labels", default="", help="comma-separated PR labels")
    parser.add_argument("--external", choices=("true", "false"), default="false")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.base:
        parser.error("--base is required")
    files = [f for f in _git("diff", "--name-only", f"{args.base}...{args.head}").splitlines() if f]
    labels = [x.strip() for x in str(args.labels).split(",") if x.strip()]
    run, reason = decide(files, labels, args.external == "true")
    print(f"run={'true' if run else 'false'}")
    print(f"reason={reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with Path(out).open("a", encoding="utf-8") as fh:
            fh.write(f"run={'true' if run else 'false'}\nreason={reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
