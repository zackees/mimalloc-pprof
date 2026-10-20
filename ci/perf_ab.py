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
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

import perf_ab_ledger
import perf_ab_stats

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
    # #573 A4: --force-kind fp builds every row with frame pointers, so `perf record -g` and a
    # caller-report shim can walk the stack without DWARF (see docs/dev-loop.md)
    "fp": (["-DCMAKE_C_FLAGS=-fno-omit-frame-pointer"], {}),
}
HOLES_KIND = "diags"
# #573 A1: the untimed replay also counts (MI_STAT=1) and prints (MIMALLOC_SHOW_STATS) the
# allocator's own statistics -- pages, abandoned, reclaims, retires -- for both arms, so a row's
# cost is explained by event counts, not only by timing. Untimed, so nothing is perturbed.
KIND_CPPDEFS = {HOLES_KIND: ["MI_STAT=1"]}
HOLES_ENV = {"PERF_AB_HOLES_REPORT": "1", "MIMALLOC_SHOW_STATS": "1"}


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
    "p99 operation ns",
)
IN_MIB = {"peak RSS MiB", "RSS 0.5 s after drain MiB", "RSS at release bound MiB"}
# ci/perf_ab.c prints one more number (#573): the RSS floor -- the RSS before the work plus the
# live requested bytes -- which the rule-12 ledger takes as `ideal`. It is not a table column.
IDEAL_FIELD = len(METRICS)
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
        "an MI_DIAGNOSTICS=ON, MI_STAT=1 build; every worker's `mi_purge_holes_report()` once all "
        "workers finished their stream, while each still holds its live slots, then the "
        "allocator's statistics at exit (`MIMALLOC_SHOW_STATS`, #573).",
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


def build(
    arm: str,
    ref: str,
    work: Path,
    kind: str,
    cppdefs: list[str],
    diagnostic: bool = False,
    tree_name: str | None = None,
) -> Path:
    # Paths named by arm ("base"/"head") and build (BUILDS), never by ref: every
    # executable path then has the same length, and so does the process's initial stack. A
    # longer argv/environment shifts stack alignment, the likely reason identical binaries
    # differed by 17% on the small-object row of #494's null run.
    # (several head variants share the tree of `ref`, each with its own build directory)
    tree, out = work / f"src-{tree_name or arm}", work / f"bin-{arm}-{kind}"
    if not tree.exists():
        run(["git", "worktree", "add", "--detach", str(tree), ref], cwd=ROOT)
    defs = [*KIND_CPPDEFS.get(kind, []), *cppdefs]
    extra = [f"-DMI_EXTRA_CPPDEFS={';'.join(defs)}"] if defs else []
    run(["cmake", "-S", str(tree), "-B", str(out), *FLAGS, *BUILDS[kind][0], *extra])
    run(["cmake", "--build", str(out), "--target", "mimalloc-static", "--parallel"])
    exe = out / "perf_ab"
    lib = next(out.glob("libmimalloc*.a"))
    run(
        [
            "cc",
            "-O2",
            *(["-DPERF_AB_DIAGNOSTIC=1"] if diagnostic else []),
            "-I",
            str(tree / "include"),
            str(ROOT / "ci/perf_ab.c"),
            str(lib),
            "-lpthread",
            "-o",
            str(exe),
        ]
    )
    if not diagnostic:
        # #585: a separate replay samples operation latency, leaving every timed
        # throughput/RSS executable free of latency clocks and buffers.
        run(
            [
                "cc",
                "-O2",
                "-DPERF_AB_LATENCY=1",
                "-I",
                str(tree / "include"),
                str(ROOT / "ci/perf_ab.c"),
                str(lib),
                "-lpthread",
                "-o",
                str(exe.with_name("perf_ab_latency")),
            ]
        )
    return exe


