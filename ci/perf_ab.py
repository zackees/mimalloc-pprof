#!/usr/bin/env python3
"""Paired A/B of two allocator revisions on a few large-block workloads (#479).

Builds the minimal static library at --base and --head (every observability flag named
OFF; a second build with MI_PPROF=ON for the profiler rows, run with MIMALLOC_PROF=1), links
ci/perf_ab.c against each, and runs every workload --reps times with the arm
order alternating inside each repetition. Prints a markdown table of medians and the
median paired difference with a bootstrap 95% interval; a direction is only claimed when
the interval excludes zero. Linux only. Meant for a CI runner, not a busy dev machine.

    python3 ci/perf_ab.py --base origin/main --head HEAD [--reps 7] [--workloads random] [--summary out.md]
"""

from __future__ import annotations

import argparse
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
# name: (profiled, (threads, generations, min bytes, max bytes, ops per thread)). A profiled
# row runs the MI_PPROF=ON build with sampling on at the default rate: it is the gate that a
# memory strategy does not slow the profiler down.
WORKLOADS = {
    "large-class/8": (False, (8, 1, 96 << 10, 512 << 10, 400000)),
    "large-class-ephemeral/8": (False, (8, 8, 96 << 10, 512 << 10, 400000)),
    "random-large/8": (False, (8, 1, 64 << 10, 4 << 20, 40000)),
    "random-large/1": (False, (1, 1, 64 << 10, 4 << 20, 200000)),
    "small/8 (control)": (False, (8, 1, 16, 1024, 5000000)),
    "large-class/8 (profiler on)": (True, (8, 1, 96 << 10, 512 << 10, 400000)),
}
# what ci/perf_ab.c prints, in order; the byte counts are shown in MiB
METRICS = (
    "ops/s",
    "cpu s",
    "worker cpu s",
    "minor faults",
    "peak RSS MiB",
    "RSS 0.5 s after drain MiB",
    "RSS 2 s after drain MiB",
)
IN_MIB = {"peak RSS MiB", "RSS 0.5 s after drain MiB", "RSS 2 s after drain MiB"}
PROFILER_ENV = {**os.environ, "MIMALLOC_PROF": "1"}


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def build(ref: str, work: Path, profiled: bool) -> Path:
    name = ref.replace("/", "_")
    tree, out = work / f"src-{name}", work / f"bin-{name}{'-pprof' if profiled else ''}"
    if not tree.exists():
        run(["git", "worktree", "add", "--detach", str(tree), ref], cwd=ROOT)
    flags = [f if f != "-DMI_PPROF=OFF" or not profiled else "-DMI_PPROF=ON" for f in FLAGS]
    run(["cmake", "-S", str(tree), "-B", str(out), *flags])
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


def paired(base: list[float], head: list[float]) -> tuple[float, float, float]:
    diffs = [(h - b) / b * 100 for b, h in zip(base, head)]
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
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            exes = {
                (arm, profiled): build(ref, work, profiled)
                for arm, ref in (("base", args.base), ("head", args.head))
                for profiled in sorted({p for p, _ in workloads.values()})
            }
            samples: dict[tuple[str, str], list[list[float]]] = {
                (w, a): [] for w in workloads for a in ("base", "head")
            }
            for rep in range(args.reps):
                for workload, (profiled, params) in workloads.items():
                    for arm in ("base", "head") if rep % 2 == 0 else ("head", "base"):
                        exe = str(exes[(arm, profiled)])
                        env = PROFILER_ENV if profiled else None
                        values = list(map(float, run([exe, *map(str, params)], env=env).split()))
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
    table = "\n".join(rows) + "\n"
    print(table)
    if args.summary:
        args.summary.write_text(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
