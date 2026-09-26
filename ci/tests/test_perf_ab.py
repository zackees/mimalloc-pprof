"""perf-ab's head-only define input and the #422 diagnostic rows (#527), the fixed-budget rows
and the --holes-report snapshot (#529)."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import perf_ab

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


def run_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *argv: str
) -> tuple[str, list[tuple[str, list[str]]], list[list[str]]]:
    """main() with the builds and children faked: which arm got which defines, the child
    command lines, and the summary."""
    builds: list[tuple[str, list[str]]] = []
    children: list[list[str]] = []

    def build(arm: str, ref: str, work: Path, kind: str, cppdefs: list[str]) -> Path:
        builds.append((arm, cppdefs) if kind != perf_ab.HOLES_KIND else (f"{arm}:{kind}", cppdefs))
        return work / f"bin-{arm}-{kind}" / "perf_ab"

    def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        if cmd[1:2] == ["probe"]:
            arm = "head" if "bin-head" in cmd[0] else "base"
            kind = "singleton" if arm == "head" else "large"
            return "".join(f"{s} 50 {s} 8 {kind}\n" for s in cmd[2:])
        if cmd[0] == "git":
            return ""
        children.append(cmd)
        return "1 1 1 1 1048576 1048576 1048576 0\n"

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
    assert perf_ab.main() == 0
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


def test_workflow_passes_head_cppdefs_through_the_environment() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["head_cppdefs"]["default"] == ""
    step = workflow["jobs"]["ab"]["steps"][-1]
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
    step = workflow["jobs"]["ab"]["steps"][-1]
    assert step["env"]["HOLES_REPORT"] == "${{ inputs.holes_report && '--holes-report' || '' }}"
    assert " $HOLES_REPORT " in step["run"]
