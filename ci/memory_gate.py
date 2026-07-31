#!/usr/bin/env python3
"""Compare a test-memory-gate JSON run against the committed per-platform baseline.

This is the piece that turns test-memory-gate from a self-relative assertion into an
actual regression gate: it compares against numbers recorded on a previous commit, so a
leak introduced gradually over several PRs cannot hide inside a per-run ratio.

Why per-platform baselines and never a shared one: lazy zeroing means Linux RSS and
Windows commit report different numbers for identical allocator behavior, and macOS
compresses memory so its resident figure falls under pressure while a leak grows. Only
same-OS deltas are meaningful.

Peak memory is *not* a low-variance signal, so both commands take several runs of the
same binary and use the **minimum** observed peak. Noise on a shared CI runner only ever
adds memory (another tenant's page cache, a straggler thread still winding down), so the
minimum is the estimator closest to the allocator's true high-water mark, and it is far
more stable than any single run. The spread across runs is printed every time -- if it
ever approaches the tolerance, the tolerance is wrong and the printout says so.

Usage:
    memory_gate.py check   <result.json> [more.json ...]
    memory_gate.py update  <result.json> [more.json ...]   # deliberate, reviewed act
    memory_gate.py control <result.json> [more.json ...]   # positive control: must FAIL

Exit codes: 0 pass, 1 regression, 2 usage/IO error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

BASELINE_DIR = Path(__file__).resolve().parent / "memory-baselines"


class Counters(TypedDict):
    """Exact allocator counters emitted by test-memory-gate.c, not statistical."""

    threads_start: int
    threads_end: int
    theaps_start: int
    theaps_end: int
    pages_start: int
    pages_end: int


class Result(TypedDict):
    """One run of test-memory-gate. Mirrors the JSON that MI_BENCH_JSON writes.

    Declared rather than left as dict[str, Any] because every field here is indexed by
    string literal below. A typo in one of those keys would previously have been a
    runtime KeyError inside a CI gate -- i.e. a gate that fails for the wrong reason,
    or (worse, on a path that catches it) one that passes without checking anything.
    """

    schema: int
    platform: str
    gated_metric: str
    mi_pprof: int
    inject_leak: int
    peak_mb: float
    peak_rss_mb: float
    peak_commit_mb: float
    profiler_arena_mb: float
    peak_minus_profiler_mb: float
    purged_gb: float
    counters: Counters


# Peak memory tolerance, applied to the minimum of RUNS_EXPECTED runs.
#
# An earlier version of this file asserted that memory was "the low-variance signal here
# (the gated numbers are kernel high-water marks, not sampled)" and set this to 0.10.
# That was an assumption, and measuring disproved it: repeated runs of the same binary
# span roughly 20% peak-to-peak. Taking the minimum of several runs cuts that to a few
# percent, which is what makes a tight threshold defensible rather than flaky.
#
# This number is set from the observed spread reported by the runs below, not guessed.
# Raise it only with the measurement that justifies it; a gate that flakes gets ignored,
# and an ignored gate is worse than none.
PEAK_TOLERANCE = 0.15

# Number of runs the CI job is expected to supply. Fewer is allowed (with a warning) so
# the script stays usable locally, but the committed baselines are min-of-N.
RUNS_EXPECTED = 4

# Counters are exact, not statistical. A thread that was created and joined must not
# still be live; anything above this is cleanup that did not run. Matches the inline
# assertion in test-memory-gate.c.
MAX_LIVE_THREADS = 32


def baseline_path(result: Result) -> Path:
    # MI_PPROF changes the legitimate peak (rule 4 puts the profiler arena in process
    # memory), so ON and OFF get separate baselines rather than one padded threshold.
    return BASELINE_DIR / "{}-pprof{}.json".format(result["platform"], result["mi_pprof"])


def load(path: str | Path) -> Result:
    with Path(path).open(encoding="utf-8") as f:
        # json.load returns Any; this cast is the single point where untyped external
        # data becomes typed, so the schema assumption is stated once and visibly.
        return cast(Result, json.load(f))


def load_runs(paths: list[str]) -> tuple[Result, list[float], float]:
    """Load N runs of the same configuration and return (representative, peak_mb, spread%).

    The representative record is the run with the minimum gated peak; its counters are
    the ones checked, so the counter assertions and the peak refer to the same run.
    """
    runs = [load(p) for p in paths]
    keys = {(r["platform"], r["mi_pprof"], r["gated_metric"]) for r in runs}
    if len(keys) != 1:
        raise ValueError(f"runs are not from the same configuration: {sorted(keys)}")
    peaks = sorted(r["peak_mb"] for r in runs)
    best = min(runs, key=lambda r: r["peak_mb"])
    spread = (100.0 * (peaks[-1] - peaks[0]) / peaks[0]) if peaks[0] else 0.0
    return best, peaks, spread


def report_runs(peaks: list[float], spread: float) -> None:
    print("  {:<22} {}".format("runs (MB)", "  ".join(f"{p:.1f}" for p in peaks)))
    print(
        "  {:<22} {:.1f}%  (min is used; noise on a shared runner only adds memory)".format(
            "spread across runs", spread
        )
    )
    if len(peaks) < RUNS_EXPECTED:
        print(
            f"  WARNING: only {len(peaks)} run(s); the committed baselines are min-of-{RUNS_EXPECTED}. "
            "A single run is not comparable."
        )
    if spread >= 100.0 * PEAK_TOLERANCE:
        print(
            f"  WARNING: observed spread ({spread:.1f}%) is at or above the tolerance "
            f"({100.0 * PEAK_TOLERANCE:.0f}%). The threshold cannot distinguish a regression from noise -- "
            "raise RUNS_EXPECTED or the tolerance, with this measurement as the "
            "justification."
        )


def control(result_paths: list[str]) -> int:
    """Positive control: require the gate to FAIL on a deliberately leaky build.

    A gate that has never been observed to fire proves nothing -- it can be silently
    broken for months and every run still looks green. So CI builds a copy with
    MI_BENCH_INJECT_LEAK, runs it through the very same comparison, and this command
    inverts the verdict: exit 0 only if `check` would have failed.

    Requiring inject_leak to be *set* is what keeps this from being usable as an escape
    hatch -- it cannot be pointed at a real regression to turn it green.
    """
    result, _, _ = load_runs(result_paths)
    if not result.get("inject_leak", 0):
        print(
            "REFUSING: positive control requires a build with MI_BENCH_INJECT_LEAK "
            "set. This run has none, so a failure here would mean a real regression, "
            "not a working control."
        )
        return 2

    print("=== positive control: the gate is expected to FAIL below ===")
    rc = check(result_paths, _control=True)
    if rc == 1:
        print("\nPASS (control): the gate detected the injected leak.")
        return 0
    print(f"\nFAIL (control): the gate did NOT detect an injected leak (rc={rc}).")
    print("The gate is not working. A green `check` on a real build means nothing")
    print("until this passes.")
    return 1


def check(result_paths: list[str], _control: bool = False) -> int:
    result, peaks, spread = load_runs(result_paths)

    if result.get("inject_leak", 0) and not _control:
        # An injected-leak run is a positive control; it is expected to fail and must
        # never be mistaken for a real result or used to re-baseline.
        print(
            "REFUSING: this run has MI_BENCH_INJECT_LEAK set and is a positive "
            "control, not a measurement. Use `memory_gate.py control` for that."
        )
        return 2

    bpath = baseline_path(result)
    if not bpath.exists():
        print(f"No baseline at {bpath}.")
        report_runs(peaks, spread)
        print("This platform/config has never been recorded. Create it with:")
        print("    python ci/memory_gate.py update {}".format(" ".join(result_paths)))
        return 2
    base = load(bpath)

    failures: list[str] = []
    metric = result["gated_metric"]
    peak, base_peak = peaks[0], base["peak_mb"]
    allowed = base_peak * (1.0 + PEAK_TOLERANCE)

    print(
        "platform={} metric={} mi_pprof={}".format(result["platform"], metric, result["mi_pprof"])
    )
    print(
        f"  {metric:<22} {peak:>9.1f} MB   baseline {base_peak:>9.1f} MB   allowed {allowed:>9.1f} MB"
    )
    print(
        "  {:<22} {:>9.1f} MB   (reported so the profiler's own arena is not "
        "mistaken for allocator growth)".format(
            "profiler arena", result.get("profiler_arena_mb", 0.0)
        )
    )
    report_runs(peaks, spread)

    if peak > allowed:
        failures.append(
            "{} regressed: {:.1f} MB vs baseline {:.1f} MB (+{:.1f}%, allowed +{:.0f}%)".format(
                metric,
                peak,
                base_peak,
                100.0 * (peak - base_peak) / base_peak if base_peak else float("inf"),
                100.0 * PEAK_TOLERANCE,
            )
        )

    c = result["counters"]
    print(
        "  counters: threads {}->{}  theaps {}->{}  pages {}->{}".format(
            c["threads_start"],
            c["threads_end"],
            c["theaps_start"],
            c["theaps_end"],
            c["pages_start"],
            c["pages_end"],
        )
    )

    if c["threads_end"] >= MAX_LIVE_THREADS:
        failures.append(
            "{} threads still live after the run (limit {}). Thread-exit cleanup is "
            "not running -- this is the signature of #44 / #47.".format(
                c["threads_end"], MAX_LIVE_THREADS
            )
        )

    if failures:
        print("\nFAIL")
        for f in failures:
            print(f"  - {f}")
        print("\nIf this growth is intentional, re-baseline deliberately:")
        print("    python ci/memory_gate.py update {}".format(" ".join(result_paths)))
        print("and say why in the PR. Do not re-baseline to make a red gate green.")
        return 1

    print("\nPASS")
    return 0


def update(result_paths: list[str]) -> int:
    result, peaks, spread = load_runs(result_paths)
    if result.get("inject_leak", 0):
        print("REFUSING to baseline a run with MI_BENCH_INJECT_LEAK set.")
        return 2
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    bpath = baseline_path(result)
    # Record how the number was obtained, so a later reader can tell whether a baseline
    # is comparable to the current methodology rather than having to guess.
    record = dict(result)
    record["peak_mb"] = peaks[0]
    record["baseline_runs"] = len(peaks)
    record["baseline_spread_pct"] = round(spread, 1)
    with bpath.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {bpath} (min peak {peaks[0]:.1f} MB of {len(peaks)} runs)")
    report_runs(peaks, spread)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("check", "update", "control"):
        print(__doc__)
        return 2
    try:
        cmd = {"check": check, "update": update, "control": control}[argv[1]]
        return cmd(argv[2:])
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
