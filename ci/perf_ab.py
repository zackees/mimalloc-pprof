#!/usr/bin/env python3
"""Paired A/B of two allocator revisions on a few large-block workloads (#479).

Builds the minimal static library at --base and --head (every observability flag named
OFF; the BUILDS table adds flags for the profiler and chart-build rows), links
ci/perf_ab.c against each, and runs every workload --reps times with the arm
order alternating inside each repetition. Prints a markdown table of medians and the
median paired difference with a bootstrap 95% interval; a direction is only claimed when
the interval excludes zero. Linux only. Meant for a CI runner, not a busy dev machine.

    python3 ci/perf_ab.py --base origin/main --head HEAD [--reps 7] [--workloads random] [--summary out.md]
        [--head-env MIMALLOC_ARENA_PURGE_MULT=1] [--head-cppdefs MI_ENABLE_LARGE_PAGES=0]

--head-env sets allocator options on the head arm only: base and head at the same ref then
attribute a cost to one option (#506). --head-cppdefs does the same for a compile-time define:
the head arm's library is built with -DMI_EXTRA_CPPDEFS=<value> (#527).

The #422 diagnostic rows (exact sizes, size-class edges, the sparse-large-buffers twin; names end
in "(#422)") run only when --workloads names them, e.g. --workloads '#422' or '80 KiB|512 KiB';
the default selection is the gating rows alone. With a diagnostic row or --head-cppdefs, the
table also shows each probed size's bin and page kind on both arms (`perf_ab probe`).

--holes-report (#529, #422 E4) adds the allocator-internal snapshot: after the timed reps, each
selected row runs once more per arm, untimed, in an MI_DIAGNOSTICS=ON build of that arm, and
every worker prints mi_purge_holes_report() while it still holds its live slots (see
ci/perf_ab.c); the text is appended to the table, one collapsed block per row and arm.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

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
    # #529: only the untimed --holes-report replay; MI_DIAGNOSTICS adds the arena layout walk
    "diags": (["-DMI_DIAGNOSTICS=ON"], {}),
}
HOLES_KIND = "diags"
HOLES_ENV = {"PERF_AB_HOLES_REPORT": "1"}


class Params(NamedTuple):
    """One row's ci/perf_ab.c arguments, in its argv order (the release bound follows)."""

    threads: int
    generations: int
    min_size: int
    max_size: int
    ops: int  # per thread
    pause_ms: int = 0
    table_slots: int = 0
    sizes: str = "uniform"  # or "log": log-uniform over [min_size, max_size] (#527)
    slots: int = 8  # live slots per thread (#529: the fixed-budget rows split FIXED_BUDGET_SLOTS)


