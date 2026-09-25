#!/usr/bin/env python3
"""Paired A/B of two allocator revisions on a few large-block workloads (#479).

Builds the minimal static library at --base and --head (every observability flag named
OFF; the BUILDS table adds flags for the profiler and chart-build rows), links
ci/perf_ab.c against each, and runs every workload --reps times with the arm
order alternating inside each repetition. Prints a markdown table of medians and the
median paired difference with a bootstrap 95% interval; a direction is only claimed when
the interval excludes zero. Linux only. Meant for a CI runner, not a busy dev machine.

    python3 ci/perf_ab.py --base origin/main --head HEAD [--reps 7] [--workloads random] [--summary out.md]
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
# name: (build, (threads, generations, min bytes, max bytes, ops per thread, pause ms)). A pause
# makes the row bursty (#486): BURSTS bursts per thread, everything freed after each, then idle.
WORKLOADS = {
    "large-class/8": ("plain", (8, 1, 96 << 10, 512 << 10, 400000, 0)),
    "large-class-ephemeral/8": ("plain", (8, 8, 96 << 10, 512 << 10, 400000, 0)),
    "random-large/8": ("plain", (8, 1, 64 << 10, 4 << 20, 40000, 0)),
    "random-large-bursty/8": ("plain", (8, 1, 64 << 10, 4 << 20, 40000, 300)),
    "random-large/1": ("plain", (1, 1, 64 << 10, 4 << 20, 200000, 0)),
    "small/8 (control)": ("plain", (8, 1, 16, 1024, 5000000, 0)),
    "large-class/8 (profiler on)": ("pprof", (8, 1, 96 << 10, 512 << 10, 400000, 0)),
    "large-class-ephemeral/8 (chart build)": ("chart", (8, 8, 96 << 10, 512 << 10, 400000, 0)),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument(
        "--workloads", default="", help="only the workloads whose name contains this text"
    )
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    workloads = {name: w for name, w in WORKLOADS.items() if args.workloads in name}
    if not workloads:
        parser.error(f"no workload matches {args.workloads!r}")
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
                        cmd = [exe, *map(str, params), str(bound_ms)]
                        values = list(map(float, run(cmd, env=env).split()))
                        for index, metric in enumerate(METRICS):
                            if metric in IN_MIB:
                                values[index] /= 2**20
                        samples[(workload, arm)].append(values)
        finally:  # unregister the trees while they still exist: a prune here would find nothing to prune
            for tree in work.glob("src-*"):
                run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT)
    rows = [
        f"`{args.base}` vs `{args.head}`, {args.reps} paired reps, alternating order. "
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