# #573 B2: the first repetition of every row doubles as a sanity screen. A head arm whose memory
# is a multiple of base's is a bug, not noise (the slot-mask leak of #572 showed RSS at the
# release bound +1989% and the job still passed); noise on a 1-rep sample is far below this.
SANITY_FACTOR = 1.5
SANITY_SLACK_MIB = 8.0


def sanity_failures(
    first: dict[tuple[str, str], list[float]],
    workloads: Iterable[str],
    factor: float = SANITY_FACTOR,
    slack_mib: float = SANITY_SLACK_MIB,
) -> list[str]:
    """Rows whose head RSS in the first repetition exceeds factor x base + slack_mib."""
    failures: list[str] = []
    for workload in workloads:
        for metric in sorted(IN_MIB):
            index = METRICS.index(metric)
            base, head = first[(workload, "base")][index], first[(workload, "head")][index]
            if head > factor * base + slack_mib:
                failures.append(
                    f"{workload}: {metric} {base:,.1f} -> {head:,.1f} "
                    f"(more than {factor}x base + {slack_mib:g} MiB)"
                )
    return failures


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


ARM_ORDER_VERSION = "rotating-reversed-blocks-v1"


def arm_order(arms: list[str], rep: int) -> list[str]:
    """Balance execution positions in each block, reversing alternate blocks.

    Reversing a three-arm sequence alone leaves its middle arm in the same
    position every time. Rotations give every arm every position; reversing
    alternate blocks also reverses the adjacent pairs. With more than three
    arms this balances positions, not every possible carryover pairing.
    """
    block, shift = divmod(rep, len(arms))
    ordered = arms if block % 2 == 0 else list(reversed(arms))
    return ordered[shift:] + ordered[:shift]


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
        arms + f" on {cpu}, {reps} paired reps, order protocol {ARM_ORDER_VERSION}. "
        "Median base -> head, then median paired difference [95% sign-test interval]; "
        "**bold** when the interval excludes 0 (and, with a null arm, exceeds its noise floor) "
        "on a rule-12 metric.",
        f"Host exposes {logical_cpus()} logical CPUs; an 8-worker row has "
        f"{8 / logical_cpus():.2f} workers per logical CPU. Ratios above 1 are "
        "oversubscription, not scaling on dedicated cores.",
        "p99 is the nearest-rank percentile of sampled stream operations in a separate "
        "replay (free or replace, including page touches); sampling buffers and clocks "
        "are absent from the throughput/RSS runs.",
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


def logical_cpus() -> int:
    return len(os.sched_getaffinity(0))


# ---------------------------------------------------------------------------------------------
# arms, repetitions and the report (#573 B3-B6)

VARIANT_SEPARATOR = "||"  # --head-env / --head-cppdefs: one head arm per item, same VM and run
NULL_ARM = "null"  # base measured a second time: what "no difference" looks like on this VM
ADAPTIVE_STEP = 5  # --max-reps: reps added at a time to the rows still inconclusive
RULE12 = tuple(perf_ab_ledger.RULE12)


class Variant(NamedTuple):
    arm: str
    env_text: str
    env: dict[str, str]
    cppdefs: list[str]


def _split(text: str, flag: str) -> list[str]:
    items = [item.strip() for item in text.split(VARIANT_SEPARATOR)] if text.strip() else [""]
    if any(not item for item in items) and len(items) > 1:
        raise SystemExit(f"{flag}: an empty variant between {VARIANT_SEPARATOR!r} separators")
    return items


def parse_variants(head_env: str, head_cppdefs: str) -> list[Variant]:
    """The head arms: --head-env and --head-cppdefs each split on ' || ' into variants; a list of
    one is shared by all. One variant is the arm `head`; several are `hd01`, `hd02`, ... (names of
    the same length as `base`, which keeps every executable path the same length)."""
    envs = _split(head_env, "--head-env")
    defs = _split(head_cppdefs, "--head-cppdefs")
    count = max(len(envs), len(defs))
    for items, flag in ((envs, "--head-env"), (defs, "--head-cppdefs")):
        if len(items) not in (1, count):
            raise SystemExit(f"{flag}: {len(items)} variants, but the other flag has {count}")
    variants: list[Variant] = []
    for index in range(count):
        env_text = envs[index if len(envs) > 1 else 0]
        defs_text = defs[index if len(defs) > 1 else 0]
        arm = "head" if count == 1 else f"hd{index + 1:02d}"
        variants.append(Variant(arm, env_text, parse_env(env_text), parse_cppdefs(defs_text)))
    return variants


