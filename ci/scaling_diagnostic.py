#!/usr/bin/env python3
"""benchmark-scaling's diagnostic mode (#528, #422 step 0 P4 + P5).

`validate` checks the dispatch inputs before anything is built, with the same grammar
`benchmark-scaling-run --diagnostic` re-checks at run time (and perf-ab's #527
`head_cppdefs` uses): `diagnostic_env` is space-separated KEY=VALUE pairs for the
mimalloc-pprof child only, `diagnostic_cppdefs` is NAME or NAME=VALUE defines for the
mimalloc-pprof build only, and the workload filters name declared patterns and worker
counts. Outside `mode: diagnostic` every diagnostic input must be empty.

`summarize` prints a diagnostic raw run as a markdown job summary: what the run changed,
then per cell and allocator the median timed peak RSS, the replay's peak and after-drain
RSS, and the phase that held the replay's peak.

    python3 ci/scaling_diagnostic.py validate --mode diagnostic --env 'MIMALLOC_PURGE_DELAY=10' \\
        --cppdefs '' --patterns sparse-large-buffers --threads 1,4
    python3 ci/scaling_diagnostic.py summarize --raw scaling-raw-run.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from benchmark_report import SCALING_PATTERN_IDS, SCALING_THREAD_POINTS
from perf_ab import CPPDEF

MODES = ("full", "smoke", "diagnostic")
# The scaling spawner forces these on every child after its own environment, so a
# diagnostic value would be silently overridden.
FORCED_ENV = ("MIMALLOC_PROF", "MIMALLOC_MEMORY_EVENTS")
# KEY=VALUE with perf-ab's CPPDEF value class (#527): means the same to the shell,
# CMake's list splitting and a reproduction command line.
ENV_PAIR = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[A-Za-z0-9_.+-]+)")
MIB = 1 << 20
DIAGNOSTIC_STATUS = "diagnostic"
MAX_BLOCKS = 40  # rust/benchmark-suite MAX_DIAGNOSTIC_BLOCKS (= DISTRIBUTION_BLOCKS)


class DiagnosticInputError(ValueError):
    """A diagnostic dispatch input is malformed."""


def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in text.split():
        match = ENV_PAIR.fullmatch(pair)
        if match is None:
            raise DiagnosticInputError(
                f"diagnostic_env: expected KEY=VALUE (VALUE of [A-Za-z0-9_.+-]), got {pair!r}"
            )
        key = match.group("key")
        if key in FORCED_ENV:
            raise DiagnosticInputError(
                f"diagnostic_env: {key} is forced to 0 on every scaling child"
            )
        if key in env:
            raise DiagnosticInputError(f"diagnostic_env: {key} is given twice")
        env[key] = match.group("value")
    return env


def parse_cppdefs(text: str) -> list[str]:
    defines = [item for item in re.split(r"[;\s]+", text) if item]
    for item in defines:
        if not CPPDEF.fullmatch(item):
            raise DiagnosticInputError(
                f"diagnostic_cppdefs: expected NAME or NAME=VALUE, got {item!r}"
            )
    return defines


def _items(text: str) -> list[str]:
    return [item for item in re.split(r"[,\s]+", text) if item]


def parse_patterns(text: str) -> list[str]:
    for item in _items(text):
        if item not in SCALING_PATTERN_IDS:
            raise DiagnosticInputError(
                f"diagnostic_patterns: {item!r} is not one of {', '.join(SCALING_PATTERN_IDS)}"
            )
    return [pattern for pattern in SCALING_PATTERN_IDS if pattern in _items(text)]


def parse_threads(text: str) -> list[int]:
    points: set[int] = set()
    for item in _items(text):
        if not item.isdigit() or int(item) not in SCALING_THREAD_POINTS:
            raise DiagnosticInputError(
                f"diagnostic_threads: {item!r} is not one of {list(SCALING_THREAD_POINTS)}"
            )
        points.add(int(item))
    return [point for point in SCALING_THREAD_POINTS if point in points]


def validate_inputs(
    mode: str, env: str, cppdefs: str, patterns: str, threads: str, blocks: int
) -> None:
    if mode not in MODES:
        raise DiagnosticInputError(f"mode: expected one of {MODES}, got {mode!r}")
    if mode != "diagnostic":
        named = [
            name
            for name, value in (
                ("diagnostic_env", env),
                ("diagnostic_cppdefs", cppdefs),
                ("diagnostic_patterns", patterns),
                ("diagnostic_threads", threads),
            )
            if value.strip()
        ]
        if named:
            raise DiagnosticInputError(
                f"{', '.join(named)} may only be set with mode: diagnostic (got mode: {mode})"
            )
        return
    parse_env(env)
    parse_cppdefs(cppdefs)
    parse_patterns(patterns)
    parse_threads(threads)
    if not 1 <= blocks <= MAX_BLOCKS:
        raise DiagnosticInputError(f"blocks: a diagnostic run takes 1..{MAX_BLOCKS}")


def _mib(value: float) -> str:
    return f"{value / MIB:.1f}"


def summarize(raw: dict[str, object]) -> str:
    diagnostic = raw.get("diagnostic")
    if raw.get("status") != DIAGNOSTIC_STATUS or not isinstance(diagnostic, dict):
        raise DiagnosticInputError("not a diagnostic scaling raw run")
    record = cast(dict[str, object], diagnostic)
    rows = [
        f"**{record['label']}**",
        "",
        f"Patterns: {', '.join(cast(list[str], record['patterns']))}; worker counts: "
        f"{', '.join(str(p) for p in cast(list[int], record['thread_points']))}; "
        f"{record['blocks']} paired block(s) per cell. Raw artifact only: nothing is "
        "validated for or published to benchmark-stats.",
        "",
        "Median MiB over blocks. *replay* is the separate live-telemetry replay: its peak, "
        "its last RSS sample in teardown (after the drain), and the phase that held its "
        "peak in each block.",
        "",
        "| pattern | workers | allocator | peak RSS | replay peak | replay after drain "
        "| replay peak phase |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    cells: dict[tuple[str, int, str], list[dict[str, object]]] = {}
    for sample in cast(list[dict[str, object]], raw.get("samples", [])):
        key = (
            cast(str, sample["pattern"]),
            cast(int, sample["thread_count"]),
            cast(str, sample["allocator_id"]),
        )
        cells.setdefault(key, []).append(sample)
    order = {pattern: index for index, pattern in enumerate(SCALING_PATTERN_IDS)}
    for (pattern, threads, allocator), samples in sorted(
        cells.items(), key=lambda item: (order.get(item[0][0], 99), item[0][1], item[0][2])
    ):
        peak = statistics.median(cast(int, s["peak_rss_bytes"]) for s in samples)
        replay = [cast(int, s.get("diagnostic_peak_rss_bytes", 0)) for s in samples]
        after: list[int] = []
        phases: Counter[str] = Counter()
        for sample in samples:
            marked = cast(list[dict[str, object]], sample.get("diagnostic_rss_phases", []))
            if not marked:
                continue
            top = max(marked, key=lambda phase: cast(int, phase["peak_rss_bytes"]))
            phases[cast(str, top["phase"])] += 1
            after += [
                cast(int, phase["last_rss_bytes"])
                for phase in marked
                if phase["phase"] == "teardown"
            ]
        rows.append(
            f"| {pattern} | {threads} | {allocator} | {_mib(peak)} | "
            + (_mib(statistics.median(replay)) if any(replay) else "-")
            + " | "
            + (_mib(statistics.median(after)) if after else "-")
            + " | "
            + (
                ", ".join(f"{phase} {count}/{len(samples)}" for phase, count in phases.items())
                or "-"
            )
            + " |"
        )
    return "\n".join(rows) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="check the dispatch inputs")
    check.add_argument("--mode", required=True)
    check.add_argument("--env", default="")
    check.add_argument("--cppdefs", default="")
    check.add_argument("--patterns", default="")
    check.add_argument("--threads", default="")
    check.add_argument("--blocks", type=int, default=3)
    show = commands.add_parser("summarize", help="markdown summary of a diagnostic raw run")
    show.add_argument("--raw", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validate_inputs(
                args.mode, args.env, args.cppdefs, args.patterns, args.threads, args.blocks
            )
            print(f"PASS benchmark-scaling inputs for mode: {args.mode}")
        else:
            raw = json.loads(args.raw.read_text(encoding="utf-8"))
            print(summarize(cast(dict[str, object], raw)), end="")
    except DiagnosticInputError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
