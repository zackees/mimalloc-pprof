"""perf-ab's head-only define input and the #422 diagnostic rows (#527), the fixed-budget rows
and the --holes-report snapshot (#529)."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import perf_ab
import perf_ab_ledger
import perf_ab_stats

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/perf-ab.yml"
SCALING_RS = ROOT / "rust/benchmark-suite/src/scaling.rs"
KIB, MIB = 1 << 10, 1 << 20


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("MI_ENABLE_LARGE_PAGES=0", ["MI_ENABLE_LARGE_PAGES=0"]),
        ("MI_A=1;MI_B=2", ["MI_A=1", "MI_B=2"]),
        ("  MI_A=1 MI_B ;MI_C=0x10 ", ["MI_A=1", "MI_B", "MI_C=0x10"]),
    ],
)
def test_cppdefs_accepts_names_and_values(text: str, expected: list[str]) -> None:
    assert perf_ab.parse_cppdefs(text) == expected


@pytest.mark.parametrize(
    "text",
    ["-DMI_A=1", "1MI=2", "MI_A=", "MI_A=$(id)", "MI_A='1'", 'MI_A="1"', "MI_A=1,2", "MI_A=`x`"],
)
def test_cppdefs_rejects_anything_else(text: str) -> None:
    with pytest.raises(SystemExit, match="--head-cppdefs"):
        perf_ab.parse_cppdefs(text)


def fake_cmake(calls: list[list[str]]) -> Any:
    def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        calls.append(cmd)
        if cmd[:2] == ["cmake", "--build"]:
            (Path(cmd[2]) / "libmimalloc.a").write_text("")
        return ""

    return run


@pytest.mark.parametrize(
    ("cppdefs", "flag"),
    [
        ([], None),
        (["MI_ENABLE_LARGE_PAGES=0"], "-DMI_EXTRA_CPPDEFS=MI_ENABLE_LARGE_PAGES=0"),
        (["MI_A=1", "MI_B"], "-DMI_EXTRA_CPPDEFS=MI_A=1;MI_B"),
    ],
)
def test_build_passes_the_defines_to_cmake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cppdefs: list[str], flag: str | None
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(perf_ab, "run", fake_cmake(calls))
    (tmp_path / "src-head").mkdir()
    (tmp_path / "bin-head-plain").mkdir()
    perf_ab.build("head", "HEAD", tmp_path, "plain", cppdefs)
    configure = next(c for c in calls if c[:2] == ["cmake", "-S"])
    extra = [a for a in configure if a.startswith("-DMI_EXTRA_CPPDEFS")]
    assert extra == ([flag] if flag else [])
    assert configure[5 : 5 + len(perf_ab.FLAGS)] == perf_ab.FLAGS


def test_diagnostic_child_compile_flag_does_not_change_default_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(perf_ab, "run", fake_cmake(calls))
    for arm in ("base", "head"):
        (tmp_path / f"src-{arm}").mkdir()
        (tmp_path / f"bin-{arm}-plain").mkdir()
    perf_ab.build("base", "OLD", tmp_path, "plain", [])
    perf_ab.build("head", "NEW", tmp_path, "plain", [], diagnostic=True)
    compiles = [call for call in calls if call and call[0] == "cc"]
    assert len(compiles) == 3
    assert "-DPERF_AB_DIAGNOSTIC=1" not in compiles[0]
    assert "-DPERF_AB_LATENCY=1" not in compiles[0]
    assert "-DPERF_AB_LATENCY=1" in compiles[1]
    assert "-DPERF_AB_DIAGNOSTIC=1" in compiles[2]


def run_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *argv: str, expected_status: int = 0
) -> tuple[str, list[tuple[str, list[str]]], list[list[str]]]:
    """main() with the builds and children faked: which arm got which defines, the child
    command lines, and the summary."""
    builds: list[tuple[str, list[str]]] = []
    children: list[list[str]] = []

    def build(arm: str, ref: str, work: Path, kind: str, cppdefs: list[str], **_: object) -> Path:
        builds.append((arm, cppdefs) if kind != perf_ab.HOLES_KIND else (f"{arm}:{kind}", cppdefs))
        return work / f"bin-{arm}-{kind}" / "perf_ab"

    def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        if Path(cmd[0]).name == "perf_ab_latency":
            return "1000\n"
        if cmd[1:2] == ["probe"]:
            arm = "head" if "bin-head" in cmd[0] else "base"
            kind = "singleton" if arm == "head" else "large"
            return "".join(f"{s} 50 {s} 8 {kind}\n" for s in cmd[2:])
        if cmd[0] == "git":
            return ""
        children.append(cmd)
        return "1 1 1 1 1048576 1048576 1048576 0 1048576\n"

    def run_stderr(cmd: list[str], env: dict[str, str]) -> str:
        assert env["PERF_AB_HOLES_REPORT"] == "1"
        children.append(cmd)
        return f"holes report of {cmd[0]}\n"

    monkeypatch.setattr(perf_ab, "build", build)
    monkeypatch.setattr(perf_ab, "run", run)
    monkeypatch.setattr(perf_ab, "run_stderr", run_stderr)
    monkeypatch.setattr(perf_ab, "cpu_model", lambda: "test CPU")
    summary = tmp_path / "summary.md"
    argv_full = ["perf_ab.py", "--base", "B", "--head", "H", "--reps", "1", "--summary"]
    monkeypatch.setattr("sys.argv", [*argv_full, str(summary), *argv])
    assert perf_ab.main() == expected_status
    return summary.read_text(), builds, children


def test_only_the_head_arm_gets_the_defines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table, builds, _ = run_main(
        tmp_path,
        monkeypatch,
        "--workloads",
        "size 80 KiB+1/1",
        "--head-cppdefs",
        "MI_ENABLE_LARGE_PAGES=0",
    )
    assert sorted(builds) == [("base", []), ("head", ["MI_ENABLE_LARGE_PAGES=0"])]
    first = table.splitlines()[0]
    assert "-DMI_EXTRA_CPPDEFS=MI_ENABLE_LARGE_PAGES=0" in first
    assert first.startswith("**Head arm built with")


def test_default_run_has_no_define_line_and_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table, builds, children = run_main(tmp_path, monkeypatch, "--workloads", "small")
    assert all(cppdefs == [] for _, cppdefs in builds)
    assert "MI_EXTRA_CPPDEFS" not in table
    assert "perf_ab probe" not in table
    assert table.startswith("`B` vs `H` on test CPU")
    # the child's argv: the row's Params in order, then the release bound
    assert children[0][1:-1] == list(map(str, perf_ab.WORKLOADS["small/8 (control)"][1]))


def test_probe_table_marks_the_arms_that_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table, _, children = run_main(tmp_path, monkeypatch, "--workloads", "sparse-large-buffers/1")
    assert children[0][-3:-1] == ["log", "8"]
    assert "| 81,921 | **bin 50, 81,921 B block, 8/page, large** |" in table
    assert "**bin 50, 81,921 B block, 8/page, singleton**" in table
    # every diagnostic size is probed once a diagnostic row runs
    for size in perf_ab.DIAGNOSTIC_SIZES.values():
        assert f"| {size:,} |" in table


def test_diagnostic_rows_are_opt_in() -> None:
    assert perf_ab.select("") == perf_ab.WORKLOADS
    assert not any(perf_ab.DIAGNOSTIC_TAG in name for name in perf_ab.WORKLOADS)
    assert all(name.endswith(perf_ab.DIAGNOSTIC_TAG) for name in perf_ab.DIAGNOSTIC_WORKLOADS)
    assert perf_ab.select(perf_ab.DIAGNOSTIC_TAG) == perf_ab.DIAGNOSTIC_WORKLOADS
    # a filter predating #527 selects what it did
    assert list(perf_ab.select("random-large")) == [
        "random-large/8",
        "random-large-bursty/8",
        "random-large/1",
    ]


def test_edge_pairs_select_together() -> None:
    names = set(perf_ab.select("80 KiB|512 KiB"))
    assert names == {
        f"size {label}/{t} {perf_ab.DIAGNOSTIC_TAG}"
        for label in ("80 KiB", "80 KiB+1", "512 KiB", "512 KiB+1")
        for t in perf_ab.DIAGNOSTIC_THREADS
    }


def test_exact_and_edge_sizes_are_the_proposals() -> None:
    # #422 E3: exact 64 KiB, 128 KiB, 512 KiB, 1 MiB, 4 MiB; edges 80 KiB | +1, 512 KiB | +1
    expected = {64 * KIB, 128 * KIB, 512 * KIB, MIB, 4 * MIB, 80 * KIB, 80 * KIB + 1}
    expected.add(512 * KIB + 1)
    assert set(perf_ab.DIAGNOSTIC_SIZES.values()) == expected
    exact = [p for _, p in perf_ab.DIAGNOSTIC_WORKLOADS.values() if p.min_size == p.max_size]
    assert len(exact) == len(expected) * len(perf_ab.DIAGNOSTIC_THREADS)
    assert {p.min_size for p in exact} == expected
    for _, p in perf_ab.DIAGNOSTIC_WORKLOADS.values():
        per_row = (
            perf_ab.DIAGNOSTIC_OPS if p.sizes == "log" else perf_ab.DIAGNOSTIC_BYTES // p.min_size
        )
        assert p.ops == per_row // p.threads
        assert (p.generations, p.pause_ms, p.table_slots) == (1, 0, 0)


def test_sparse_twin_matches_the_benchmark_suite() -> None:
    source = SCALING_RS.read_text()
    spec = re.search(r"Self::LargeBuffers => PatternSpec \{(.*?)\}", source, re.S)
    assert spec is not None
    fields = dict(re.findall(r"(\w+): ([^,]+),", spec.group(1)))
    assert fields["log_uniform"] == "true"

    def product(expr: str) -> int:  # "64 * 1024"
        return math.prod(int(factor) for factor in expr.split("*"))

    bounds = (product(fields["min_size"]), product(fields["max_size"]))
    assert bounds == perf_ab.SPARSE_LARGE_BUFFERS
    # ci/perf_ab.c's stream: SLOTS live slots, choice % 16 split 8 / 6 / 2
    assert (fields["capacity"], fields["page_touch"]) == ("8", "true")
    weights = (fields["weight_alloc"], fields["weight_free_oldest"], fields["weight_free_random"])
    assert weights == ("8", "6", "2")
    assert fields["weight_realloc"] == "0"
    twins = [
        p
        for name, (_, p) in perf_ab.DIAGNOSTIC_WORKLOADS.items()
        if p.sizes == "log" and "budget" not in name
    ]
    assert {p.threads for p in twins} == set(perf_ab.DIAGNOSTIC_THREADS)
    assert all((p.min_size, p.max_size) == perf_ab.SPARSE_LARGE_BUFFERS for p in twins)


def ab_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    (step,) = [s for s in workflow["jobs"]["ab"]["steps"] if s.get("name") == "paired A/B"]
    return step


def test_workflow_passes_head_cppdefs_through_the_environment() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["head_cppdefs"]["default"] == ""
    step = ab_step(workflow)
    assert step["env"]["HEAD_REF"] == "${{ inputs.head_sha || 'HEAD' }}"
    assert '--head "$HEAD_REF"' in step["run"]
    checkout = workflow["jobs"]["ab"]["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert step["env"]["HEAD_CPPDEFS"] == "${{ inputs.head_cppdefs }}"
    assert '--head-cppdefs "$HEAD_CPPDEFS"' in step["run"]
    assert "${{" not in step["run"]  # inputs reach the shell as data, never as script


def test_fixed_budget_rows_hold_the_aggregate_live_slots() -> None:
    rows = {n: p for n, (_, p) in perf_ab.DIAGNOSTIC_WORKLOADS.items() if "budget" in n}
    assert {p.threads for p in rows.values()} == set(perf_ab.FIXED_BUDGET_THREADS)
    for p in rows.values():
        assert p.threads * p.slots == perf_ab.FIXED_BUDGET_SLOTS
        assert (p.min_size, p.max_size, p.sizes) == (*perf_ab.SPARSE_LARGE_BUFFERS, "log")
    # the child's slot array bounds the budget
    source = (ROOT / "ci/perf_ab.c").read_text()
    limit = re.search(r"#define MAX_SLOTS (\d+)", source)
    assert limit is not None and int(limit.group(1)) >= perf_ab.FIXED_BUDGET_SLOTS
    # "sparse-large-buffers/1" still selects the twin alone
    assert list(perf_ab.select("sparse-large-buffers/1")) == [
        f"sparse-large-buffers/1 {perf_ab.DIAGNOSTIC_TAG}"
    ]


def test_holes_report_is_an_extra_untimed_run_per_row_and_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table, builds, children = run_main(
        tmp_path,
        monkeypatch,
        "--workloads",
        "sparse-large-buffers/4",
        "--head-cppdefs",
        "MI_ENABLE_LARGE_PAGES=0",
        "--holes-report",
    )
    kind = perf_ab.HOLES_KIND
    assert (f"head:{kind}", ["MI_ENABLE_LARGE_PAGES=0"]) in builds
    assert (f"base:{kind}", []) in builds
    reported = [c for c in children if f"-{kind}" in c[0]]
    assert len(reported) == 2  # one per arm, after the timed rep
    assert children[-2:] == reported
    assert "<details><summary>sparse-large-buffers/4 (#422): head</summary>" in table
    assert "bin-head-diags/perf_ab" in table and "bin-base-diags/perf_ab" in table


def test_no_holes_report_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table, builds, _ = run_main(tmp_path, monkeypatch, "--workloads", "small")
    assert not any(":" in arm for arm, _ in builds)
    assert "<details>" not in table


def test_workflow_passes_holes_report_as_a_flag() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["holes_report"]["type"] == "boolean"
    assert inputs["holes_report"]["default"] is False
    step = ab_step(workflow)
    assert step["env"]["HOLES_REPORT"] == "${{ inputs.holes_report && '--holes-report' || '' }}"
    assert " $HOLES_REPORT " in step["run"]


def _first(base: list[float], head: list[float]) -> dict[tuple[str, str], list[float]]:
    return {("row", "base"): base, ("row", "head"): head}


def _metrics(**mib: float) -> list[float]:
    values = [1.0] * len(perf_ab.METRICS)
    for metric, value in mib.items():
        values[perf_ab.METRICS.index(metric.replace("_", " "))] = value
    return values


def test_sanity_gate_flags_a_memory_leak_the_release_bound_hid() -> None:
    # #572: RSS at the release bound reached +1989% while the job passed
    base = _metrics(**{"RSS_at_release_bound_MiB": 10.0})
    head = _metrics(**{"RSS_at_release_bound_MiB": 209.0})
    failures = perf_ab.sanity_failures(_first(base, head), ["row"])
    assert len(failures) == 1 and "RSS at release bound MiB" in failures[0]


def test_sanity_gate_passes_noise_and_the_slack_floor() -> None:
    base = _metrics(**{"peak_RSS_MiB": 100.0, "RSS_0.5_s_after_drain_MiB": 1.0})
    # +40% on a large base and +7 MiB on a tiny base are both inside the gate
    head = _metrics(**{"peak_RSS_MiB": 140.0, "RSS_0.5_s_after_drain_MiB": 8.0})
    assert perf_ab.sanity_failures(_first(base, head), ["row"]) == []


def test_sanity_gate_ignores_cpu_and_throughput() -> None:
    base = _metrics()
    head = _metrics()
    head[perf_ab.METRICS.index("cpu s")] = 50.0
    head[perf_ab.METRICS.index("ops/s")] = 0.001
    assert perf_ab.sanity_failures(_first(base, head), ["row"]) == []


def test_replay_build_counts_events_and_prints_them() -> None:
    # #573 A1: the untimed replay carries MI_STAT=1 and asks for the statistics at exit
    assert "MI_STAT=1" in perf_ab.KIND_CPPDEFS[perf_ab.HOLES_KIND]
    assert perf_ab.HOLES_ENV["MIMALLOC_SHOW_STATS"] == "1"
    # ... and only there: a timed build is not perturbed
    assert set(perf_ab.KIND_CPPDEFS) == {perf_ab.HOLES_KIND}


def test_workflow_gates_and_preserves_running_measurements() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["sanity_gate"]["default"] is True
    assert inputs["head_sha"]["default"] == ""
    assert workflow["concurrency"]["cancel-in-progress"] is False
    # another label's (skipped) run must not share the group of a live perf-ab run
    assert "github.event.label.name != 'perf-ab'" in workflow["concurrency"]["group"]
    condition = workflow["jobs"]["ab"]["if"]
    assert "github.event.label.name == 'perf-ab'" in condition
    step = ab_step(workflow)
    assert step["env"]["NO_SANITY_GATE"].endswith("'--no-sanity-gate' || '' }}")
    assert " $NO_SANITY_GATE " in step["run"]
    assert "${{" not in step["run"]


# ---- #573 B3-B6: statistics, ledger, variants, null arm, adaptive reps, artifact -------------


@pytest.mark.parametrize(
    ("n", "low", "high"),
    [(7, 1, 7), (15, 4, 12), (25, 8, 18), (3, 1, 3), (1, 1, 1)],
)
def test_sign_interval_is_the_order_statistic_interval(n: int, low: int, high: int) -> None:
    mid, lo, hi = perf_ab_stats.sign_interval(list(range(1, n + 1)))
    assert (lo, hi) == (low, high)
    assert lo <= mid <= hi


def test_sign_interval_has_at_least_its_coverage() -> None:
    # by construction: P(Binomial(n, 1/2) <= j) <= 0.025 for the j it picks, so the interval
    # misses the true median with probability <= 2 * 0.025
    for n in (6, 7, 11, 15, 25):
        _, low, high = perf_ab_stats.sign_interval(list(range(n)))
        j = int(low)
        assert 2 * perf_ab_stats._cdf(n, j) <= 0.05 + 1e-12  # pyright: ignore[reportPrivateUsage]
        assert high == n - 1 - j


def test_noise_floor_and_beyond_noise() -> None:
    null = perf_ab_stats.sign_interval([-2.0, 1.0, 3.0, 0.5, -1.0, 2.0, 0.0])
    floor = perf_ab_stats.noise_floor(null)
    assert floor == 3.0
    assert perf_ab_stats.beyond_noise((5.0, 4.0, 6.0), floor)
    assert not perf_ab_stats.beyond_noise((2.0, 1.0, 3.0), floor)  # excludes 0, inside the noise
    assert not perf_ab_stats.beyond_noise((5.0, -1.0, 9.0), None)  # includes 0
    assert perf_ab_stats.equivalent((0.5, -1.0, 2.0))
    assert not perf_ab_stats.equivalent((0.5, -1.0, 4.0))


def ledger_samples(base: float, head: float, spread: float = 0.0) -> dict[str, list[float]]:
    return {"x": [base + spread * (i % 3 - 1) for i in range(7)], "y": [head] * 7}


def metric_columns(
    peak: tuple[float, float], cpu: tuple[float, float], ops: tuple[float, float]
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    def arm(index: int) -> dict[str, list[float]]:
        return {
            "ops/s": [ops[index] * (1 + 0.001 * i) for i in range(7)],
            "cpu s": [cpu[index] * (1 + 0.001 * i) for i in range(7)],
            "p99 operation ns": [1000.0] * 7,
            "minor faults": [1000.0 * (1 + 0.001 * i) for i in range(7)],
            perf_ab_ledger.PEAK: [peak[index] * (1 + 0.001 * i) for i in range(7)],
            perf_ab_ledger.DRAIN: [50.0 * (1 + 0.001 * i) for i in range(7)],
        }

    return arm(0), arm(1)


def test_ledger_allows_a_third_of_the_saved_gap_in_cpu() -> None:
    # 200 -> 100 MiB against an ideal of 50: 66.7% of the gap saved, so up to +22.2% CPU
    base, head = metric_columns((200.0, 100.0), (10.0, 12.0), (1000.0, 1000.0))
    saved, cells = perf_ab_ledger.cells(base, head, 50.0), None
    assert (
        abs(
            perf_ab_ledger.credit(base[perf_ab_ledger.PEAK], head[perf_ab_ledger.PEAK], 50.0)
            - 66.67
        )
        < 0.1
    )
    cells = perf_ab_ledger.cells(base, head, 50.0)
    cpu = next(c for c in cells if c.metric == "cpu s")
    assert abs(cpu.allowance - 22.2) < 0.1
    assert cpu.verdict == "PASS"  # +20% is within +22.2%
    del saved


def test_ledger_fails_cpu_beyond_the_allowance_and_controls_allow_none() -> None:
    base, head = metric_columns((200.0, 100.0), (10.0, 14.0), (1000.0, 1000.0))  # +40% CPU
    cpu = next(c for c in perf_ab_ledger.cells(base, head, 50.0) if c.metric == "cpu s")
    assert cpu.verdict == "FAIL"
    # a control: no RSS credit, so +5% CPU is a regression
    base, head = metric_columns((200.0, 200.0), (10.0, 10.5), (1000.0, 1000.0))
    cells = perf_ab_ledger.cells(base, head, 50.0)
    cpu = next(c for c in cells if c.metric == "cpu s")
    assert cpu.allowance == 0.0 and cpu.verdict == "FAIL"
    assert perf_ab_ledger.row_verdict(cells) == "FAIL"


def test_ledger_throughput_is_signed_the_other_way() -> None:
    base, head = metric_columns((200.0, 200.0), (10.0, 10.0), (1000.0, 900.0))  # -10% ops/s
    ops = next(c for c in perf_ab_ledger.cells(base, head, 50.0) if c.metric == "ops/s")
    assert ops.regression[0] > 0 and ops.verdict == "FAIL"
    base, head = metric_columns((200.0, 200.0), (10.0, 10.0), (1000.0, 1100.0))  # faster
    ops = next(c for c in perf_ab_ledger.cells(base, head, 50.0) if c.metric == "ops/s")
    assert ops.verdict == "PASS"


def test_ledger_is_inconclusive_when_the_interval_straddles_the_allowance() -> None:
    base, head = metric_columns((200.0, 200.0), (10.0, 10.0), (1000.0, 1000.0))
    head["cpu s"] = [10.0 * f for f in (0.95, 0.98, 1.0, 1.03, 1.06, 1.1, 1.02)]
    cpu = next(c for c in perf_ab_ledger.cells(base, head, 50.0) if c.metric == "cpu s")
    assert cpu.verdict == "INCONCLUSIVE"
    assert perf_ab_ledger.inconclusive([cpu])


def test_a_peak_fall_inside_the_noise_earns_no_credit() -> None:
    base, head = metric_columns((200.0, 199.0), (10.0, 10.0), (1000.0, 1000.0))
    head[perf_ab_ledger.PEAK] = [199.0, 201.0, 198.0, 202.0, 197.0, 203.0, 200.0]
    assert perf_ab_ledger.credit(base[perf_ab_ledger.PEAK], head[perf_ab_ledger.PEAK], 50.0) == 0.0


def test_variants_split_on_the_separator_and_broadcast() -> None:
    one = perf_ab.parse_variants("", "")
    assert [(v.arm, v.env, v.cppdefs) for v in one] == [("head", {}, [])]
    several = perf_ab.parse_variants("A=1 || A=2 || B=3", "MI_X=1")
    assert [v.arm for v in several] == ["hd01", "hd02", "hd03"]
    assert [v.env for v in several] == [{"A": "1"}, {"A": "2"}, {"B": "3"}]
    assert all(v.cppdefs == ["MI_X=1"] for v in several)
    assert len({len(v.arm) for v in several} | {len("base")}) == 1  # equal-length arm names
    with pytest.raises(SystemExit):
        perf_ab.parse_variants("A=1 || A=2", "MI_X=1 || MI_X=2 || MI_X=3")
    with pytest.raises(SystemExit):
        perf_ab.parse_variants("A=1 ||  || A=3", "")


def test_several_variants_share_one_rotation_and_one_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table, builds, children = run_main(
        tmp_path,
        monkeypatch,
        "--workloads",
        "small",
        "--head-cppdefs",
        "MI_A=1 || MI_A=2",
        "--null-arm",
    )
    assert sorted(builds) == [("base", []), ("hd01", ["MI_A=1"]), ("hd02", ["MI_A=2"])]
    # base, hd01, hd02 and the null arm each ran once in the one repetition
    assert len(children) == 4
    assert "### hd01" in table and "### hd02" in table
    assert table.count("Rule-12 ledger") == 2
    assert "Noise floor" in table


def test_null_arm_reuses_the_base_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, children = run_main(tmp_path, monkeypatch, "--workloads", "small", "--null-arm")
    exes = [cmd[0] for cmd in children]
    assert len(exes) == 3 and len(set(exes)) == 2  # base twice, head once


@pytest.mark.parametrize("failed_sanity", [False, True])
def test_preserves_measured_children_on_success_and_sanity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_sanity: bool
) -> None:
    captured: list[tuple[set[tuple[str, str]], Path]] = []

    def capture(exes: dict[tuple[str, str], Path], output: Path) -> None:
        captured.append((set(exes), output))

    def sanity(*_: object) -> list[str]:
        return ["test failure"] if failed_sanity else []

    monkeypatch.setattr(perf_ab, "preserve_builds", capture)
    monkeypatch.setattr(perf_ab, "sanity_failures", sanity)
    output = tmp_path / "builds"
    run_main(
        tmp_path,
        monkeypatch,
        "--workloads",
        "small",
        "--null-arm",
        "--preserve-builds",
        str(output),
        expected_status=int(failed_sanity),
    )
    assert captured == [({("base", "plain"), ("head", "plain"), ("null", "plain")}, output)]


def test_samples_survive_a_build_preservation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_copy(*_: object) -> None:
        raise OSError("test copy failure")

    monkeypatch.setattr(perf_ab, "preserve_builds", fail_copy)
    output = tmp_path / "samples.json"
    with pytest.raises(OSError, match="test copy failure"):
        run_main(
            tmp_path,
            monkeypatch,
            "--workloads",
            "small",
            "--json",
            str(output),
            "--preserve-builds",
            str(tmp_path / "builds"),
        )
    assert len(json.loads(output.read_text())["samples"]["small/8 (control)"]["head"]) == 1


def test_candidate_occupies_every_execution_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, children = run_main(
        tmp_path, monkeypatch, "--workloads", "small", "--null-arm", "--reps", "6"
    )
    positions = [
        next(i for i, cmd in enumerate(children[start : start + 3]) if "bin-head" in cmd[0])
        for start in range(0, len(children), 3)
    ]
    assert [positions.count(i) for i in range(3)] == [2, 2, 2]


def test_three_arm_order_covers_all_permutations() -> None:
    arms = ["base", "head", "null"]
    assert [perf_ab.arm_order(arms, rep) for rep in range(6)] == [
        ["base", "head", "null"],
        ["head", "null", "base"],
        ["null", "base", "head"],
        ["null", "head", "base"],
        ["head", "base", "null"],
        ["base", "null", "head"],
    ]
    assert arms == ["base", "head", "null"]


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_arm_positions_are_balanced_with_additional_variants(count: int) -> None:
    arms = [f"arm{i}" for i in range(count)]
    for block in range(4):
        orders = [perf_ab.arm_order(arms, rep) for rep in range(block * count, (block + 1) * count)]
        assert all(sorted(order) == arms for order in orders)
        for position in range(count):
            assert sorted(order[position] for order in orders) == arms
    assert perf_ab.arm_order(arms, 1) == perf_ab.arm_order(arms, 1 + 2 * count)


def test_adaptive_reps_extend_only_the_inconclusive_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts: dict[str, int] = {}

    def noisy_run(
        cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> str:
        if cmd[0] == "git":
            return ""
        if Path(cmd[0]).name == "perf_ab_latency":
            return "1000\n"
        if cmd[1:2] == ["probe"]:
            return ""
        arm = "head" if "bin-head" in cmd[0] else "base"
        row = f"{cmd[1]}/{cmd[3]}"  # threads and min size identify the row
        counts[f"{row}:{arm}"] = counts.get(f"{row}:{arm}", 0) + 1
        n = counts[f"{row}:{arm}"]
        cpu = 10.0 if arm == "base" else 10.0 + (0.6 if (n % 2) else -0.6) * (n % 4)
        if cmd[3] == str(16):  # the small-object control: identical arms, conclusive at once
            cpu = 10.0
        return f"1000 {cpu} {cpu} 1000 209715200 52428800 52428800 0 52428800\n"

    def fake_build(
        arm: str, ref: str, work: Path, kind: str, cppdefs: list[str], **_: object
    ) -> Path:
        return work / f"bin-{arm}-{kind}" / "perf_ab"

    monkeypatch.setattr(perf_ab, "build", fake_build)
    monkeypatch.setattr(perf_ab, "run", noisy_run)
    monkeypatch.setattr(perf_ab, "cpu_model", lambda: "test CPU")
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(
        "sys.argv",
        [
            "perf_ab.py",
            "--base",
            "B",
            "--head",
            "H",
            "--reps",
            "5",
            "--max-reps",
            "15",
            "--workloads",
            "large-class/8|small/8",
            "--no-sanity-gate",
            "--summary",
            str(summary),
        ],
    )
    perf_ab.main()
    small = counts["8/16:head"]
    large = counts["8/98304:head"]
    assert small == 5  # conclusive after the first five: no extra reps
    assert 5 < large <= 15 and (large - 5) % perf_ab.ADAPTIVE_STEP == 0


def test_json_artifact_holds_the_raw_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "samples.json"
    run_main(tmp_path, monkeypatch, "--workloads", "small", "--null-arm", "--json", str(out))
    data = json.loads(out.read_text())
    assert data["version"] == 1 and data["cpu"] == "test CPU"
    assert data["fields"][-1] == "ideal RSS MiB" and len(data["fields"]) == len(perf_ab.METRICS) + 1
    row = data["samples"]["small/8 (control)"]
    assert set(row) == {"base", "head", "null"}
    assert all(len(reps) == 1 and len(reps[0]) == len(data["fields"]) for reps in row.values())
    assert data["workloads"]["small/8 (control)"]["params"]["threads"] == 8
    assert data["variants"] == [{"arm": "head", "env": {}, "cppdefs": []}]
    assert data["arm_order_protocol"] == {
        "version": "rotating-reversed-blocks-v1",
        "arms": ["base", "head", "null"],
    }


def test_force_kind_builds_every_row_with_frame_pointers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "-fno-omit-frame-pointer" in " ".join(perf_ab.BUILDS["fp"][0])
    _, builds, _ = run_main(tmp_path, monkeypatch, "--workloads", "small", "--force-kind", "fp")
    assert sorted(builds) == [("base", []), ("head", [])]


def test_workflow_uploads_the_samples_and_takes_the_new_inputs() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["null_arm"]["default"] is False
    assert inputs["max_reps"]["default"] == "0"
    assert inputs["build_report_only"]["default"] is False
    step = ab_step(workflow)
    assert " $NULL_ARM " in step["run"] and '--max-reps "$MAX_REPS"' in step["run"]
    assert "--json perf-ab-samples.json" in step["run"]
    assert "--preserve-builds perf-ab-builds" in step["run"]
    assert "${{" not in step["run"]
    (upload,) = [
        s
        for s in workflow["jobs"]["ab"]["steps"]
        if s.get("with", {}).get("name") == "perf-ab-samples"
    ]
    assert upload["if"] == "always()"  # a failed sanity gate still leaves its samples
    assert upload["with"]["name"] == "perf-ab-samples"
    (executables,) = [
        s
        for s in workflow["jobs"]["ab"]["steps"]
        if s.get("with", {}).get("name") == "perf-ab-builds"
    ]
    assert executables["if"] == "always()"


def test_p99_cannot_spend_memory_credit() -> None:
    base, head = metric_columns((200.0, 100.0), (10.0, 10.0), (1000.0, 1000.0))
    head["p99 operation ns"] = [1100.0] * 7
    latency = next(
        c for c in perf_ab_ledger.cells(base, head, 50.0) if c.metric == "p99 operation ns"
    )
    assert latency.allowance == 0
    assert latency.verdict == "FAIL"


def test_sanity_failure_saves_all_partial_arms_without_claiming_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def injected_failure(*_: object) -> list[str]:
        return ["injected memory failure"]

    monkeypatch.setattr(perf_ab, "sanity_failures", injected_failure)
    out = tmp_path / "partial.json"
    run_main(
        tmp_path,
        monkeypatch,
        "--workloads",
        "small/8",
        "--reps",
        "15",
        "--null-arm",
        "--json",
        str(out),
        expected_status=1,
    )
    data = json.loads(out.read_text())
    assert data["complete"] is False
    assert data["requested_reps"] == 15 and data["reps"] == 1
    assert "injected memory failure" in data["failure"]
    arms = data["samples"]["small/8 (control)"]
    assert set(arms) == {"base", "head", "null"}
    assert all(len(values) == 1 for values in arms.values())