# name: (build, Params). A pause makes the row bursty (#486): BURSTS bursts per thread, everything
# freed after each, then idle. Table slots make it the Larson server workload (#506): one shared
# table of that many blocks per thread, rotating between the threads, so later frees are remote,
# and a per-table log that grows by doubling realloc as the chart harness's own does (see
# ci/perf_ab.c). min == max is an exact-size row.
LARSON_SLOTS = 5000  # the benchmark suite's larson live set per thread (mimalloc-bench's `larson ... 5000 ...`)
WORKLOADS: dict[str, tuple[str, Params]] = {
    "large-class/8": ("plain", Params(8, 1, 96 << 10, 512 << 10, 400000, 0, 0)),
    "large-class-ephemeral/8": ("plain", Params(8, 8, 96 << 10, 512 << 10, 400000, 0, 0)),
    "random-large/8": ("plain", Params(8, 1, 64 << 10, 4 << 20, 40000, 0, 0)),
    "random-large-bursty/8": ("plain", Params(8, 1, 64 << 10, 4 << 20, 40000, 300, 0)),
    "random-large/1": ("plain", Params(1, 1, 64 << 10, 4 << 20, 200000, 0, 0)),
    "small/8 (control)": ("plain", Params(8, 1, 16, 1024, 5000000, 0, 0)),
    # #506: the README's larson chart (8-1000 B); its peak RSS regressed with no row to show it
    # (ops per thread: the chart's calibrated cells, ~9.8M at 1 worker and ~2.5M at 8)
    "larson/1": ("plain", Params(1, 1, 8, 1000, 10000000, 0, LARSON_SLOTS)),
    "larson/8": ("plain", Params(8, 1, 8, 1000, 2500000, 0, LARSON_SLOTS)),
    "larson/8 (chart build)": ("chart", Params(8, 1, 8, 1000, 2500000, 0, LARSON_SLOTS)),
    "large-class/8 (profiler on)": ("pprof", Params(8, 1, 96 << 10, 512 << 10, 400000, 0, 0)),
    "large-class-ephemeral/8 (chart build)": (
        "chart",
        Params(8, 8, 96 << 10, 512 << 10, 400000, 0, 0),
    ),
    # the README chart's generation length (~12.5k ops per short-lived thread at 8 workers, #478):
    # a per-thread start/exit cost weighs 4x more than in the row above
    "large-class-ephemeral/8 short generations (chart build)": (
        "chart",
        Params(8, 8, 96 << 10, 512 << 10, 100000, 0, 0),
    ),
}
# #422 step 0 (E3, #527): diagnostic rows, not gates. They run only when --workloads selects them,
# so a perf-ab PR run costs what it did. Every name ends in DIAGNOSTIC_TAG; selecting the tag
# runs them all. Sizes: exact sizes across the size classes, and both sides of two edges -- the
# last medium bin (80 KiB) against the first large one (96 KiB), a 4 MiB large page (512 KiB)
# against a singleton page -- which `perf_ab probe` confirms per build. Screened at 1 and 4
# workers, as the proposal asks. DIAGNOSTIC_OPS is random-large/1's count, split over the workers;
# a size row gets as many ops as request as many bytes as the 4 MiB row (DIAGNOSTIC_BYTES), so a
# 64 KiB row runs long enough for its cpu columns to mean something.
DIAGNOSTIC_TAG = "(#422)"
DIAGNOSTIC_THREADS = (1, 4)
DIAGNOSTIC_OPS = 200000
KIB, MIB = 1 << 10, 1 << 20
DIAGNOSTIC_BYTES = DIAGNOSTIC_OPS * 4 * MIB
DIAGNOSTIC_SIZES = {
    "64 KiB": 64 * KIB,
    "80 KiB": 80 * KIB,
    "80 KiB+1": 80 * KIB + 1,
    "128 KiB": 128 * KIB,
    "512 KiB": 512 * KIB,
    "512 KiB+1": 512 * KIB + 1,
    "1 MiB": MIB,
    "4 MiB": 4 * MIB,
}
# rust/benchmark-suite ScalingPattern::LargeBuffers: 64 KiB-4 MiB log-uniform over 8 live slots
# (alloc 8 / free-oldest 6 / free-random 2, page-touched), which is ci/perf_ab.c's stream shape
SPARSE_LARGE_BUFFERS = (64 * KIB, 4 * MIB)
# #529 (#422 E4 / H3): a fixed AGGREGATE budget of live slots, split over the workers, so the
# live payload stays the same while the worker count grows; RSS that still grows with the workers
# is per-thread retention. At 8 workers it is the sparse twin's 8 slots per worker.
FIXED_BUDGET_SLOTS = 64
FIXED_BUDGET_THREADS = (1, 2, 4, 8)
DIAGNOSTIC_WORKLOADS: dict[str, tuple[str, Params]] = {
    **{
        f"size {label}/{t} {DIAGNOSTIC_TAG}": (
            "plain",
            Params(t, 1, size, size, DIAGNOSTIC_BYTES // size // t),
        )
        for t in DIAGNOSTIC_THREADS
        for label, size in DIAGNOSTIC_SIZES.items()
    },
    **{
        f"sparse-large-buffers/{t} {DIAGNOSTIC_TAG}": (
            "plain",
            Params(t, 1, *SPARSE_LARGE_BUFFERS, DIAGNOSTIC_OPS // t, sizes="log"),
        )
        for t in DIAGNOSTIC_THREADS
    },
    **{
        f"sparse-large-buffers {FIXED_BUDGET_SLOTS}-slot budget/{t} {DIAGNOSTIC_TAG}": (
            "plain",
            Params(
                t,
                1,
                *SPARSE_LARGE_BUFFERS,
                DIAGNOSTIC_OPS // t,
                sizes="log",
                slots=FIXED_BUDGET_SLOTS // t,
            ),
        )
        for t in FIXED_BUDGET_THREADS
    },
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


def run_stderr(cmd: list[str], env: dict[str, str]) -> str:
    """#529: a --holes-report child; its report is on stderr, its stdout line is not used."""
    return subprocess.run(cmd, env=env, check=True, capture_output=True, text=True).stderr


def holes_rows(reports: dict[tuple[str, str], str]) -> list[str]:
    rows = [
        "",
        "Allocator-internal snapshot (`--holes-report`, #529): one untimed run per row and arm in "
        "an MI_DIAGNOSTICS=ON build; every worker's `mi_purge_holes_report()` once all workers "
        "finished their stream, while each still holds its live slots.",
    ]
    for (workload, arm), text in reports.items():
        rows += [
            "",
            f"<details><summary>{workload}: {arm}</summary>",
            "",
            "```text",
            text.strip(),
            "```",
            "</details>",
        ]
    return rows


def build(arm: str, ref: str, work: Path, kind: str, cppdefs: list[str]) -> Path:
    # Paths named by arm ("base"/"head") and build (BUILDS), never by ref: every
    # executable path then has the same length, and so does the process's initial stack. A
    # longer argv/environment shifts stack alignment, the likely reason identical binaries
    # differed by 17% on the small-object row of #494's null run.
    tree, out = work / f"src-{arm}", work / f"bin-{arm}-{kind}"
    if not tree.exists():
        run(["git", "worktree", "add", "--detach", str(tree), ref], cwd=ROOT)
    extra = [f"-DMI_EXTRA_CPPDEFS={';'.join(cppdefs)}"] if cppdefs else []
    run(["cmake", "-S", str(tree), "-B", str(out), *FLAGS, *BUILDS[kind][0], *extra])
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


def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in text.split():
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--head-env: expected KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


# #527: a define is NAME or NAME=VALUE, kept to characters that mean the same to CMake's list
# splitting, the shell and the compiler command line (no ';', quotes, spaces or '$')
CPPDEF = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_.+-]+)?")


def parse_cppdefs(text: str) -> list[str]:
    """--head-cppdefs: defines separated by ';' (CMake's list form) or spaces."""
    defines = [item for item in re.split(r"[;\s]+", text) if item]
    for item in defines:
        if not CPPDEF.fullmatch(item):
            raise SystemExit(f"--head-cppdefs: expected NAME or NAME=VALUE, got {item!r}")
    return defines


def select(pattern: str) -> dict[str, tuple[str, Params]]:
    """The rows whose name contains one of the '|'-separated alternatives; by default the gating
    rows only (the diagnostic rows must be named)."""
    if not pattern:
        return dict(WORKLOADS)
    alternatives = pattern.split("|")
    rows = {**WORKLOADS, **DIAGNOSTIC_WORKLOADS}
    return {name: w for name, w in rows.items() if any(a in name for a in alternatives)}


def probe_sizes(workloads: dict[str, tuple[str, Params]], cppdefs: list[str]) -> list[int]:
    """The sizes whose bin and page kind the table reports: the exact-size rows selected, and
    every diagnostic size when the head arm is built with a define (it may move an edge)."""
    sizes = {p.min_size for _, p in workloads.values() if p.min_size == p.max_size}
    if cppdefs or any(name.endswith(DIAGNOSTIC_TAG) for name in workloads):
        sizes.update(DIAGNOSTIC_SIZES.values())
    return sorted(sizes)


def probe(exe: Path, sizes: list[int]) -> dict[int, str]:
    """`perf_ab probe`: size -> "bin N, B B block, K/page, kind" for that arm's build."""
    found: dict[int, str] = {}
    for line in run([str(exe), "probe", *map(str, sizes)]).splitlines():
        size, bin_, block, blocks, kind = line.split()
        found[int(size)] = f"bin {bin_}, {int(block):,} B block, {blocks}/page, {kind}"
    return found


def probe_rows(sizes: list[int], base: dict[int, str], head: dict[int, str]) -> list[str]:
    rows = [
        "",
        "Bins and page kinds (`perf_ab probe`: one request of each size alone in a fresh heap; "
        "**bold** where the arms differ).",
        "",
        "| size | base | head |",
        "|---|---|---|",
    ]
    for size in sizes:
        b, h = base[size], head[size]
        if b != h:
            b, h = f"**{b}**", f"**{h}**"
        rows.append(f"| {size:,} | {b} | {h} |")
    return rows


def header(
    base: str, head: str, head_env: str, cppdefs: list[str], cpu: str, reps: int
) -> list[str]:
    arms = f"`{base}` vs `{head}`"
    if head_env.strip():
        arms += f" with `{head_env.strip()}` on head"
    rows: list[str] = []
    if cppdefs:  # first, so the table cannot be mistaken for a default build's
        rows += [
            f"**Head arm built with `-DMI_EXTRA_CPPDEFS={';'.join(cppdefs)}`** "
            "(base: the default build).",
            "",
        ]
    rows += [
        arms + f" on {cpu}, {reps} paired reps, alternating order. "
        "Median base -> head, then median paired difference [bootstrap 95%]; "
        "**bold** when the interval excludes 0.",
        "",
        "| workload | " + " | ".join(METRICS) + " |",
        "|---|" + "---|" * len(METRICS),
    ]
    return rows


def cpu_model() -> str:
    # an effect can depend on the CPU the runner happens to get (#478: -6.5% on some runners,
    # 0 on others), so every table says which one it came from
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown CPU"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument(
        "--workloads",
        default="",
        help="only the workloads whose name contains this text ('|' separates alternatives); "
        f"the diagnostic rows ('{DIAGNOSTIC_TAG}') run only when named",
    )
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--holes-report",
        action="store_true",
        help="after the timed reps, one untimed MI_DIAGNOSTICS=ON run per row and arm printing "
        "every worker's mi_purge_holes_report() (#529)",
    )
    parser.add_argument(
        "--head-env",
        default="",
        help="KEY=VALUE pairs (space separated) set on the head arm only, e.g. MIMALLOC_ARENA_PURGE_MULT=1",
    )
    parser.add_argument(
        "--head-cppdefs",
        default="",
        help="defines (';' or space separated) the head arm's library is built with, as "
        "-DMI_EXTRA_CPPDEFS, e.g. MI_ENABLE_LARGE_PAGES=0",
    )
    args = parser.parse_args()
    head_env = parse_env(args.head_env)
    cppdefs = parse_cppdefs(args.head_cppdefs)
    workloads = select(args.workloads)
    if not workloads:
        parser.error(f"no workload matches {args.workloads!r}")
    bound_ms = int(json.loads(RATCHET.read_text())["bound_ms"])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            kinds = {k for k, _ in workloads.values()}
            if args.holes_report:
                kinds.add(HOLES_KIND)
            exes = {
                (arm, kind): build(arm, ref, work, kind, cppdefs if arm == "head" else [])
                for arm, ref in (("base", args.base), ("head", args.head))
                for kind in sorted(kinds)
            }
            sizes = probe_sizes(workloads, cppdefs)
            probe_kind = min(k for _, k in exes)  # a bin is the same in every BUILDS kind
            probes = {
                arm: probe(exes[(arm, probe_kind)], sizes) if sizes else {}
                for arm in ("base", "head")
            }
            samples: dict[tuple[str, str], list[list[float]]] = {
                (w, a): [] for w in workloads for a in ("base", "head")
            }
            for rep in range(args.reps):
                for workload, (kind, params) in workloads.items():
                    for arm in ("base", "head") if rep % 2 == 0 else ("head", "base"):
                        exe = str(exes[(arm, kind)])
                        env = {
                            **os.environ,
                            **BUILDS[kind][1],
                            **(head_env if arm == "head" else {}),
                        }
                        cmd = [exe, *map(str, params), str(bound_ms)]
                        values = list(map(float, run(cmd, env=env).split()))
                        for index, metric in enumerate(METRICS):
                            if metric in IN_MIB:
                                values[index] /= 2**20
                        samples[(workload, arm)].append(values)
            reports: dict[tuple[str, str], str] = {}
            if args.holes_report:  # untimed, after every timed rep
                for workload, (_kind, params) in workloads.items():
                    for arm in ("base", "head"):
                        env = {**os.environ, **HOLES_ENV, **(head_env if arm == "head" else {})}
                        cmd = [str(exes[(arm, HOLES_KIND)]), *map(str, params), str(bound_ms)]
                        reports[(workload, arm)] = run_stderr(cmd, env)
        finally:  # unregister the trees while they still exist: a prune here would find nothing to prune
            for tree in work.glob("src-*"):
                run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT)
    rows = header(args.base, args.head, args.head_env, cppdefs, cpu_model(), args.reps)
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
    if sizes:
        rows += probe_rows(sizes, probes["base"], probes["head"])
    if reports:
        rows += holes_rows(reports)
    table = "\n".join(rows) + "\n"
    print(table)
    if args.summary:
        args.summary.write_text(table)
    return 1 if late else 0


if __name__ == "__main__":
    raise SystemExit(main())
