#!/usr/bin/env python3
"""Assert the fork scales like the allocators it is measured against (#371 layer 3).

#371's regression published a chart in which `mimalloc-pprof` gained 1.30x from eight
threads on the tiny hot path while upstream mimalloc, Bun's fork and jemalloc all gained
~2.60x -- and it stayed that way for eleven days, through a release and a daily benchmark
job, because nothing ever looked at the numbers. The data to catch it was already being
published: `speedup_vs_single_worker` is recorded per allocator, per pattern, per thread
point, in every run.

This compares the fork against the OTHER MIMALLOCS IN THE SAME RUN, which is what makes it
usable as a gate on a hosted runner: absolute throughput varies with whatever else the
machine is doing, but every row in one run met the same conditions, so a ratio between them
is not noise in the way a rate is.

It runs AFTER publication on purpose. A parity failure means the fork got slower, not that
the measurement is untrustworthy -- suppressing the chart would hide exactly the evidence
someone needs. So the site publishes and this then fails loudly.

Usage:
    check_scaling_parity.py --latest <latest.json> [--min-ratio 0.75] [--pattern ...]
    check_scaling_parity.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

#: The fork, and the rows it must keep up with. tcmalloc and jemalloc are deliberately not
#: references here: they are different allocators with their own scaling characteristics,
#: whereas the other two mimallocs share this one's design and are the honest comparison.
FORK = "mimalloc-pprof"
REFERENCES = ("upstream-mimalloc", "bun-mimalloc")

#: Patterns where scaling is the point of the measurement.
DEFAULT_PATTERNS = ("sparse-tiny-hot", "sparse-mixed-general")

#: The fork must reach this fraction of the best reference's speedup. Wide on purpose: the
#: regression produced 1.30x against 2.60x (a ratio of 0.50), and a healthy build measures
#: within a few percent, so 0.75 fails the regression by a distance while tolerating an
#: unlucky run.
DEFAULT_MIN_RATIO = 0.75


def cells(scaling: Mapping[str, object]) -> list[dict[str, object]]:
    summaries = scaling.get("cell_summaries")
    if not isinstance(summaries, list):
        return []
    return [
        cast("dict[str, object]", c) for c in cast("list[object]", summaries) if isinstance(c, dict)
    ]


def top_thread_speedups(
    scaling: Mapping[str, object], pattern: str
) -> tuple[int, dict[str, float]]:
    """(highest thread point, {allocator: speedup}) for one pattern."""
    rows = [c for c in cells(scaling) if c.get("pattern") == pattern]
    if not rows:
        return 0, {}
    top = max(int(cast("int", c["thread_count"])) for c in rows)
    out: dict[str, float] = {}
    for c in rows:
        if int(cast("int", c["thread_count"])) != top:
            continue
        allocator = c.get("allocator_id")
        speedup = c.get("speedup_vs_single_worker")
        if isinstance(allocator, str) and isinstance(speedup, (int, float)):
            out[allocator] = float(speedup)
    return top, out


def check(latest: Mapping[str, object], patterns: tuple[str, ...], min_ratio: float) -> int:
    scaling = latest.get("scaling")
    if not isinstance(scaling, dict):
        print("check_scaling_parity: no scaling section in this report; nothing to compare")
        return 0
    problems: list[str] = []
    checked = 0
    for pattern in patterns:
        top, speedups = top_thread_speedups(cast("Mapping[str, object]", scaling), pattern)
        if not speedups:
            continue
        fork = speedups.get(FORK)
        refs = {name: speedups[name] for name in REFERENCES if name in speedups}
        if fork is None or not refs:
            continue
        best_name, best = max(refs.items(), key=lambda item: item[1])
        ratio = fork / best if best > 0 else 0.0
        checked += 1
        verdict = "ok" if ratio >= min_ratio else "FAIL"
        print(
            f"  {pattern} @{top} threads: {FORK} {fork:.2f}x vs {best_name} {best:.2f}x "
            f"-> {ratio:.2f} of it  [{verdict}]"
        )
        if ratio < min_ratio:
            problems.append(
                f"{pattern}: {FORK} gains only {fork:.2f}x from {top} threads while "
                f"{best_name} gains {best:.2f}x ({ratio:.2f} of it, floor {min_ratio:.2f})"
            )
    if not checked:
        print("check_scaling_parity: no comparable cells found; nothing asserted")
        return 0
    if problems:
        print("\ncheck_scaling_parity: the fork is not scaling like the mimallocs beside it\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\n  This is the #371 shape: work done on every allocation that touches memory "
            "shared\n  between threads. See MI_OBSERVERS_INITIAL in "
            "include/mimalloc/internal.h, and\n  test-observer-scaling for the same "
            "assertion at ctest scale.",
        )
        return 1
    print(f"check_scaling_parity: PASS ({checked} pattern(s) at parity)")
    return 0


def selftest() -> int:
    """The regression, and a healthy run, on synthetic sections."""
    ok = True

    def section(fork_speedup: float) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for allocator, speedup in (
            (FORK, fork_speedup),
            ("upstream-mimalloc", 2.60),
            ("bun-mimalloc", 2.58),
        ):
            for threads in (1, 8):
                rows.append(
                    {
                        "pattern": "sparse-tiny-hot",
                        "allocator_id": allocator,
                        "thread_count": threads,
                        "speedup_vs_single_worker": (1.0 if threads == 1 else speedup),
                        "median_throughput": 1.0,
                    }
                )
        return {"cell_summaries": rows}

    regressed = {"scaling": section(1.30)}
    if check(regressed, ("sparse-tiny-hot",), DEFAULT_MIN_RATIO) != 1:
        print("FAIL: the published regression (1.30x vs 2.60x) was not caught")
        ok = False
    healthy = {"scaling": section(2.55)}
    if check(healthy, ("sparse-tiny-hot",), DEFAULT_MIN_RATIO) != 0:
        print("FAIL: a healthy run was flagged")
        ok = False
    # A run with no scaling section must not fail: sections are carried forward and a
    # metric that has not been measured at these pins is legitimately absent (#376).
    if check({}, ("sparse-tiny-hot",), DEFAULT_MIN_RATIO) != 0:
        print("FAIL: a report with no scaling section was treated as a regression")
        ok = False
    print("check_scaling_parity --selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=Path)
    parser.add_argument("--min-ratio", type=float, default=DEFAULT_MIN_RATIO)
    parser.add_argument("--pattern", action="append", dest="patterns")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.latest is None:
        parser.error("--latest is required unless --selftest")
    latest = json.loads(Path(cast("Path", args.latest)).read_text(encoding="utf-8"))
    patterns = tuple(cast("list[str]", args.patterns) or DEFAULT_PATTERNS)
    return check(latest, patterns, float(cast("float", args.min_ratio)))


if __name__ == "__main__":
    sys.exit(main())
