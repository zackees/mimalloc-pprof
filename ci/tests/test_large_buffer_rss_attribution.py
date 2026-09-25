"""RED/GREEN contracts for #425's large-buffer RSS attribution tool."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from large_buffer_rss_attribution import (
    DEFAULT_PATTERNS,
    MIB,
    RawDataError,
    cell_table,
    fit_worker_slope,
    load_samples,
    median,
    ratio_table,
    slope_table,
    trace_mismatches,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ci/large_buffer_rss_attribution.py"
PATTERN = "sparse-large-buffers"
THREADS = (1, 2, 4)


def approx(actual: float, expected: float) -> bool:
    """Float equality for the fixture's exact arithmetic (pytest ships no type stubs)."""
    return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)


@contextmanager
def raises(exc_type: type[BaseException], match: str | None = None) -> Generator[None, None, None]:
    """Minimal typed ``pytest.raises``: the block must raise ``exc_type`` matching ``match``."""
    try:
        yield
    except exc_type as exc:
        if match is not None:
            assert re.search(match, str(exc)), f"{match!r} not found in {exc!s}"
        return
    raise AssertionError(f"{exc_type.__name__} not raised")


def fixture_data() -> dict[str, Any]:
    """RSS = 10 + 20t MiB (mimalloc-pprof) or 10 + 10t MiB (jemalloc); live = 5t MiB.

    The three ordinals of each cell sit at -1, 0 and +1 MiB around that value, so the
    median is exact and the tool has to pick the middle sample.
    """
    rss_per_worker = {"mimalloc-pprof": 20, "jemalloc": 10}
    samples: list[dict[str, Any]] = []
    for pattern in (PATTERN, "sparse-tiny-hot"):
        for allocator_id, per_worker in rss_per_worker.items():
            for thread_count in THREADS:
                for ordinal in range(3):
                    rss_mib = 10 + per_worker * thread_count + (ordinal - 1)
                    samples.append(
                        {
                            "pattern": pattern,
                            "allocator_id": allocator_id,
                            "thread_count": thread_count,
                            "block_id": 0,
                            "ordinal": ordinal,
                            "peak_rss_bytes": rss_mib * MIB,
                            "response": {
                                "operation_count": 1000 * thread_count,
                                "elapsed_ns": 1_000_000_000,
                                "peak_live_requested_bytes": 5 * thread_count * MIB,
                                "checksum": 7 * thread_count + ordinal,
                                "alloc_calls": 500 * thread_count,
                                "free_calls": 500 * thread_count,
                            },
                        }
                    )
    return {
        "run": {"run_id": 36160378572, "source_sha": "6c93b7ba"},
        "runner": {"cpu_model": "fixture cpu", "logical_cores": 4},
        "allocators": [{"allocator_id": name, "source_sha": "0" * 40} for name in rss_per_worker],
        "samples": samples,
    }


def find_sample(data: dict[str, Any], allocator_id: str, thread_count: int) -> dict[str, Any]:
    for sample in data["samples"]:
        if (
            sample["pattern"] == PATTERN
            and sample["allocator_id"] == allocator_id
            and sample["thread_count"] == thread_count
            and sample["ordinal"] == 1
        ):
            return sample
    raise AssertionError("fixture sample not found")


