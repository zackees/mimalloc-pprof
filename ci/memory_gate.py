#!/usr/bin/env python3
"""Compare a test-memory-gate JSON run against the committed per-platform baseline.

This is the piece that turns test-memory-gate from a self-relative assertion into an
actual regression gate: it compares against numbers recorded on a previous commit, so a
leak introduced gradually over several PRs cannot hide inside a per-run ratio.

Why per-platform baselines and never a shared one: lazy zeroing means Linux RSS and
Windows commit report different numbers for identical allocator behavior, and macOS
compresses memory so its resident figure falls under pressure while a leak grows. Only
same-OS deltas are meaningful.

Both commands take several runs of the same binary and use the **minimum** observed peak.
Noise on a shared CI runner only ever adds memory (another tenant's page cache, a
straggler thread still winding down), so the minimum is the estimator closest to the
allocator's true high-water mark. The spread across runs is printed every time -- if it
ever approaches the tolerance, the tolerance is wrong and the printout says so.

The minimum is not what makes this gate stable, though; #298 established that no
statistic could rescue the old workload (min-of-8 across twelve consecutive `main` runs
spanned 22.3-28.0 MB, the median 25.6-31.2 MB), because the peak was set by how many of
test-memory-gate's churn workers the runner's scheduler happened to overlap. The fix was
in the workload, not here: test/test-memory-gate.c now rendezvouses those workers, so the
peak is the structural cost of N live sets rather than a scheduler coin flip. The minimum
stays because one-sided noise makes it the right estimator, not because it is load-bearing.

Usage:
    memory_gate.py check   [<result.json> ...]   # no paths: build/run the binary itself
    memory_gate.py update  <result.json> [more.json ...]   # deliberate, reviewed act
    memory_gate.py control <result.json> [more.json ...]   # positive control: must FAIL
    memory_gate.py where   <result.json>                   # 0 = baseline exists, 3 = none

`--arch <name>` and `--compiler <name>` (accepted by every command) declare the
toolchain identity the binary cannot know about itself, and become part of the baseline
file's name. #277 phase B needs them: an arm64 macOS run from a soldr-cross-built bundle
and an arm64 macOS run from an Xcode-built tree are the same `platform` on the same
`macos-latest` runner, and comparing one against the other's committed peak would be a
cross-toolchain comparison dressed as a regression check. Omitting both keeps the legacy
`<platform>-pprof<N>.json` name, so no existing baseline moves.

With no JSON paths, `check` locates the built `mimalloc-test-memory-gate` binary,
runs it RUNS_EXPECTED times (see run_gate_binary), and checks those results -- this
is what makes `python ci/memory_gate.py check` alone reproduce the CI job locally.

Exit codes: 0 pass, 1 regression, 2 usage/IO error, 3 (`where` only) no baseline yet.
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
# 2026-09-03 (#298). This number is derived from measurement, not chosen. History first,
# because it is what the derivation has to beat:
#
#   The tolerance was 0.15 and the ubuntu gate still flipped red on commits that changed
#   no allocator code at all (docs-only #275, ci-only #278/#297). The `memory-gate-
#   ubuntu-latest` artifacts of the last twelve `main` runs of c-unit.yml say why --
#   min-of-8 / median / within-run spread, against a 22.3 MB baseline and a 25.6 MB
#   allowed:
#     33618717188  26.7 / 29.6 / 32.6%      33647332787  24.5 / 26.5 / 17.6%
#     33629562858  24.7 / 27.4 / 19.0%      33654892179  27.9 / 31.2 / 24.4%
#     33631613324  28.0 / 29.2 / 15.7%      33660325957  26.5 / 27.6 / 18.5%
#     33635722154  26.0 / 28.0 / 19.6%      33689409217  22.7 / 25.6 / 25.6%
#     33636560081  22.3 / 26.9 / 30.0%      33692432919  25.5 / 27.9 / 28.2%
#     33646494496  24.3 / 28.2 / 44.4%      33699070304  27.8 / 30.2 / 19.8%
#   Every within-run spread exceeds the tolerance it was compared under, min-of-8 across
#   runs spans 25.6% and the median 21.9%. No tolerance and no statistic can separate a
#   regression from that; a per-scenario decomposition (idle host, `taskset -c 0-3`)
#   found scenario 1 -- the 8-thread churn -- alone ranging 14.1 -> 24.6 MB run to run,
#   with every later scenario adding <= 2 MB on top. The gate was measuring the runner.
#
#   So the workload was fixed instead (test/test-memory-gate.c: the churn workers
#   rendezvous, and the cross-thread handoff no longer busy-spins). Three independent
#   ubuntu-latest runner VMs (nproc=4, THP=always), 16 runs each, the same object code:
#     33700518749  min 58.0  med 58.3  max 58.5  spread 0.9%
#     33700575788  min 58.0  med 58.5  max 58.5  spread 0.9%
#     33700583919  min 58.0  med 58.3  max 58.6  spread 1.0%
#   min-of-16 is 58.0 MB on all three -- 0.0% across-run variation of the estimator, and
#   1.0% across all 48 samples. Pinning changes nothing any more (`taskset -c 0-1` 0.5%,
#   `-c 0` 0.7%), `MIMALLOC_SCAVENGER=0 MIMALLOC_PURGE_HOLES=0` changes nothing (1.2%),
#   and an unpinned 16-core workstation reads 58.2-58.3 -- the same number as the 4-vCPU
#   runner, where the old workload read 58 MB unpinned vs 23 MB pinned. The old workload
#   re-measured on those same three VMs for comparison: 26.4 / 27.4 / 25.5 min-of-16,
#   spreads 26.1% / 24.5% / 23.1%.
#
# 0.05 is therefore 5x the largest observed spread (1.0%) and infinitely more than the
# observed across-run variation of the estimator itself (0.0%), leaving 2.9 MB of
# absolute headroom on a 58 MB baseline. The measurement would support 0.03; the
# difference is reserved for runner-image drift, which three runs on one image cannot
# bound. Tighten it once ten `main` runs on a new image have been observed, with those
# numbers written here.
#
# The positive control's margin, which is the other half of a defensible threshold: the
# MI_BENCH_INJECT_LEAK=200000 build read min-of-8 82.6 / 82.4 / 82.5 MB on the same three
# VMs -- +42.1% over the baseline, or 8.4x this tolerance. The rule is that the control
# must fail by at least 2x the tolerance, so a leak this size is caught with a factor of
# four to spare, and the smallest regression the gate can now see is 2.9 MB (5% of a
# 58 MB structural peak) instead of "nothing at all under 25% of noise".
PEAK_TOLERANCE = 0.05

# Number of runs the CI job is expected to supply. Fewer is allowed (with a warning) so
# the script stays usable locally.
#
# Left at 8 by #298 rather than raised or lowered, and that is a measured decision both
# ways. Raising it buys nothing: min-of-16 and min-of-8 of the post-#298 distribution are
# the same 58.0 MB on all three runner VMs measured above, because the distribution is
# 0.6 MB wide. Lowering it to 4 would also be defensible on those numbers and would need a
# matching edit to the `for i in 1 2 3 4 5 6 7 8` loops in c-unit.yml -- not worth the
# churn for a binary that now completes in ~0.1 s (the busy-spin removal in scenario 3
# made it faster, not slower). Eight runs also keep the printed spread informative enough
# to notice an image change before it becomes a red gate.
RUNS_EXPECTED = 8

# Counters are exact, not statistical. A thread that was created and joined must not
# still be live; anything above this is cleanup that did not run. Matches the inline
# assertion in test-memory-gate.c.
MAX_LIVE_THREADS = 32


# CI's runners are 4-core. This pinning was load-bearing before #298: the old workload's
# peak scaled with how many of its churn threads the OS put on distinct CPUs at once, so
# an unrestricted 16-core host read 58 MB against a 4-core baseline of ~23 MB for the
# identical binary and commit -- an apples-to-oranges comparison nothing in the JSON
# flagged as such.
#
# It is no longer load-bearing, and the measurement that says so is in PEAK_TOLERANCE
# above: with the rendezvoused workload the same 16-core host reads 58.2-58.3 MB unpinned
# and the 4-vCPU runner reads 58.0-58.6 MB. It is kept as a cheap guard -- a future
# scenario that reintroduces CPU-count sensitivity would otherwise make local numbers
# quietly incomparable to CI's again, which is exactly how #298 stayed hidden for so long.
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


def report_runs(peaks: list[float], spread: float, *, gated: bool = True) -> None:
    """Print the run distribution; `gated=False` for a run nothing is thresholded against.

    The spread warning below is advice about *this gate's* threshold, so it must not fire
    on the positive control. A leak build's runs legitimately span tens of percent (the
    injected blocks land in already-resident arena memory or not, depending on purge
    timing: 82.6-129.8 MB across one measured control run), and telling a reader to raise
    RUNS_EXPECTED or the tolerance on the strength of that would be advice to loosen a
    real gate because a deliberately broken build was noisy.
    """
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
    if gated and spread >= 100.0 * PEAK_TOLERANCE:
        print(
            f"  WARNING: observed spread ({spread:.1f}%) is at or above the tolerance "
            f"({100.0 * PEAK_TOLERANCE:.0f}%). The threshold cannot distinguish a regression from noise -- "
            "fix the workload's determinism first (that is what #298 did), and only then "
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
    report_runs(peaks, spread, gated=not _control)

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


def where(result_paths: list[str], *, arch: str | None = None, compiler: str | None = None) -> int:
    """Print the baseline file a run of this identity is compared against.

    Exit 0 if it exists, **3** if it does not, 2 if this run's JSON could not be read.

    The three-way split is the point. `check` also exits 2 for "no baseline", but it exits
    2 for an unreadable or absent result file as well -- so a workflow that treats `check`
    rc=2 as "bootstrap me" turns a *crashed gate binary* into a green run with a
    reassuring warning. Gating on this probe instead keeps those two apart: 3 means the
    lane is genuinely new, 2 means something is wrong with the run itself.

    #277 phase B needs the probe anyway, because the positive control cannot run before a
    baseline exists (`control` requires `check` to fail, and a missing baseline is not a
    failure), and hardcoding the filename in YAML would drift from what this module
    computes from platform/arch/compiler/MI_PPROF.
    """
    result, _, _ = load_runs(result_paths, arch, compiler)
    path = baseline_path(result)
    print(path)
    return 0 if path.exists() else 3


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
    if len(argv) < 2 or argv[1] not in ("check", "update", "control", "where"):
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
        cmd = {"check": check, "update": update, "control": control, "where": where}[argv[1]]
        return cmd(result_paths, arch=arch, compiler=compiler)
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
