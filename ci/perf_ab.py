#!/usr/bin/env python3
"""Paired A/B of two allocator revisions (#479).

The workloads are a few large-block streams, a small-object control, and Larson &
Krishnan's server workload (the `larson` rows, #506: small blocks whose tables rotate
between threads, so most frees are remote). Builds the minimal static library at --base
and --head (every observability flag named OFF; the BUILDS table adds flags for the
profiler and chart-build rows), links ci/perf_ab.c against each, and runs every workload
--reps times with the arm order alternating inside each repetition. --head-env KEY=VALUE
(repeatable) sets extra environment on the head arm only, e.g. an allocator option, to
attribute a difference to it; the table header names it. Prints a markdown table of
medians and the median paired difference with a bootstrap 95% interval; a direction is
only claimed when the interval excludes zero. Linux only. Meant for a CI runner, not a
busy dev machine.

    python3 ci/perf_ab.py --base origin/main --head HEAD [--reps 7] [--workloads random] [--summary out.md]
    python3 ci/perf_ab.py --base HEAD --head HEAD --workloads larson --head-env MIMALLOC_ARENA_PURGE_MULT=1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLAGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DMI_BUILD_SHARED=OFF",
    "-DMI_BUILD_OBJECT=OFF",
    "-DMI_BUILD_TESTS=OFF",
    "-DMI_OVERRIDE=OFF",
    "-DMI_PPROF=OFF",
    "-DMI_MEMEVT=OFF",
    "-DMI_DIAGNOSTICS=OFF",
    "-DMI_DHAT=OFF",
    "-DMI_OWNER_GATE=OFF",
]
# The builds a row can run on: extra CMake flags over FLAGS, and extra environment. "pprof"
# samples at the default rate: the gate that a memory strategy does not slow the profiler down.
# "chart" is how the README scaling charts build mimalloc-pprof (profiler and memory events
# compiled in, both off at run time, #478): a regression only that build shows is still one.
# (Names of equal length: see `build`.)
BUILDS: dict[str, tuple[list[str], dict[str, str]]] = {
    "plain": ([], {}),
    "pprof": (["-DMI_PPROF=ON"], {"MIMALLOC_PROF": "1"}),
    "chart": (["-DMI_PPROF=ON", "-DMI_MEMEVT=ON"], {}),
}
# name: (build, (threads, generations, min bytes, max bytes, ops per thread, pause ms, larson
# slots)). A pause makes the row bursty (#486): BURSTS bursts per thread, everything freed after
# each, then idle. Larson slots > 0 make the row Larson & Krishnan's server workload (#506, as
# ScalingPattern::Larson in rust/benchmark-suite/src/scaling.rs): a table of that many small
# blocks per thread, rotated between threads every round so later frees are remote; generations
# and pause are ignored there. 0 = the stream modes above.
WORKLOADS = {
    "large-class/8": ("plain", (8, 1, 96 << 10, 512 << 10, 400000, 0, 0)),
    "large-class-ephemeral/8": ("plain", (8, 8, 96 << 10, 512 << 10, 400000, 0, 0)),
    "random-large/8": ("plain", (8, 1, 64 << 10, 4 << 20, 40000, 0, 0)),
    "random-large-bursty/8": ("plain", (8, 1, 64 << 10, 4 << 20, 40000, 300, 0)),
    "random-large/1": ("plain", (1, 1, 64 << 10, 4 << 20, 200000, 0, 0)),
    "small/8 (control)": ("plain", (8, 1, 16, 1024, 5000000, 0, 0)),
    "large-class/8 (profiler on)": ("pprof", (8, 1, 96 << 10, 512 << 10, 400000, 0, 0)),
    "large-class-ephemeral/8 (chart build)": ("chart", (8, 8, 96 << 10, 512 << 10, 400000, 0, 0)),
    # the README chart's generation length (~12.5k ops per short-lived thread at 8 workers, #478):
    # a per-thread start/exit cost weighs 4x more than in the row above
    "large-class-ephemeral/8 short generations (chart build)": (
        "chart",
        (8, 8, 96 << 10, 512 << 10, 100000, 0, 0),
    ),
    # Larson & Krishnan's server workload (#506): 8-1000 B blocks, 5000 per table
    "larson/1": ("plain", (1, 1, 8, 1000, 2000000, 0, 5000)),
    "larson/8": ("plain", (8, 1, 8, 1000, 2000000, 0, 5000)),
    # the README chart builds mimalloc-pprof this way (#478)
    "larson/8 (chart build)": ("chart", (8, 1, 8, 1000, 2000000, 0, 5000)),
}
# what ci/perf_ab.c prints, in order; the byte counts are shown in MiB
METRICS = (
    "ops/s",
    "cpu s",
    "worker cpu s",
    "minor faults",
    "peak RSS MiB",
    "RSS 0.5 s after drain MiB",
    "RSS at release bound MiB",
    "release ms",
)
IN_MIB = {"peak RSS MiB", "RSS 0.5 s after drain MiB", "RSS at release bound MiB"}
# #491: the promise "idle memory is back within bound_ms"; perf-ab fails when head breaks it
RATCHET = ROOT / "ci/release_ratchet.json"
RELEASE_PERCENTILE = 95


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def build(arm: str, ref: str, work: Path, kind: str) -> Path:
    # Paths named by arm ("base"/"head") and build (BUILDS), never by ref: every
    # executable path then has the same length, and so does the process's initial stack. A
    # longer argv/environment shifts stack alignment, the likely reason identical binaries
    # differed by 17% on the small-object row of #494's null run.
    tree, out = work / f"src-{arm}", work / f"bin-{arm}-{kind}"
    if not tree.exists():
        run(["git", "worktree", "add", "--detach", str(tree), ref], cwd=ROOT)
    run(["cmake", "-S", str(tree), "-B", str(out), *FLAGS, *BUILDS[kind][0]])
    run(["cmake", "--build", str(out), "--target", "mimalloc-static", "--parallel"])
    exe = out / "perf_ab"
    lib = next(out.glob("libmimalloc*.a"))
    run(
        [
            "cc",
            "-O2",
            "-I",
            str(tree / "include"),
            str(ROOT / "ci/perf_ab.c"),
            str(lib),
            "-lpthread",
            "-o",
            str(exe),
        ]
    )
    return exe


def percent(b: float, h: float) -> float:
    if b == 0:  # (a release time can be 0 ms)
        return 0.0 if h == 0 else 100.0
    return (h - b) / b * 100


def percentile(values: list[float], pct: int) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1)]


def paired(base: list[float], head: list[float]) -> tuple[float, float, float]:
    diffs = [percent(b, h) for b, h in zip(base, head)]
    rng = random.Random(479)
    boots = sorted(statistics.median(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
    return statistics.median(diffs), boots[50], boots[1949]


def cpu_model() -> str:
    # an effect can depend on the CPU the runner happens to get (#478: -6.5% on some runners,
    # 0 on others), so every table says which one it came from
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown CPU"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument(
        "--workloads", default="", help="only the workloads whose name contains this text"
    )
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--head-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra environment for the head arm only (repeatable), e.g. "
        "MIMALLOC_ARENA_PURGE_MULT=1 to attribute a cost to one option (#506)",
    )
    args = parser.parse_args()
    workloads = {name: w for name, w in WORKLOADS.items() if args.workloads in name}
    if not workloads:
        parser.error(f"no workload matches {args.workloads!r}")
    head_env: dict[str, str] = {}
    for pair in args.head_env:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            parser.error(f"--head-env wants KEY=VALUE, got {pair!r}")
        head_env[key] = value
    bound_ms = int(json.loads(RATCHET.read_text())["bound_ms"])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            exes = {
                (arm, kind): build(arm, ref, work, kind)
                for arm, ref in (("base", args.base), ("head", args.head))
                for kind in sorted({k for k, _ in workloads.values()})
            }
            samples: dict[tuple[str, str], list[list[float]]] = {
                (w, a): [] for w in workloads for a in ("base", "head")
            }
            for rep in range(args.reps):
                for workload, (kind, params) in workloads.items():
                    for arm in ("base", "head") if rep % 2 == 0 else ("head", "base"):
                        exe = str(exes[(arm, kind)])
                        env = {**os.environ, **BUILDS[kind][1]}
                        if arm == "head":
                            env.update(head_env)
                        *stream, larson_slots = params
                        cmd = [exe, *map(str, stream), str(bound_ms), str(larson_slots)]
                        values = list(map(float, run(cmd, env=env).split()))
                        for index, metric in enumerate(METRICS):
                            if metric in IN_MIB:
                                values[index] /= 2**20
                        samples[(workload, arm)].append(values)
        finally:  # unregister the trees while they still exist: a prune here would find nothing to prune
            for tree in work.glob("src-*"):
                run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT)
    shown_env = " ".join(f"{k}={v}" for k, v in head_env.items())
    head_label = f"`{args.head}`" + (f" (head env: {shown_env})" if head_env else "")
    rows = [
        f"`{args.base}` vs {head_label} on {cpu_model()}, {args.reps} paired reps, "
        "alternating order. "
        "Median base -> head, then median paired difference [bootstrap 95%]; "
        "**bold** when the interval excludes 0.",
        "",
        "| workload | " + " | ".join(METRICS) + " |",
        "|---|" + "---|" * len(METRICS),
    ]
    for workload in workloads:
        cells: list[str] = []
        for index, _metric in enumerate(METRICS):
            base = [s[index] for s in samples[(workload, "base")]]
            head = [s[index] for s in samples[(workload, "head")]]
            mid, low, high = paired(base, head)
            delta = f"{mid:+.1f}% [{low:+.1f}, {high:+.1f}]"
            if low > 0 or high < 0:
                delta = f"**{delta}**"
            cells.append(
                f"{statistics.median(base):,.4g} -> {statistics.median(head):,.4g}<br>{delta}"
            )
        rows.append(f"| {workload} | " + " | ".join(cells) + " |")
    # #491: the release promise, per workload, on head; the worst P95 is the evidence a lower
    # bound in ci/release_ratchet.json needs (`p95_release_ms`)
    release = METRICS.index("release ms")
    p95 = {
        w: percentile([s[release] for s in samples[(w, "head")]], RELEASE_PERCENTILE)
        for w in workloads
    }
    late = {w: p for w, p in p95.items() if p > bound_ms}
    worst = max(p95, key=lambda w: p95[w])
    rows.append("")
    rows.append(
        f"Release bound (ci/release_ratchet.json): {bound_ms} ms; head P{RELEASE_PERCENTILE} "
        f"release, worst workload: {p95[worst]:.0f} ms ({worst}) -- "
        + (", ".join(f"**{w}: {p:.0f} ms, LATE**" for w, p in late.items()) or "held")
    )
    table = "\n".join(rows) + "\n"
    print(table)
    if args.summary:
        args.summary.write_text(table)
    return 1 if late else 0


if __name__ == "__main__":
    raise SystemExit(main())