def run_cli(
    tmp_path: Path, data: dict[str, Any] | str, *extra: str
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "scaling-raw-run.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_median_odd_even_and_empty() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5
    with raises(RawDataError):
        median([])


def test_fit_worker_slope_recovers_line_and_rejects_one_x() -> None:
    intercept, slope = fit_worker_slope([(1.0, 30.0), (2.0, 50.0), (4.0, 90.0)])
    assert approx(intercept, 10.0)
    assert approx(slope, 20.0)
    with raises(RawDataError):
        fit_worker_slope([(2.0, 1.0), (2.0, 5.0)])


def test_cell_table_filters_patterns_and_takes_medians() -> None:
    cells = cell_table(load_samples(fixture_data(), [PATTERN]))
    assert {c.pattern for c in cells} == {PATTERN}
    assert [(c.allocator_id, c.thread_count) for c in cells] == [
        ("jemalloc", 1),
        ("jemalloc", 2),
        ("jemalloc", 4),
        ("mimalloc-pprof", 1),
        ("mimalloc-pprof", 2),
        ("mimalloc-pprof", 4),
    ]
    by_key = {(c.allocator_id, c.thread_count): c for c in cells}
    pprof = by_key[("mimalloc-pprof", 2)]
    assert pprof.n == 3
    assert approx(pprof.median_rss_mib, 50.0)
    assert approx(pprof.median_live_mib, 10.0)
    assert approx(pprof.median_amplification, 5.0)
    assert approx(pprof.median_throughput_ops, 2000.0)
    jemalloc = by_key[("jemalloc", 4)]
    assert approx(jemalloc.median_rss_mib, 50.0)
    assert approx(jemalloc.median_amplification, 2.5)


def test_cell_table_amplification_is_nan_without_live_bytes() -> None:
    data = fixture_data()
    find_sample(data, "jemalloc", 1)["response"]["peak_live_requested_bytes"] = 0
    cells = cell_table(load_samples(data, [PATTERN]))
    cell = next(c for c in cells if c.allocator_id == "jemalloc" and c.thread_count == 1)
    assert math.isnan(cell.median_amplification)


def test_slope_table_excess_slope() -> None:
    slopes = slope_table(cell_table(load_samples(fixture_data(), [PATTERN])))
    by_allocator = {s.allocator_id: s for s in slopes}
    assert set(by_allocator) == {"mimalloc-pprof", "jemalloc"}
    pprof = by_allocator["mimalloc-pprof"]
    assert approx(pprof.rss_intercept_mib, 10.0)
    assert approx(pprof.rss_slope_mib_per_worker, 20.0)
    assert approx(pprof.live_slope_mib_per_worker, 5.0)
    assert approx(pprof.excess_slope_mib_per_worker, 15.0)
    assert approx(by_allocator["jemalloc"].excess_slope_mib_per_worker, 5.0)


def test_ratio_table_against_reference() -> None:
    ratios = ratio_table(cell_table(load_samples(fixture_data(), [PATTERN])))
    # Only jemalloc is present; missing references are skipped, not errors.
    assert {r.reference for r in ratios} == {"jemalloc"}
    at_four = next(r for r in ratios if r.thread_count == 4)
    assert at_four.pattern == PATTERN
    assert approx(at_four.ratio, 90.0 / 50.0)
    assert approx(at_four.ratio, 1.8)


def test_trace_mismatches_identical_and_divergent() -> None:
    data = fixture_data()
    assert trace_mismatches(load_samples(data, [PATTERN])) == []
    find_sample(data, "jemalloc", 4)["response"]["checksum"] += 1
    mismatches = trace_mismatches(load_samples(data, [PATTERN]))
    assert len(mismatches) == 1
    assert f"{PATTERN} workers=4 block=0 ordinal=1" in mismatches[0]
    assert "jemalloc" in mismatches[0]
    assert "mimalloc-pprof" in mismatches[0]


def test_load_samples_rejects_missing_and_nonpositive_fields() -> None:
    data = fixture_data()
    del find_sample(data, "mimalloc-pprof", 2)["peak_rss_bytes"]
    with raises(RawDataError, match="peak_rss_bytes"):
        load_samples(data, [PATTERN])
    data = fixture_data()
    find_sample(data, "jemalloc", 1)["response"]["elapsed_ns"] = 0
    with raises(RawDataError, match="elapsed_ns"):
        load_samples(data, [PATTERN])
    with raises(RawDataError):
        load_samples(fixture_data(), ["no-such-pattern"])


def test_cli_markdown_json_mismatch_and_malformed(tmp_path: Path) -> None:
    result = run_cli(tmp_path, fixture_data())
    assert result.returncode == 0, result.stderr
    assert "trace check: identical across allocators" in result.stdout
    assert "36160378572" in result.stdout

    result = run_cli(tmp_path, fixture_data(), "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {"cells", "slopes", "ratios", "trace_mismatches"}
    # The default pattern set keeps sparse-large-buffers and drops sparse-tiny-hot.
    assert PATTERN in DEFAULT_PATTERNS
    assert {c["pattern"] for c in report["cells"]} == {PATTERN}
    assert report["trace_mismatches"] == []

    mismatched = fixture_data()
    find_sample(mismatched, "jemalloc", 2)["response"]["checksum"] += 1
    result = run_cli(tmp_path, mismatched)
    assert result.returncode == 1
    assert "workers=2 block=0 ordinal=1" in result.stdout

    result = run_cli(tmp_path, "{not json")
    assert result.returncode == 2
    assert result.stderr.startswith("error: ")
