"""The CLAUDE.md rule-12 ledger, computed from perf-ab samples (#573 B4).

Rule 12: memory buys CPU at 3:1 and nothing else is free.

- Memory credit: `saved%` = (baseline - candidate) / (baseline - ideal) of the cell's peak RSS,
  where `ideal` is the RSS floor (the process's RSS before the work plus the live requested bytes,
  which ci/perf_ab.c prints).
- Allowed CPU (and throughput) regression on that cell = `saved% / 3`.
- A cell whose RSS did not fall (a control) allows none beyond perf-ab's noise.
- The verdict is PASS when the whole interval of the regression lies within the allowance, FAIL
  when the whole interval lies beyond it, and INCONCLUSIVE otherwise (rerun; it is not a pass).

Everything here works on the per-repetition samples, so it can be re-run on the JSON artifact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import perf_ab_stats

Verdict = Literal["PASS", "FAIL", "INCONCLUSIVE"]

MEMORY_TO_CPU = 3.0  # rule 12: CPU is allowed to cost this many times less than memory saves
PEAK = "peak RSS MiB"
DRAIN = "RSS 0.5 s after drain MiB"
# the rule-12 metrics, and which way is worse (+1: a larger value is a regression)
RULE12: dict[str, int] = {
    "ops/s": -1,
    "cpu s": +1,
    "minor faults": +1,
    "p99 operation ns": +1,
    PEAK: +1,
    DRAIN: +1,
}
MEMORY_METRICS = (PEAK, DRAIN)
NO_CREDIT_METRICS = (*MEMORY_METRICS, "p99 operation ns")


@dataclass(frozen=True)
class Cell:
    """One rule-12 metric of one row: the change, what it may cost, and the verdict."""

    metric: str
    base_median: float
    head_median: float
    change: tuple[float, float, float]  # percent, head vs base: median, low, high
    regression: tuple[float, float]  # the same interval, signed so that positive is worse
    allowance: float  # percent
    verdict: Verdict


def _median(values: Sequence[float]) -> float:
    return perf_ab_stats.sign_interval(values)[0]


def saved_percent(base: float, head: float, ideal: float) -> float:
    """The share of the reducible gap that head closes, 0..100 (0 when there is no gap)."""
    gap = base - ideal
    if gap <= 0:
        return 0.0
    return max(0.0, min(100.0, (base - head) / gap * 100))


def credit(base_peak: Sequence[float], head_peak: Sequence[float], ideal: float) -> float:
    """Memory credit of a cell: saved%, but only when the peak RSS fell beyond doubt (its whole
    paired interval below 0); a fall the samples cannot distinguish from noise earns nothing."""
    _, _, high = perf_ab_stats.paired_percent(base_peak, head_peak)
    if high >= 0:
        return 0.0
    return saved_percent(_median(base_peak), _median(head_peak), ideal)


def judge(regression: tuple[float, float], allowance: float) -> Verdict:
    low, high = regression
    if low > allowance:
        return "FAIL"
    if high <= allowance:
        return "PASS"
    return "INCONCLUSIVE"


def cells(
    base: Mapping[str, Sequence[float]],
    head: Mapping[str, Sequence[float]],
    ideal: float,
    noise: Mapping[str, float] | None = None,
) -> list[Cell]:
    """The ledger of one row: `base` and `head` map a metric name to its samples (MiB for the
    RSS metrics), `ideal` is the RSS floor in MiB, `noise` a per-metric floor in percent from a
    base-vs-base arm (absent: 0)."""
    saved = credit(base[PEAK], head[PEAK], ideal)
    result: list[Cell] = []
    for metric, worse in RULE12.items():
        change = perf_ab_stats.paired_percent(base[metric], head[metric])
        floor = (noise or {}).get(metric, 0.0)
        # the memory metrics may not regress at all beyond noise; CPU-like ones may cost saved%/3
        allowance = floor if metric in NO_CREDIT_METRICS else max(saved / MEMORY_TO_CPU, floor)
        mid, low, high = change
        regression = (low, high) if worse > 0 else (-high, -low)
        result.append(
            Cell(
                metric,
                _median(base[metric]),
                _median(head[metric]),
                (mid, low, high),
                regression,
                allowance,
                judge(regression, allowance),
            )
        )
    return result


def row_verdict(row: Sequence[Cell]) -> Verdict:
    verdicts = {cell.verdict for cell in row}
    if "FAIL" in verdicts:
        return "FAIL"
    if "INCONCLUSIVE" in verdicts:
        return "INCONCLUSIVE"
    return "PASS"


def table(rows: dict[str, tuple[float, list[Cell]]]) -> list[str]:
    """Markdown ledger: one line per row, `saved%` and each rule-12 metric's change, allowance
    and verdict. `rows` maps a workload to (saved%, its cells)."""
    lines = [
        "",
        "Rule-12 ledger (CLAUDE.md; ci/perf_ab_ledger.py): `saved%` of the reducible peak-RSS gap, "
        "then per metric the paired change [95% sign-test interval] against its allowance "
        "(saved%/3 for CPU, throughput and faults; noise for memory and for cells that saved none). "
        "INCONCLUSIVE is not a pass.",
        "",
        "| workload | saved% | verdict | " + " | ".join(RULE12) + " |",
        "|---|---|---|" + "---|" * len(RULE12),
    ]
    for workload, (saved, row) in rows.items():
        cols = [
            f"{c.change[0]:+.1f}% [{c.change[1]:+.1f}, {c.change[2]:+.1f}]"
            f" / {c.allowance:.1f}% **{c.verdict}**"
            for c in row
        ]
        lines.append(
            f"| {workload} | {saved:.0f}% | **{row_verdict(row)}** | " + " | ".join(cols) + " |"
        )
    return lines


def inconclusive(row: Sequence[Cell]) -> bool:
    return row_verdict(row) == "INCONCLUSIVE"
