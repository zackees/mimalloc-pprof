"""#528: benchmark-scaling's diagnostic-mode inputs and job summary."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scaling_diagnostic as diagnostic


def test_env_accepts_head_env_pairs() -> None:
    assert diagnostic.parse_env(" MIMALLOC_PURGE_DELAY=10  MIMALLOC_ARENA_PURGE_MULT=1 ") == {
        "MIMALLOC_PURGE_DELAY": "10",
        "MIMALLOC_ARENA_PURGE_MULT": "1",
    }
    assert diagnostic.parse_env("") == {}


@pytest.mark.parametrize(
    "text",
    [
        "MIMALLOC_PURGE_DELAY",
        "=10",
        "MIMALLOC_PURGE_DELAY=",
        "1ABC=2",
        "MIMALLOC_PURGE_DELAY=$(id)",
        "MIMALLOC_PURGE_DELAY=1;2",
        "MIMALLOC_PURGE_DELAY=1 MIMALLOC_PURGE_DELAY=2",
        "MIMALLOC_PROF=1",
        "MIMALLOC_MEMORY_EVENTS=1",
    ],
)
def test_env_rejects_anything_else(text: str) -> None:
    with pytest.raises(diagnostic.DiagnosticInputError):
        diagnostic.parse_env(text)


def test_cppdefs_share_perf_ab_grammar() -> None:
    assert diagnostic.parse_cppdefs("MI_ENABLE_LARGE_PAGES=0;MI_X  FOO=1.5") == [
        "MI_ENABLE_LARGE_PAGES=0",
        "MI_X",
        "FOO=1.5",
    ]
    for bad in ("1X=0", "X=", 'X="0"', "X=$(id)", "X=a,b", "-DX=1"):
        with pytest.raises(diagnostic.DiagnosticInputError):
            diagnostic.parse_cppdefs(bad)


def test_workload_filters_name_declared_patterns_and_points() -> None:
    assert diagnostic.parse_patterns("random-large, sparse-large-buffers") == [
        "sparse-large-buffers",
        "random-large",
    ]
    assert diagnostic.parse_threads("4 1") == [1, 4]
    for bad in ("thread-churn", "sparse-large-buffer"):
        with pytest.raises(diagnostic.DiagnosticInputError):
            diagnostic.parse_patterns(bad)
    for bad in ("5", "x", "-1"):
        with pytest.raises(diagnostic.DiagnosticInputError):
            diagnostic.parse_threads(bad)


@pytest.mark.parametrize("mode", ["full", "smoke"])
@pytest.mark.parametrize(
    "field", ["env", "cppdefs", "patterns", "threads"], ids=lambda value: value
)
def test_diagnostic_inputs_are_refused_outside_diagnostic_mode(mode: str, field: str) -> None:
    values = {"env": "", "cppdefs": "", "patterns": "", "threads": ""}
    values[field] = {
        "env": "MIMALLOC_PURGE_DELAY=10",
        "cppdefs": "MI_ENABLE_LARGE_PAGES=0",
        "patterns": "sparse-large-buffers",
        "threads": "1",
    }[field]
    with pytest.raises(diagnostic.DiagnosticInputError, match="mode: diagnostic"):
        diagnostic.validate_inputs(mode, blocks=3, **values)


def test_diagnostic_mode_validates_every_input() -> None:
    diagnostic.validate_inputs(
        "diagnostic", "MIMALLOC_PURGE_DELAY=10", "", "sparse-large-buffers", "1,4", 1
    )
    diagnostic.validate_inputs("full", "", "", "", "", 3)
    with pytest.raises(diagnostic.DiagnosticInputError):
        diagnostic.validate_inputs("diagnostic", "X=$(id)", "", "", "", 1)
    with pytest.raises(diagnostic.DiagnosticInputError):
        diagnostic.validate_inputs("diagnostic", "", "", "", "", 0)
    with pytest.raises(diagnostic.DiagnosticInputError):
        diagnostic.validate_inputs("publish", "", "", "", "", 3)


def raw_run() -> dict[str, object]:
    def sample(allocator: str, peak: int, phases: list[dict[str, object]]) -> dict[str, object]:
        return {
            "pattern": "sparse-large-buffers",
            "thread_count": 1,
            "allocator_id": allocator,
            "peak_rss_bytes": peak << 20,
            "diagnostic_peak_rss_bytes": (peak + 1) << 20,
            "diagnostic_rss_phases": phases,
        }

    phases: list[dict[str, object]] = [
        {"phase": "measured", "peak_rss_bytes": 30 << 20, "last_rss_bytes": 20 << 20},
        {"phase": "teardown", "peak_rss_bytes": 40 << 20, "last_rss_bytes": 35 << 20},
    ]
    return {
        "status": "diagnostic",
        "diagnostic": {
            "label": "DIAGNOSTIC, not publishable: mimalloc-pprof built with the default "
            "recipe, run with MIMALLOC_PURGE_DELAY=10",
            "patterns": ["sparse-large-buffers"],
            "thread_points": [1],
            "blocks": 1,
        },
        "samples": [sample("mimalloc-pprof", 50, phases), sample("jemalloc", 30, [])],
    }


def test_summary_labels_the_run_and_names_the_peak_phase(tmp_path: Path) -> None:
    text = diagnostic.summarize(raw_run())
    assert text.startswith("**DIAGNOSTIC, not publishable")
    assert "| sparse-large-buffers | 1 | mimalloc-pprof | 50.0 | 51.0 | 35.0 | teardown 1/1 |" in (
        text
    )
    assert "| sparse-large-buffers | 1 | jemalloc | 30.0 | 31.0 | - | - |" in text
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(raw_run()), encoding="utf-8")
    assert diagnostic.main(["summarize", "--raw", str(path)]) == 0


def test_summary_refuses_a_default_run() -> None:
    raw = raw_run()
    raw["status"] = "complete"
    with pytest.raises(diagnostic.DiagnosticInputError):
        diagnostic.summarize(raw)