def measure(exe: Path, params: Params, env: dict[str, str], bound_ms: int) -> list[float]:
    """One child run: the METRICS, then the RSS floor; byte counts in MiB."""
    values = list(map(float, run([str(exe), *map(str, params), str(bound_ms)], env=env).split()))
    latency = float(
        run(
            [str(exe.with_name("perf_ab_latency")), *map(str, params), str(bound_ms)], env=env
        ).strip()
    )
    if not math.isfinite(latency) or latency <= 0:
        raise ValueError(f"invalid p99 latency: {latency}")
    values.insert(METRICS.index("p99 operation ns"), latency)
    for index, metric in enumerate(METRICS):
        if metric in IN_MIB:
            values[index] /= 2**20
    values[IDEAL_FIELD] /= 2**20
    return values


def column(samples: list[list[float]], metric: str) -> list[float]:
    return [s[METRICS.index(metric)] for s in samples]


def ledger_row(
    base: list[list[float]], head: list[list[float]], null: list[list[float]] | None
) -> tuple[float, list[perf_ab_ledger.Cell]]:
    """The rule-12 ledger of one row and one head arm: (saved%, cells). The RSS floor is base's
    median; a null arm turns its base-vs-base intervals into per-metric noise floors."""
    ideal = perf_ab_stats.sign_interval([s[IDEAL_FIELD] for s in base])[0]
    noise = (
        {
            m: perf_ab_stats.noise_floor(
                perf_ab_stats.paired_percent(column(base, m), column(null, m))
            )
            for m in RULE12
        }
        if null
        else None
    )
    b = {m: column(base, m) for m in RULE12}
    h = {m: column(head, m) for m in RULE12}
    saved = perf_ab_ledger.credit(b[perf_ab_ledger.PEAK], h[perf_ab_ledger.PEAK], ideal)
    return saved, perf_ab_ledger.cells(b, h, ideal, noise)


