#!/usr/bin/env python3
"""Check the native Mac leak control against the same runner's unmodified bundle.

This does not replace memory_gate.py's committed baseline check. New cross-built Mac
lanes have no comparable committed baseline yet; the positive control still has to
prove that MI_BENCH_INJECT_LEAK produces a clear measured increase.
"""

from __future__ import annotations

import sys
from pathlib import Path

import memory_gate


def check(normal: list[Path], leaked: list[Path]) -> None:
    expected = memory_gate.RUNS_EXPECTED
    if len(normal) != expected or len(leaked) != expected:
        raise ValueError(f"expected {expected} normal and {expected} leaked measurements")
    base, base_peaks, _ = memory_gate.load_runs([str(p) for p in normal])
    control, control_peaks, _ = memory_gate.load_runs([str(p) for p in leaked])
    if base["inject_leak"] != 0 or control["inject_leak"] != 200000:
        raise ValueError("normal/control bundles have the wrong MI_BENCH_INJECT_LEAK values")
    if base["platform"] != "macos" or control["platform"] != "macos":
        raise ValueError("measurements were not produced on macOS")
    if base["gated_metric"] != control["gated_metric"] or base["mi_pprof"] != control["mi_pprof"]:
        raise ValueError("normal/control measurement identities differ")
    threshold = base_peaks[0] * (1 + 2 * memory_gate.PEAK_TOLERANCE)
    if control_peaks[0] <= threshold:
        raise ValueError(
            f"leak control did not fire: {control_peaks[0]:.1f} MB <= {threshold:.1f} MB "
            "(twice the gate tolerance above this runner's normal minimum)"
        )
    print(f"leak control fired: {base_peaks[0]:.1f} -> {control_peaks[0]:.1f} MB")


if __name__ == "__main__":
    try:
        check(
            sorted(Path("results").glob("normal-*.json")),
            sorted(Path("results").glob("leak-*.json")),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"macOS memory positive control failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
