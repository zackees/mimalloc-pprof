#!/usr/bin/env python3
"""Summarise an interleaved A/B run of the zero-tracking benchmark (issue #67).

Reads the JSON-lines the benchmark emits and reports paired medians per workload.

Deliberately reports numbers rather than a verdict. The first attempt at deciding #67
locally "measured" a 13% regression on the anti-workload; re-running the same two
binaries interleaved reversed the sign. The difference was measurement order on a loaded
machine, not the feature. So: paired samples, medians, and the spread printed alongside,
so a reader can see whether the effect is larger than the noise instead of taking a
single number on faith.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast


class Sample(TypedDict):
    purge_zeroes: int
    sparse_s: float
    dense_s: float
    verify: str


# `sparse` favours the feature so strongly that quoting it alone would be misleading:
# skipping the memset also skips faulting the pages in, so it measures lazy commit as
# much as zeroing. `dense` is the anti-workload and is the one that decides adoption.
# Accessors rather than string keys: indexing a TypedDict with a loop variable erases
# the field's type, which would defeat the point of declaring the schema at all.
WORKLOADS: tuple[tuple[Callable[[Sample], float], str], ...] = (
    (lambda s: s["sparse_s"], "sparse (favourable: one byte touched per block)"),
    (lambda s: s["dense_s"], "dense  (ANTI-WORKLOAD: every byte touched)"),
)


def summarise(samples: list[Sample]) -> int:
    off = [s for s in samples if s["purge_zeroes"] == 0]
    on = [s for s in samples if s["purge_zeroes"] == 1]
    if not off or not on:
        print(f"error: need samples from both arms (got {len(off)} off, {len(on)} on)")
        return 2

    print(f"paired samples: {len(off)} off, {len(on)} on\n")
    for field, label in WORKLOADS:
        a = sorted(field(s) for s in off)
        b = sorted(field(s) for s in on)
        ma, mb = statistics.median(a), statistics.median(b)
        change = 100.0 * (mb - ma) / ma if ma else 0.0
        spread_a = 100.0 * (a[-1] - a[0]) / a[0] if a[0] else 0.0
        spread_b = 100.0 * (b[-1] - b[0]) / b[0] if b[0] else 0.0
        verdict = "faster" if change < 0 else "slower"
        print(label)
        print(f"  off  median {ma:.4f}s   range {a[0]:.4f}-{a[-1]:.4f}  (spread {spread_a:.1f}%)")
        print(f"  on   median {mb:.4f}s   range {b[0]:.4f}-{b[-1]:.4f}  (spread {spread_b:.1f}%)")
        print(f"  -> {change:+.1f}% ({verdict})")
        # An effect smaller than the noise in either arm is not an effect.
        if abs(change) < max(spread_a, spread_b):
            print("     NOTE: |change| is smaller than the within-arm spread; this is")
            print("     not distinguishable from noise at this sample count.")
        print()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    samples: list[Sample] = []
    for line in Path(argv[1]).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            samples.append(cast(Sample, json.loads(line)))
    if not samples:
        print("error: no JSON samples found")
        return 2
    if any(s["verify"] != "ok" for s in samples):
        print("error: a run reported failed verification")
        return 1
    return summarise(samples)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