def render_variant(
    variant: Variant,
    args: argparse.Namespace,
    workloads: dict[str, tuple[str, Params]],
    samples: dict[tuple[str, str], list[list[float]]],
    bound_ms: int,
    cpu: str,
    probes: dict[str, dict[int, str]],
    sizes: list[int],
    reports: dict[tuple[str, str], str],
) -> tuple[list[str], bool]:
    """The tables of one head arm against base, and whether its release bound held."""
    reps = min(len(samples[(w, variant.arm)]) for w in workloads)
    rows = header(args.base, args.head, variant.env_text, variant.cppdefs, cpu, reps)
    with_null = NULL_ARM in {a for _, a in samples}
    ledger: dict[str, tuple[float, list[perf_ab_ledger.Cell]]] = {}
    for workload in workloads:
        base, head = samples[(workload, "base")], samples[(workload, variant.arm)]
        null = samples[(workload, NULL_ARM)] if with_null else None
        cells: list[str] = []
        for metric in METRICS:
            interval = perf_ab_stats.paired_percent(column(base, metric), column(head, metric))
            noise = (
                perf_ab_stats.noise_floor(
                    perf_ab_stats.paired_percent(column(base, metric), column(null, metric))
                )
                if null
                else None
            )
            mid, low, high = interval
            delta = f"{mid:+.1f}% [{low:+.1f}, {high:+.1f}]"
            if metric in RULE12 and perf_ab_stats.beyond_noise(interval, noise):
                delta = f"**{delta}**"
            b = statistics.median(column(base, metric))
            h = statistics.median(column(head, metric))
            cells.append(f"{b:,.4g} -> {h:,.4g}<br>{delta}")
        rows.append(f"| {workload} | " + " | ".join(cells) + " |")
        ledger[workload] = ledger_row(base, head, null)
    # #491: the release promise, per workload, on head; the worst P95 is the evidence a lower
    # bound in ci/release_ratchet.json needs (`p95_release_ms`)
    release = METRICS.index("release ms")
    p95 = {
        w: percentile([s[release] for s in samples[(w, variant.arm)]], RELEASE_PERCENTILE)
        for w in workloads
    }
    late = {w: p for w, p in p95.items() if p > bound_ms}
    worst = max(p95, key=lambda w: p95[w])
    rows += [
        "",
        f"Release bound (ci/release_ratchet.json): {bound_ms} ms; head P{RELEASE_PERCENTILE} "
        f"release, worst workload: {p95[worst]:.0f} ms ({worst}) -- "
        + (", ".join(f"**{w}: {p:.0f} ms, LATE**" for w, p in late.items()) or "held"),
    ]
    rows += perf_ab_ledger.table(ledger)
    if with_null:
        rows += [
            "",
            f"Noise floor: a base-vs-base arm ran in the same rotation; a difference is bold only "
            f"when it exceeds what that arm's interval reached. A control cell counts as the same "
            f"when its whole interval lies inside +-{perf_ab_stats.EQUIVALENCE_MARGIN_PCT:g}%.",
        ]
    if sizes:
        rows += probe_rows(sizes, probes["base"], probes[variant.arm])
    own = {k: v for k, v in reports.items() if k[1] in ("base", variant.arm)}
    if own:
        rows += holes_rows(own)
    return rows, not late


def preserve_builds(exes: dict[tuple[str, str], Path], output: Path) -> None:
    """Keep the exact measured children before temporary build trees disappear."""
    output.mkdir(parents=True, exist_ok=False)
    digests: dict[str, str] = {}
    for (arm, kind), exe in sorted(exes.items()):
        if arm == NULL_ARM:  # aliases base's executable
            continue
        destination = output / arm / kind
        destination.mkdir(parents=True)
        for source in (exe, exe.with_name("perf_ab_latency")):
            target = destination / source.name
            shutil.copy2(source, target)
            digests[str(target.relative_to(output))] = hashlib.sha256(
                target.read_bytes()
            ).hexdigest()
    (output / "sha256.json").write_text(json.dumps(digests, indent=2) + "\n")


