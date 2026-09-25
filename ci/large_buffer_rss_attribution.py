#!/usr/bin/env python3
"""Attribute large-buffer peak RSS growth to workers in a benchmark-scaling raw run (#425, #422).

Input: the ``scaling-raw-run.json`` inside the ``benchmark-scaling-raw-<run_id>`` artifact
uploaded by ``.github/workflows/benchmark-scaling.yml``. Output: per-cell medians (peak RSS,
peak live bytes, RSS/live amplification, throughput), a least-squares fit of RSS and live
bytes against the worker count, the subject allocator's RSS ratio against each reference,
and a check that every allocator replayed the identical trace. Pure analysis; it measures
nothing.

Usage: large_buffer_rss_attribution.py RAW_JSON [--pattern NAME ...] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

MIB = 1024 * 1024
DEFAULT_PATTERNS: tuple[str, ...] = (
    "sparse-large-buffers",
    "random-large",
    "power-of-two-large",
    "large-class-persistent",
    "large-class-ephemeral",
)
DEFAULT_SUBJECT = "mimalloc-pprof"
DEFAULT_REFERENCES: tuple[str, ...] = ("jemalloc", "tcmalloc", "upstream-mimalloc")


class RawDataError(ValueError):
    """The raw scaling run is malformed or has nothing to analyse."""


@dataclass(frozen=True)
class Sample:
    pattern: str
    allocator_id: str
    thread_count: int
    block_id: int
    ordinal: int
    peak_rss_bytes: int
    live_bytes: int
    operation_count: int
    elapsed_ns: int
    checksum: int
    alloc_calls: int
    free_calls: int


@dataclass(frozen=True)
class Cell:
    pattern: str
    allocator_id: str
    thread_count: int
    n: int
    median_rss_mib: float
    median_live_mib: float
    median_amplification: float
    median_throughput_ops: float


@dataclass(frozen=True)
class Slope:
    pattern: str
    allocator_id: str
    rss_intercept_mib: float
    rss_slope_mib_per_worker: float
    live_slope_mib_per_worker: float
    excess_slope_mib_per_worker: float


@dataclass(frozen=True)
class Ratio:
    pattern: str
    thread_count: int
    reference: str
    ratio: float


def median(values: Sequence[float]) -> float:
    if not values:
        raise RawDataError("median of an empty sequence")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _str_field(record: Mapping[str, Any], name: str, where: str) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise RawDataError(f"{where}: missing or non-string field '{name}'")
    return value


def _int_field(record: Mapping[str, Any], name: str, where: str) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RawDataError(f"{where}: missing or non-integer field '{name}'")
    return value


def _positive(value: int, name: str, where: str) -> int:
    if value <= 0:
        raise RawDataError(f"{where}: field '{name}' must be positive, got {value}")
    return value


def load_samples(data: Mapping[str, Any], patterns: Sequence[str]) -> list[Sample]:
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list):
        raise RawDataError("missing or non-list top-level field 'samples'")
    wanted = set(patterns)
    samples: list[Sample] = []
    for index, raw in enumerate(cast(list[Any], raw_samples)):
        where = f"samples[{index}]"
        if not isinstance(raw, dict):
            raise RawDataError(f"{where}: not an object")
        record = cast(dict[str, Any], raw)
        pattern = _str_field(record, "pattern", where)
        if pattern not in wanted:
            continue
        raw_response = record.get("response")
        if not isinstance(raw_response, dict):
            raise RawDataError(f"{where}: missing or non-object field 'response'")
        response = cast(dict[str, Any], raw_response)
        in_response = f"{where}.response"
        samples.append(
            Sample(
                pattern=pattern,
                allocator_id=_str_field(record, "allocator_id", where),
                thread_count=_int_field(record, "thread_count", where),
                block_id=_int_field(record, "block_id", where),
                ordinal=_int_field(record, "ordinal", where),
                peak_rss_bytes=_positive(
                    _int_field(record, "peak_rss_bytes", where), "peak_rss_bytes", where
                ),
                live_bytes=_int_field(response, "peak_live_requested_bytes", in_response),
                operation_count=_positive(
                    _int_field(response, "operation_count", in_response),
                    "operation_count",
                    in_response,
                ),
                elapsed_ns=_positive(
                    _int_field(response, "elapsed_ns", in_response), "elapsed_ns", in_response
                ),
                checksum=_int_field(response, "checksum", in_response),
                alloc_calls=_int_field(response, "alloc_calls", in_response),
                free_calls=_int_field(response, "free_calls", in_response),
            )
        )
    if not samples:
        raise RawDataError(f"no samples match patterns {sorted(wanted)}")
    return samples


def cell_table(samples: Sequence[Sample]) -> list[Cell]:
    groups: dict[tuple[str, str, int], list[Sample]] = {}
    for sample in samples:
        key = (sample.pattern, sample.allocator_id, sample.thread_count)
        groups.setdefault(key, []).append(sample)
    cells: list[Cell] = []
    for pattern, allocator_id, thread_count in sorted(groups):
        group = groups[(pattern, allocator_id, thread_count)]
        amplification = (
            math.nan
            if any(s.live_bytes == 0 for s in group)
            else median([s.peak_rss_bytes / s.live_bytes for s in group])
        )
        cells.append(
            Cell(
                pattern=pattern,
                allocator_id=allocator_id,
                thread_count=thread_count,
                n=len(group),
                median_rss_mib=median([float(s.peak_rss_bytes) for s in group]) / MIB,
                median_live_mib=median([float(s.live_bytes) for s in group]) / MIB,
                median_amplification=amplification,
                median_throughput_ops=median(
                    [s.operation_count / (s.elapsed_ns / 1e9) for s in group]
                ),
            )
        )
    return cells


def fit_worker_slope(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    if len({x for x, _ in points}) < 2:
        raise RawDataError("a worker-count fit needs at least two distinct worker counts")
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    slope = covariance / variance
    return mean_y - slope * mean_x, slope


def slope_table(cells: Sequence[Cell]) -> list[Slope]:
    groups: dict[tuple[str, str], list[Cell]] = {}
    for cell in cells:
        groups.setdefault((cell.pattern, cell.allocator_id), []).append(cell)
    slopes: list[Slope] = []
    for pattern, allocator_id in sorted(groups):
        group = groups[(pattern, allocator_id)]
        try:
            intercept, rss_slope = fit_worker_slope(
                [(float(c.thread_count), c.median_rss_mib) for c in group]
            )
            _, live_slope = fit_worker_slope(
                [(float(c.thread_count), c.median_live_mib) for c in group]
            )
        except RawDataError as error:
            raise RawDataError(f"{pattern}/{allocator_id}: {error}") from error
        slopes.append(
            Slope(
                pattern=pattern,
                allocator_id=allocator_id,
                rss_intercept_mib=intercept,
                rss_slope_mib_per_worker=rss_slope,
                live_slope_mib_per_worker=live_slope,
                excess_slope_mib_per_worker=rss_slope - live_slope,
            )
        )
    return slopes


def ratio_table(
    cells: Sequence[Cell],
    subject: str = DEFAULT_SUBJECT,
    references: Sequence[str] = DEFAULT_REFERENCES,
) -> list[Ratio]:
    by_key = {(c.pattern, c.allocator_id, c.thread_count): c for c in cells}
    ratios: list[Ratio] = []
    for cell in cells:
        if cell.allocator_id != subject:
            continue
        for reference in references:
            other = by_key.get((cell.pattern, reference, cell.thread_count))
            if other is None or other.median_rss_mib == 0:
                continue
            ratios.append(
                Ratio(
                    pattern=cell.pattern,
                    thread_count=cell.thread_count,
                    reference=reference,
                    ratio=cell.median_rss_mib / other.median_rss_mib,
                )
            )
    return ratios


def trace_mismatches(samples: Sequence[Sample]) -> list[str]:
    groups: dict[tuple[str, int, int, int], list[Sample]] = {}
    for sample in samples:
        key = (sample.pattern, sample.thread_count, sample.block_id, sample.ordinal)
        groups.setdefault(key, []).append(sample)
    lines: list[str] = []
    for pattern, thread_count, block_id, ordinal in sorted(groups):
        group = groups[(pattern, thread_count, block_id, ordinal)]
        signatures = {(s.operation_count, s.checksum, s.alloc_calls, s.free_calls) for s in group}
        if len(signatures) <= 1:
            continue
        detail = ", ".join(
            f"{s.allocator_id}(ops={s.operation_count}, checksum={s.checksum}, "
            f"alloc={s.alloc_calls}, free={s.free_calls})"
            for s in sorted(group, key=lambda sample: sample.allocator_id)
        )
        lines.append(
            f"{pattern} workers={thread_count} block={block_id} ordinal={ordinal}: {detail}"
        )
    return lines


def _analyse(
    data: Mapping[str, Any], patterns: Sequence[str]
) -> tuple[list[Cell], list[Slope], list[Ratio], list[str]]:
    samples = load_samples(data, patterns)
    cells = cell_table(samples)
    return cells, slope_table(cells), ratio_table(cells), trace_mismatches(samples)


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _amplification(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:.2f}"


def render_markdown(data: Mapping[str, Any], patterns: Sequence[str]) -> str:
    cells, slopes, ratios, mismatches = _analyse(data, patterns)
    run = _section(data, "run")
    runner = _section(data, "runner")
    lines = [
        f"run {run.get('run_id', '?')}, source {run.get('source_sha', '?')}, "
        f"cpu {runner.get('cpu_model', '?')}, {runner.get('logical_cores', '?')} logical cores",
        "",
        "| pattern | allocator | workers | n | median peak RSS MiB | median peak live MiB "
        "| RSS/live | median ops/s |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {c.pattern} | {c.allocator_id} | {c.thread_count} | {c.n} | "
        f"{c.median_rss_mib:.1f} | {c.median_live_mib:.1f} | "
        f"{_amplification(c.median_amplification)} | {c.median_throughput_ops:.0f} |"
        for c in cells
    )
    lines += [
        "",
        "| pattern | allocator | RSS intercept MiB | RSS MiB/worker | live MiB/worker "
        "| excess MiB/worker |",
        "|---|---|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {s.pattern} | {s.allocator_id} | {s.rss_intercept_mib:.1f} | "
        f"{s.rss_slope_mib_per_worker:.1f} | {s.live_slope_mib_per_worker:.1f} | "
        f"{s.excess_slope_mib_per_worker:.1f} |"
        for s in slopes
    )
    lines += [
        "",
        f"| pattern | workers | reference | {DEFAULT_SUBJECT} RSS / reference RSS |",
        "|---|---:|---|---:|",
    ]
    lines.extend(
        f"| {r.pattern} | {r.thread_count} | {r.reference} | {r.ratio:.2f} |" for r in ratios
    )
    lines.append("")
    if mismatches:
        lines.append("trace check: MISMATCH")
        lines.extend(f"- {line}" for line in mismatches)
    else:
        lines.append("trace check: identical across allocators")
    return "\n".join(lines) + "\n"


def _json_row(row: Any) -> dict[str, Any]:
    return {
        key: None if isinstance(value, float) and math.isnan(value) else value
        for key, value in asdict(row).items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_json", type=Path, help="scaling-raw-run.json from the artifact")
    parser.add_argument(
        "--pattern",
        action="append",
        help=f"pattern to analyse; repeatable (default: {', '.join(DEFAULT_PATTERNS)})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    patterns: Sequence[str] = args.pattern or DEFAULT_PATTERNS
    raw_path: Path = args.raw_json
    try:
        loaded: Any = json.loads(raw_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RawDataError("top level is not a JSON object")
        data = cast(dict[str, Any], loaded)
        cells, slopes, ratios, mismatches = _analyse(data, patterns)
        if args.json:
            output = json.dumps(
                {
                    "cells": [_json_row(c) for c in cells],
                    "slopes": [_json_row(s) for s in slopes],
                    "ratios": [_json_row(r) for r in ratios],
                    "trace_mismatches": mismatches,
                },
                indent=2,
                allow_nan=False,
            )
            print(output)
        else:
            print(render_markdown(data, patterns), end="")
    except (RawDataError, json.JSONDecodeError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
