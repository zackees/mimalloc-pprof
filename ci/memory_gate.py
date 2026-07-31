#!/usr/bin/env python3
"""Compare a test-memory-gate JSON run against the committed per-platform baseline.

This is the piece that turns test-memory-gate from a self-relative assertion into an
actual regression gate: it compares against numbers recorded on a previous commit, so a
leak introduced gradually over several PRs cannot hide inside a per-run ratio.

Why per-platform baselines and never a shared one: lazy zeroing means Linux RSS and
Windows commit report different numbers for identical allocator behavior, and macOS
compresses memory so its resident figure falls under pressure while a leak grows. Only
same-OS deltas are meaningful.

Usage:
    memory_gate.py check   <result.json>   # compare against the committed baseline
    memory_gate.py update  <result.json>   # re-baseline (deliberate, reviewed act)

Exit codes: 0 pass, 1 regression, 2 usage/IO error.
"""

import json
import os
import sys

BASELINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory-baselines")

# Peak memory tolerance. Deliberately tighter than a throughput threshold would be:
# memory is the low-variance signal here (the gated numbers are kernel high-water marks,
# not sampled), and this repository has shipped two memory bugs and zero throughput bugs.
PEAK_TOLERANCE = 0.10

# Counters are exact, not statistical. A thread that was created and joined must not
# still be live; anything above this is cleanup that did not run. Matches the inline
# assertion in test-memory-gate.c.
MAX_LIVE_THREADS = 32


def baseline_path(result):
    # MI_PPROF changes the legitimate peak (rule 4 puts the profiler arena in process
    # memory), so ON and OFF get separate baselines rather than one padded threshold.
    return os.path.join(
        BASELINE_DIR, "{}-pprof{}.json".format(result["platform"], result["mi_pprof"])
    )


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check(result_path):
    result = load(result_path)

    if result.get("inject_leak", 0):
        # An injected-leak run is a positive control; it is expected to fail and must
        # never be mistaken for a real result or used to re-baseline.
        print("REFUSING: this run has MI_BENCH_INJECT_LEAK set and is a positive "
              "control, not a measurement.")
        return 2

    bpath = baseline_path(result)
    if not os.path.exists(bpath):
        print("No baseline at {}.".format(bpath))
        print("This platform/config has never been recorded. Create it with:")
        print("    python ci/memory_gate.py update {}".format(result_path))
        return 2
    base = load(bpath)

    failures = []
    metric = result["gated_metric"]
    peak, base_peak = result["peak_mb"], base["peak_mb"]
    allowed = base_peak * (1.0 + PEAK_TOLERANCE)

    print("platform={} metric={} mi_pprof={}".format(
        result["platform"], metric, result["mi_pprof"]))
    print("  {:<22} {:>9.1f} MB   baseline {:>9.1f} MB   allowed {:>9.1f} MB".format(
        metric, peak, base_peak, allowed))
    print("  {:<22} {:>9.1f} MB   (reported so the profiler's own arena is not "
          "mistaken for allocator growth)".format(
              "profiler arena", result.get("profiler_arena_mb", 0.0)))

    if peak > allowed:
        failures.append(
            "{} regressed: {:.1f} MB vs baseline {:.1f} MB (+{:.1f}%, allowed +{:.0f}%)".format(
                metric, peak, base_peak,
                100.0 * (peak - base_peak) / base_peak if base_peak else float("inf"),
                100.0 * PEAK_TOLERANCE))

    c = result["counters"]
    print("  counters: threads {}->{}  theaps {}->{}  pages {}->{}".format(
        c["threads_start"], c["threads_end"], c["theaps_start"],
        c["theaps_end"], c["pages_start"], c["pages_end"]))

    if c["threads_end"] >= MAX_LIVE_THREADS:
        failures.append(
            "{} threads still live after the run (limit {}). Thread-exit cleanup is "
            "not running -- this is the signature of #44 / #47.".format(
                c["threads_end"], MAX_LIVE_THREADS))

    if failures:
        print("\nFAIL")
        for f in failures:
            print("  - {}".format(f))
        print("\nIf this growth is intentional, re-baseline deliberately:")
        print("    python ci/memory_gate.py update {}".format(result_path))
        print("and say why in the PR. Do not re-baseline to make a red gate green.")
        return 1

    print("\nPASS")
    return 0


def update(result_path):
    result = load(result_path)
    if result.get("inject_leak", 0):
        print("REFUSING to baseline a run with MI_BENCH_INJECT_LEAK set.")
        return 2
    os.makedirs(BASELINE_DIR, exist_ok=True)
    bpath = baseline_path(result)
    with open(bpath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")
    print("wrote {} (peak {:.1f} MB)".format(bpath, result["peak_mb"]))
    return 0


def main(argv):
    if len(argv) != 3 or argv[1] not in ("check", "update"):
        print(__doc__)
        return 2
    try:
        return check(argv[2]) if argv[1] == "check" else update(argv[2])
    except (OSError, ValueError, KeyError) as e:
        print("error: {}".format(e))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
