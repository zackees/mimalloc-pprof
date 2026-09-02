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
    memory_gate.py check   [<result.json> ...]   # no paths: build/run the binary itself
    memory_gate.py update  <result.json> [more.json ...]   # deliberate, reviewed act
    memory_gate.py control <result.json> [more.json ...]   # positive control: must FAIL

`--arch <name>` and `--compiler <name>` (accepted by all three commands) declare the
toolchain identity the binary cannot know about itself, and become part of the baseline
file's name. #277 phase B needs them: an arm64 macOS run from a soldr-cross-built bundle
and an arm64 macOS run from an Xcode-built tree are the same `platform` on the same
`macos-latest` runner, and comparing one against the other's committed peak would be a
cross-toolchain comparison dressed as a regression check. Omitting both keeps the legacy
`<platform>-pprof<N>.json` name, so no existing baseline moves.

With no JSON paths, `check` locates the built `mimalloc-test-memory-gate` binary,
runs it RUNS_EXPECTED times (see run_gate_binary), and checks those results -- this
is what makes `python ci/memory_gate.py check` alone reproduce the CI job locally.

Exit codes: 0 pass, 1 regression, 2 usage/IO error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, TypedDict, cast

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

    Two optional keys are written by the *caller* rather than by test-memory-gate.c and
    so are read through `identity()` instead of being declared here (this project pins
    pyright to pythonVersion 3.9, where `NotRequired` does not exist): `arch` and
    `compiler`. What they distinguish is invisible to the binary: an arm64 macOS run from
    a soldr-cross-built bundle and an arm64 macOS run from an Xcode-built tree report the
    same `platform` on the same runner while being different toolchains with legitimately
    different peaks (#277 phase B).

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
#
# 2026-09-02 (#266): min-of-4 on ubuntu-latest was flaky enough to flip PASS/FAIL between
# consecutive pushes of the *same* Release object code (diagnostic.c-only commits do not
# touch the Release build). Five consecutive PR #276 / main measurements, all min-of-4,
# baseline 22.3 MB, allowed 25.6 MB:
#   238218d5 -> 24.7 MB PASS (spread 17.8%)
#   5d1d6ae3 -> 26.5 MB FAIL (spread  9.1%)
#   32e08564 -> 25.4 MB PASS (spread 23.2%)
#   0e23010b -> 29.2 MB FAIL (spread 14.7%)   <- identical Release code to 32e08564
#   main #275 (docs-only, no allocator-path changes at all) -> 25.8 MB FAIL (+15.7%)
# Four of five spreads are at or above PEAK_TOLERANCE itself -- the threshold this
# comment used to warn "cannot distinguish a regression from noise" was already
# happening in practice, not just hypothetically. Per that same comment's own rule
# ("raise RUNS_EXPECTED or the tolerance, with this measurement as the justification"),
# these five measurements are that justification for min-of-8: RUNS_EXPECTED below moves
# from 4 to 8. PEAK_TOLERANCE stays 0.15 and ci/memory-baselines/*.json is deliberately
# NOT touched -- the baseline was recorded as a min-of-4 (22.3 MB, 5.8% spread, #70), and
# a min-of-8 of the same underlying peak-RSS distribution can only be <= a min-of-4 of
# that distribution, never higher, so doubling the run count here is strictly
# conservative with respect to that baseline: it cannot manufacture a new pass, only
# remove noise-driven false failures.
PEAK_TOLERANCE = 0.15

# Number of runs the CI job is expected to supply. Fewer is allowed (with a warning) so
# the script stays usable locally, but the committed baselines are min-of-N (N=4 for the
# existing ci/memory-baselines/*.json; see the RUNS_EXPECTED comment above for why a
# larger N here is still a valid, strictly-conservative comparison against those files).
RUNS_EXPECTED = 8

# Counters are exact, not statistical. A thread that was created and joined must not
# still be live; anything above this is cleanup that did not run. Matches the inline
# assertion in test-memory-gate.c.
MAX_LIVE_THREADS = 32


# CI's runners are 4-core (ubuntu-latest/windows-latest/macos-latest as of the baselines
# this gate compares against). A wider local box measurably inflates the peak this test
# exercises: it drives MI_BENCH_THREADS()-many concurrently-running threads doing an
# allocation churn, and peak RSS/commit scales with how many of those threads the OS
# actually schedules onto distinct CPUs at once, not just with the allocator's true
# high-water mark. Observed on this repo: 58 MB on an unrestricted 16-core host vs ~23 MB
# under `taskset -c 0-3` on the same host, for the identical binary and commit -- an
# apples-to-oranges comparison against the 4-core baselines that nothing about the JSON
# schema flags as such. Pinning the child to <= 4 CPUs here is what makes a local
# `python ci/memory_gate.py check` (no path arguments) comparable to the CI numbers
# instead of just plausible-looking.
MAX_GATE_CPUS = 4


def _cpu_affinity_preexec(cpus: list[int]) -> Callable[[], None]:
    def _set() -> None:
        os.sched_setaffinity(0, cpus)

    return _set


def find_gate_binary() -> Path:
    """Locate a built mimalloc-test-memory-gate executable under common build dirs.

    Multiple build directories (Release, Debug, ASan, ...) commonly coexist locally.
    Comparing against a 4-core *Release* baseline against, say, an ASan build's peak
    (which is inflated by shadow memory and redzones, not allocator growth) is exactly
    the kind of apples-to-oranges mismatch MAX_GATE_CPUS above exists to avoid for CPU
    count -- so among all candidates, prefer the most recently built one (mtime), which
    in practice is whichever config the caller configured/built right before running
    this, matching what `python ci/memory_gate.py check` right after a build expects.
    """
    root = Path(__file__).resolve().parents[1]
    names = ("mimalloc-test-memory-gate", "mimalloc-test-memory-gate.exe")
    search_dirs = sorted(root.glob("build*")) + sorted(root.glob("out/*"))
    candidates: list[Path] = []
    for d in search_dirs:
        for name in names:
            for candidate in d.rglob(name):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    candidates.append(candidate)
    if not candidates:
        raise FileNotFoundError(
            "no built mimalloc-test-memory-gate found under build*/ or out/*/ -- "
            "configure and build first, e.g.:\n"
            "  cmake -B build -DMI_PPROF=ON && cmake --build build --config Release"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_gate_binary(binary: Path, runs: int = RUNS_EXPECTED) -> list[str]:
    """Run the gate binary `runs` times, pinned to <= MAX_GATE_CPUS CPUs where the
    platform supports it, and return the resulting JSON file paths.

    CPU pinning uses os.sched_setaffinity via preexec_fn on platforms that have it
    (Linux); elsewhere (macOS, Windows) there is no equivalent standard-library call, so
    this runs unpinned there -- those platforms' baselines were not observed to need it,
    and CI itself does not pin on them either.
    """
    cpu_count = os.cpu_count() or MAX_GATE_CPUS
    pin_cpus = list(range(min(MAX_GATE_CPUS, cpu_count)))
    can_pin = hasattr(os, "sched_setaffinity")
    print(
        f"running {binary} x{runs}"
        + (
            f", pinned to CPUs {pin_cpus}"
            if can_pin
            else " (no CPU pinning available on this platform)"
        )
    )
    result_paths: list[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="mimalloc-memory-gate-"))
    for i in range(1, runs + 1):
        out_path = tmpdir / f"result-{i}.json"
        env = dict(os.environ)
        env["MI_BENCH_JSON"] = str(out_path)
        preexec: Callable[[], None] | None = _cpu_affinity_preexec(pin_cpus) if can_pin else None
        subprocess.run(
            [str(binary)], env=env, stdout=subprocess.DEVNULL, check=True, preexec_fn=preexec
        )
        result_paths.append(str(out_path))
    return result_paths


def baseline_key(result: Result) -> tuple[str, ...]:
    """The identity a baseline file is matched on: platform, arch, compiler, MI_PPROF.

    MI_PPROF changes the legitimate peak (rule 4 puts the profiler arena in process
    memory), so ON and OFF get separate baselines rather than one padded threshold. Arch
    and compiler joined in for #277 phase B: `macos` alone stopped being a sufficient key
    the moment the same `macos-latest` runner started executing both an Xcode-built binary
    and a soldr-cross-built one. They are optional, so every existing baseline
    (`linux-pprof1.json`, `windows-pprof1.json`, `macos-pprof1.json`) keeps its name and
    keeps matching the runs that produced it -- a new toolchain asks for its own file
    rather than silently borrowing another compiler's number.
    """
    parts = [result["platform"]]
    for field in IDENTITY_FIELDS:
        value = identity(result, field)
        if value:
            parts.append(value)
    parts.append("pprof{}".format(result["mi_pprof"]))
    return tuple(parts)


def baseline_path(result: Result) -> Path:
    return BASELINE_DIR / ("-".join(baseline_key(result)) + ".json")


def load(path: str | Path) -> Result:
    with Path(path).open(encoding="utf-8") as f:
        # json.load returns Any; this cast is the single point where untyped external
        # data becomes typed, so the schema assumption is stated once and visibly.
        return cast(Result, json.load(f))


#: Optional identity keys (see Result's docstring). Order fixes the baseline file name.
IDENTITY_FIELDS = ("arch", "compiler")


def identity(record: Result, field: str) -> str | None:
    value = cast("dict[str, object]", record).get(field)
    return value if isinstance(value, str) and value else None


def stamp(result: Result, arch: str | None, compiler: str | None) -> Result:
    """Record the toolchain identity the binary cannot know about itself."""
    fields = cast("dict[str, object]", result)
    if arch:
        fields["arch"] = arch
    if compiler:
        fields["compiler"] = compiler
    return result


def load_runs(
    paths: list[str], arch: str | None = None, compiler: str | None = None
) -> tuple[Result, list[float], float]:
    """Load N runs of the same configuration and return (representative, peak_mb, spread%).

    The representative record is the run with the minimum gated peak; its counters are
    the ones checked, so the counter assertions and the peak refer to the same run.
    """
    runs = [stamp(load(p), arch, compiler) for p in paths]
    keys = {(baseline_key(r), r["gated_metric"]) for r in runs}
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


def control(
    result_paths: list[str], *, arch: str | None = None, compiler: str | None = None
) -> int:
    """Positive control: require the gate to FAIL on a deliberately leaky build.

    A gate that has never been observed to fire proves nothing -- it can be silently
    broken for months and every run still looks green. So CI builds a copy with
    MI_BENCH_INJECT_LEAK, runs it through the very same comparison, and this command
    inverts the verdict: exit 0 only if `check` would have failed.

    Requiring inject_leak to be *set* is what keeps this from being usable as an escape
    hatch -- it cannot be pointed at a real regression to turn it green.
    """
    result, _, _ = load_runs(result_paths, arch, compiler)
    if not result.get("inject_leak", 0):
        print(
            "REFUSING: positive control requires a build with MI_BENCH_INJECT_LEAK "
            "set. This run has none, so a failure here would mean a real regression, "
            "not a working control."
        )
        return 2

    print("=== positive control: the gate is expected to FAIL below ===")
    rc = check(result_paths, _control=True, arch=arch, compiler=compiler)
    if rc == 1:
        print("\nPASS (control): the gate detected the injected leak.")
        return 0
    print(f"\nFAIL (control): the gate did NOT detect an injected leak (rc={rc}).")
    print("The gate is not working. A green `check` on a real build means nothing")
    print("until this passes.")
    return 1


def check(
    result_paths: list[str],
    _control: bool = False,
    *,
    arch: str | None = None,
    compiler: str | None = None,
) -> int:
    result, peaks, spread = load_runs(result_paths, arch, compiler)

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
        print(
            "This platform/arch/compiler/config ({}) has never been recorded. "
            "Create it with:".format("+".join(baseline_key(result)))
        )
        print(
            "    python ci/memory_gate.py update{} {}".format(
                _identity_flags(arch, compiler), " ".join(result_paths)
            )
        )
        return 2
    base = load(bpath)

    failures: list[str] = []
    metric = result["gated_metric"]
    peak, base_peak = peaks[0], base["peak_mb"]
    allowed = base_peak * (1.0 + PEAK_TOLERANCE)

    print("baseline={} metric={}  ({})".format("+".join(baseline_key(result)), metric, bpath.name))
    base_compiler = identity(base, "compiler")
    if base_compiler and not identity(result, "compiler"):
        # Not fatal: the file still keys on `macos`, so this is the legacy match working
        # as intended. Saying so keeps a reader from assuming the number was recorded
        # with the toolchain that just produced it.
        print(
            f"  NOTE: this baseline was recorded with compiler={base_compiler!r}; this run "
            f"declared none (pass --compiler to give a new toolchain its own baseline)."
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
        print(
            "    python ci/memory_gate.py update{} {}".format(
                _identity_flags(arch, compiler), " ".join(result_paths)
            )
        )
        print("and say why in the PR. Do not re-baseline to make a red gate green.")
        return 1

    print("\nPASS")
    return 0


def _identity_flags(arch: str | None, compiler: str | None) -> str:
    return (f" --arch {arch}" if arch else "") + (f" --compiler {compiler}" if compiler else "")


def update(result_paths: list[str], *, arch: str | None = None, compiler: str | None = None) -> int:
    result, peaks, spread = load_runs(result_paths, arch, compiler)
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


def take_option(argv: list[str], name: str) -> tuple[list[str], str | None]:
    """Pull `--name VALUE` (or `--name=VALUE`) out of argv, anywhere in it.

    Hand-rolled rather than argparse so the positional shape this script has always had
    (`memory_gate.py <command> <result.json>...`, with a glob expanding to any number of
    paths) is untouched, and so the module-level `check`/`update`/`control` functions that
    ci/verify_local.py calls directly keep the same signature.
    """
    rest: list[str] = []
    value: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == f"--{name}":
            if index + 1 >= len(argv):
                raise ValueError(f"--{name} needs a value")
            value = argv[index + 1]
            index += 2
            continue
        if token.startswith(f"--{name}="):
            value = token.split("=", 1)[1]
            index += 1
            continue
        rest.append(token)
        index += 1
    return rest, value


def main(argv: list[str]) -> int:
    try:
        argv, arch = take_option(list(argv), "arch")
        argv, compiler = take_option(argv, "compiler")
    except ValueError as e:
        print(f"error: {e}")
        return 2
    if len(argv) < 2 or argv[1] not in ("check", "update", "control"):
        print(__doc__)
        return 2
    result_paths = argv[2:]
    if argv[1] == "check" and not result_paths:
        try:
            binary = find_gate_binary()
            result_paths = run_gate_binary(binary)
        except (OSError, ValueError, FileNotFoundError, subprocess.SubprocessError) as e:
            print(f"error: {e}")
            return 2
    elif not result_paths:
        print(__doc__)
        return 2
    try:
        cmd = {"check": check, "update": update, "control": control}[argv[1]]
        return cmd(result_paths, arch=arch, compiler=compiler)
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