def artifact(
    args: argparse.Namespace,
    variants: list[Variant],
    workloads: dict[str, tuple[str, Params]],
    samples: dict[tuple[str, str], list[list[float]]],
    bound_ms: int,
    cpu: str,
) -> dict[str, Any]:
    """The raw per-repetition samples with everything needed to reproduce and re-judge them."""

    def rev(ref: str) -> str:
        return run(["git", "rev-parse", ref], cwd=ROOT).strip()

    return {
        "version": 1,
        "base": {"ref": args.base, "sha": rev(args.base)},
        "head": {"ref": args.head, "sha": rev(args.head)},
        "cpu": cpu,
        "logical_cpus": logical_cpus(),
        "arm_order_protocol": {
            "version": ARM_ORDER_VERSION,
            "arms": ["base", *(v.arm for v in variants), *([NULL_ARM] if args.null_arm else [])],
        },
        "latency_protocol": {
            "version": "sampled-stream-operation-v1",
            "percentile": 99,
            "replay": "separate executable; timing/RSS executable has no sampling",
            "operation": "free or replace, including page touches",
            "sampling": "power-of-two stride targeting at most 4096 samples per worker",
        },
        "reps": args.reps,
        "max_reps": args.max_reps,
        "release_bound_ms": bound_ms,
        "fields": [*METRICS, "ideal RSS MiB"],
        "variants": [{"arm": v.arm, "env": v.env, "cppdefs": v.cppdefs} for v in variants],
        "workloads": {
            name: {"kind": kind, "params": params._asdict()}
            for name, (kind, params) in workloads.items()
        },
        "samples": {
            name: {arm: samples[(name, arm)] for arm in sorted({a for _, a in samples})}
            for name in workloads
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument(
        "--max-reps",
        type=int,
        default=0,
        help=f"adaptive reps (#573 B6): after --reps, add {ADAPTIVE_STEP} at a time to the rows "
        "whose rule-12 ledger is still INCONCLUSIVE, up to this many (0 = no extra reps)",
    )
    parser.add_argument(
        "--null-arm",
        action="store_true",
        help="measure base a second time in the rotation (#573 B3): its base-vs-base intervals are "
        "the per-cell noise floor",
    )
    parser.add_argument(
        "--workloads",
        default="",
        help="only the workloads whose name contains this text ('|' separates alternatives); "
        f"the diagnostic rows ('{DIAGNOSTIC_TAG}') run only when named",
    )
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--preserve-builds",
        type=Path,
        help="copy the exact timing/latency executables and SHA-256 digests here after measurements",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="write the raw per-repetition samples, SHAs, defines, environment and CPU here (#573 B4)",
    )
    parser.add_argument(
        "--holes-report",
        action="store_true",
        help="after the timed reps, one untimed MI_DIAGNOSTICS=ON run per row and arm printing "
        "every worker's mi_purge_holes_report() (#529)",
    )
    parser.add_argument(
        "--head-env",
        default="",
        help="KEY=VALUE pairs (space separated) set on the head arm only, e.g. "
        f"MIMALLOC_ARENA_PURGE_MULT=1; several variants separated by {VARIANT_SEPARATOR!r} run "
        "as several head arms of one rotation (#573 B5)",
    )
    parser.add_argument(
        "--head-cppdefs",
        default="",
        help="defines (';' or space separated) the head arm's library is built with, as "
        f"-DMI_EXTRA_CPPDEFS, e.g. MI_ENABLE_LARGE_PAGES=0; variants separated by {VARIANT_SEPARATOR!r}",
    )
    parser.add_argument(
        "--no-sanity-gate",
        action="store_true",
        help=f"do not stop after the first repetition when a head RSS metric exceeds "
        f"{SANITY_FACTOR}x base + {SANITY_SLACK_MIB:g} MiB (#573)",
    )
    parser.add_argument(
        "--force-kind",
        choices=sorted(BUILDS),
        help="build every row with this BUILDS kind (e.g. fp: frame pointers for perf/callers, #573 A4)",
    )
    args = parser.parse_args()
    variants = parse_variants(args.head_env, args.head_cppdefs)
    workloads = select(args.workloads)
    if not workloads:
        parser.error(f"no workload matches {args.workloads!r}")
    if args.force_kind:
        workloads = {n: (args.force_kind, p) for n, (_, p) in workloads.items()}
    args.max_reps = max(args.max_reps, args.reps)
    bound_ms = int(json.loads(RATCHET.read_text())["bound_ms"])
    head_arms = [v.arm for v in variants]
    arms = ["base", *head_arms, *([NULL_ARM] if args.null_arm else [])]
    env_of = {"base": {}, NULL_ARM: {}, **{v.arm: v.env for v in variants}}
    cpu = cpu_model()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        exes: dict[tuple[str, str], Path] = {}
        try:
            wanted: set[str] = {k for k, _ in workloads.values()}
            if args.holes_report:
                wanted.add(HOLES_KIND)
            kinds = sorted(wanted)
            for kind in kinds:
                exes[("base", kind)] = build("base", args.base, work, kind, [])
            for v in variants:
                for kind in kinds:
                    exes[(v.arm, kind)] = build(
                        v.arm, args.head, work, kind, v.cppdefs, tree_name="head"
                    )
            for kind in kinds:
                exes[(NULL_ARM, kind)] = exes[("base", kind)]  # the same binary: only luck differs
            sizes = probe_sizes(workloads, [d for v in variants for d in v.cppdefs])
            probe_kind = min(kinds)  # a bin is the same in every BUILDS kind
            probes = {
                arm: probe(exes[(arm, probe_kind)], sizes) if sizes else {}
                for arm in ["base", *head_arms]
            }
            samples: dict[tuple[str, str], list[list[float]]] = {
                (w, a): [] for w in workloads for a in arms
            }

            def run_rep(rep: int, rows: Iterable[str]) -> None:
                for workload in rows:
                    kind, params = workloads[workload]
                    for arm in arm_order(arms, rep):
                        env = {**os.environ, **BUILDS[kind][1], **env_of[arm]}
                        samples[(workload, arm)].append(
                            measure(exes[(arm, kind)], params, env, bound_ms)
                        )

            for rep in range(args.reps):
                run_rep(rep, workloads)
                if rep == 0 and not args.no_sanity_gate:
                    broken: list[str] = []
                    for arm in head_arms:
                        first = {(w, "base"): samples[(w, "base")][0] for w in workloads}
                        first |= {(w, "head"): samples[(w, arm)][0] for w in workloads}
                        broken += [f"[{arm}] {m}" for m in sanity_failures(first, workloads)]
                    if broken:  # fail in minutes, not after the whole run (#573)
                        message = "sanity gate (first repetition, #573):\n  " + "\n  ".join(broken)
                        print(message)
                        if args.json:
                            partial = artifact(args, variants, workloads, samples, bound_ms, cpu)
                            partial.update(
                                complete=False,
                                failure=message,
                                requested_reps=args.reps,
                                reps=rep + 1,
                            )
                            args.json.write_text(json.dumps(partial, indent=1) + "\n")
                        if args.summary:
                            args.summary.write_text(message + "\n")
                        return 1
            # #573 B6: extra reps only where the ledger cannot yet say pass or fail
            done = args.reps
            while done < args.max_reps:
                pending = [
                    w
                    for w in workloads
                    if any(
                        perf_ab_ledger.inconclusive(
                            ledger_row(
                                samples[(w, "base")],
                                samples[(w, arm)],
                                samples[(w, NULL_ARM)] if args.null_arm else None,
                            )[1]
                        )
                        for arm in head_arms
                    )
                ]
                if not pending:
                    break
                for rep in range(done, min(done + ADAPTIVE_STEP, args.max_reps)):
                    run_rep(rep, pending)
                done = min(done + ADAPTIVE_STEP, args.max_reps)
            reports: dict[tuple[str, str], str] = {}
            if args.holes_report:  # untimed, after every timed rep
                for workload, (_kind, params) in workloads.items():
                    for arm in ["base", *head_arms]:
                        env = {**os.environ, **HOLES_ENV, **env_of[arm]}
                        cmd = [str(exes[(arm, HOLES_KIND)]), *map(str, params), str(bound_ms)]
                        reports[(workload, arm)] = run_stderr(cmd, env)
            data = (
                artifact(args, variants, workloads, samples, bound_ms, cpu) if args.json else None
            )
            if args.json and data is not None:
                args.json.write_text(json.dumps(data, indent=1) + "\n")
        finally:  # unregister the trees while they still exist: a prune here would find nothing to prune
            try:
                if args.preserve_builds:
                    preserve_builds(exes, args.preserve_builds)
            finally:
                for tree in work.glob("src-*"):
                    run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT)
    rows: list[str] = []
    held = True
    for variant in variants:
        if len(variants) > 1:
            rows += [
                "",
                f"### {variant.arm}: `{variant.env_text or '-'}` / `{';'.join(variant.cppdefs) or '-'}`",
                "",
            ]
        text, ok = render_variant(
            variant, args, workloads, samples, bound_ms, cpu, probes, sizes, reports
        )
        rows += text
        held = held and ok
    table = "\n".join(rows) + "\n"
    print(table)
    if args.summary:
        args.summary.write_text(table)
    return 0 if held else 1


if __name__ == "__main__":
    raise SystemExit(main())
