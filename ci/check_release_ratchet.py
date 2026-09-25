#!/usr/bin/env python3
"""Guard ci/release_ratchet.json, the release-time ratchet (#491).

`bound_ms` is the promise "freed memory that stays idle is back with the OS within bound_ms"
(at the default purge_delay); perf-ab fails a PR whose P95 release time exceeds it. This check
keeps the promise honest over time:

- it only goes down: `bound_ms` may not exceed the base revision's, unless the owner sets
  RELEASE_RATCHET_OVERRIDE=1;
- a lower bound needs evidence: `p95_release_ms` (from the PR's perf-ab table) is recorded and
  within `margin * bound_ms`;
- it is not below what the allocator itself promises: `_mi_release_bound_ms()` at the default
  purge_delay, computed from the `#define`s in include/mimalloc/types.h and the option table.

    python3 ci/check_release_ratchet.py [--base <git ref>] [--selftest]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RATCHET = "ci/release_ratchet.json"
Ratchet = dict[str, Any]


def define(text: str, name: str) -> int:
    match = re.search(rf"#define\s+{name}\s+\(?(\d+)\)?", text)
    if match is None:
        raise SystemExit(f"check_release_ratchet: {name} not found in include/mimalloc/types.h")
    return int(match.group(1))


def allocator_bound_ms(types_h: str, options_c: str) -> int:
    """`_mi_release_bound_ms()` (src/page-holes.c) at the default purge_delay."""
    match = re.search(r"\{\s*(\d+),\s*MI_OPTION_UNINIT,\s*MI_OPTION_LEGACY\(purge_delay", options_c)
    if match is None:
        raise SystemExit(
            "check_release_ratchet: the purge_delay default not found in src/options.c"
        )
    pages = max(
        define(types_h, "MI_RETIRED_RELEASE_MULT"), define(types_h, "MI_PAGE_RESERVE_RELEASE_MULT")
    )
    arena = define(types_h, "MI_ARENA_PURGE_PERIODS") * define(
        types_h, "MI_ARENA_PURGE_MULT_DEFAULT"
    )
    return max(pages, arena) * int(match.group(1)) + define(types_h, "MI_RELEASE_SLACK_MS")


def problems(head: Ratchet, base: Ratchet | None, floor_ms: int, override: bool) -> list[str]:
    out: list[str] = []
    bound = int(head["bound_ms"])
    if bound < floor_ms:
        out.append(f"bound_ms {bound} is below what the allocator promises ({floor_ms} ms)")
    if base is not None and bound > int(base["bound_ms"]) and not override:
        out.append(
            f"bound_ms went up ({base['bound_ms']} -> {bound}); only the owner may raise it (RELEASE_RATCHET_OVERRIDE=1)"
        )
    lowered = base is not None and bound < int(base["bound_ms"])
    p95 = head.get("p95_release_ms")
    if lowered and p95 is None:
        out.append(
            "bound_ms went down without evidence: record p95_release_ms from the PR's perf-ab table"
        )
    if p95 is not None and p95 > float(head["margin"]) * bound:
        out.append(
            f"p95_release_ms {p95} exceeds margin * bound_ms ({float(head['margin']) * bound:.0f})"
        )
    return out


def selftest() -> int:
    base = {"bound_ms": 2000, "margin": 0.9, "p95_release_ms": None}
    cases = [
        ({"bound_ms": 1500, "margin": 0.9, "p95_release_ms": 1300}, 0),  # lowered with evidence
        ({"bound_ms": 1500, "margin": 0.9, "p95_release_ms": None}, 1),  # lowered without evidence
        ({"bound_ms": 1500, "margin": 0.9, "p95_release_ms": 1400}, 1),  # evidence over the margin
        ({"bound_ms": 2500, "margin": 0.9, "p95_release_ms": None}, 1),  # raised
        (
            {"bound_ms": 1000, "margin": 0.9, "p95_release_ms": 800},
            1,
        ),  # below the allocator's floor
        ({"bound_ms": 2000, "margin": 0.9, "p95_release_ms": None}, 0),  # unchanged
    ]
    failed = 0
    for head, want in cases:
        got = len(problems(head, base, 1200, override=False))
        if (got > 0) != (want > 0):
            print(f"selftest FAILED: {head} -> {got} problem(s), expected {want}")
            failed += 1
    if (
        problems(
            {"bound_ms": 2500, "margin": 0.9, "p95_release_ms": None}, base, 1200, override=True
        )
        != []
    ):
        print("selftest FAILED: the owner override does not allow raising")
        failed += 1
    types_h = "#define MI_RETIRED_RELEASE_MULT (10)\n#define MI_PAGE_RESERVE_RELEASE_MULT (10)\n#define MI_ARENA_PURGE_PERIODS (2)\n#define MI_ARENA_PURGE_MULT_DEFAULT (4)\n#define MI_RELEASE_SLACK_MS (300)\n"
    options_c = "{ 100, MI_OPTION_UNINIT, MI_OPTION_LEGACY(purge_delay,reset_delay) },"
    if allocator_bound_ms(types_h, options_c) != 1300:
        print("selftest FAILED: allocator_bound_ms")
        failed += 1
    print("selftest ok" if failed == 0 else f"selftest: {failed} failure(s)")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base", help="git ref holding the previous ratchet (omit: no history check)"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    head = json.loads((ROOT / RATCHET).read_text())
    base = None
    if args.base:
        shown = subprocess.run(
            ["git", "show", f"{args.base}:{RATCHET}"], cwd=ROOT, capture_output=True, text=True
        )
        base = json.loads(shown.stdout) if shown.returncode == 0 else None  # (no ratchet there yet)
    floor_ms = allocator_bound_ms(
        (ROOT / "include/mimalloc/types.h").read_text(), (ROOT / "src/options.c").read_text()
    )
    found = problems(head, base, floor_ms, os.environ.get("RELEASE_RATCHET_OVERRIDE") == "1")
    for problem in found:
        print(f"check_release_ratchet: {problem}")
    if not found:
        print(
            f"ok: release bound {head['bound_ms']} ms (allocator floor {floor_ms} ms, base {base['bound_ms'] if base else '-'} ms)"
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
