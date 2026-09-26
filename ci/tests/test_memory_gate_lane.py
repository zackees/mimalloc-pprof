"""The memory gate runs on every allocator-touching PR, whatever its labels (#518).

#501 regressed the memory gate and nobody saw it until after merge: internal PRs run
the minimal lane, and the gate lived only in the `ci-test`/`ci-full` DAG. These tests pin
the rule that closes that hole -- a diff touching `src/`, `include/` or `CMakeLists.txt`
selects `c-unit.yml`'s `memory-gate` job in minimal mode, a docs-only diff does not -- and
the workflow wiring that makes the selection fail closed through `PR test gate (c-unit)`.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from memory_gate_lane_decide import decide
from pr_ci_gate import failures, required_jobs

ROOT = Path(__file__).resolve().parents[2]
C_UNIT = ROOT / ".github" / "workflows" / "c-unit.yml"


def c_unit_jobs() -> dict[str, Any]:
    return yaml.safe_load(C_UNIT.read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize(
    "files",
    [
        ["src/arena.c"],
        ["src/prim/unix/prim.c"],
        ["include/mimalloc/types.h"],
        ["include/mimalloc.h", "docs/ci-gates.md"],
        ["CMakeLists.txt"],
        # The gate's own machinery: a change to it must be proven on a real run.
        ["test/test-memory-gate.c"],
        ["ci/memory_gate.py"],
        ["ci/memory-baselines/linux-pprof1.json"],
        ["ci/memory_gate_lane_decide.py"],
        [".github/workflows/c-unit.yml"],
    ],
)
def test_allocator_diff_selects_the_gate_in_minimal_mode(files: list[str]) -> None:
    run, reason = decide(files, labels=[], external=False)
    assert run, reason


@pytest.mark.parametrize(
    "files",
    [
        ["docs/ci-gates.md"],
        ["README.md", "CLAUDE.md"],
        ["rust/mimalloc-pprof/src/lib.rs"],
        ["test/test-api.c"],
        ["ci/pr_ci_gate.py"],
        ["cmake/toolchains/x86_64-apple-darwin.cmake"],
        # Prefix look-alikes are not the directories.
        ["srcdoc/notes.md", "includes.txt", "docs/CMakeLists.txt.md"],
        [],
    ],
)
def test_docs_and_unrelated_diffs_do_not_select_the_gate(files: list[str]) -> None:
    run, reason = decide(files, labels=[], external=False)
    assert not run, reason


@pytest.mark.parametrize(
    ("labels", "external"),
    [(["ci-test"], False), (["ci-full"], False), (["bug", "ci-test"], False), ([], True)],
)
def test_full_lane_runs_the_gate_in_run_linux_instead(labels: list[str], external: bool) -> None:
    # `run-linux` already runs the gate AND its leak control there; a second copy would
    # only cost a runner.
    run, reason = decide(["src/arena.c"], labels=labels, external=external)
    assert not run
    assert "run-linux" in reason


def test_pr_gate_requires_the_selected_minimal_gate() -> None:
    minimal = required_jobs("c-unit", False, False, False, True)
    assert {"memory-gate-decide", "memory-gate"} <= minimal
    assert "run-linux" not in minimal
    unselected = required_jobs("c-unit", False, False, False, False)
    assert "memory-gate-decide" in unselected
    assert "memory-gate" not in unselected


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", None])
def test_pr_gate_fails_closed_when_the_selected_gate_did_not_pass(result: str | None) -> None:
    needs: dict[str, object] = {
        "pr-ci-mode": {"result": "success", "outputs": {"external": "false"}},
        "resolve-candidate": {"result": "success"},
        "memory-gate-decide": {"result": "success", "outputs": {"run": "true"}},
    }
    if result is not None:
        needs["memory-gate"] = {"result": result}
    assert any(e.startswith("memory-gate:") for e in failures("c-unit", needs, False, False))
    needs["memory-gate"] = {"result": "success"}
    assert failures("c-unit", needs, False, False) == []


def test_pr_gate_requires_the_decision_itself() -> None:
    needs: dict[str, object] = {
        "pr-ci-mode": {"result": "success", "outputs": {"external": "false"}},
        "resolve-candidate": {"result": "success"},
        "memory-gate-decide": {"result": "failure"},
    }
    assert "memory-gate-decide: failure" in failures("c-unit", needs, False, False)


def test_decide_job_runs_on_every_pr_without_label_gating() -> None:
    job = c_unit_jobs()["memory-gate-decide"]
    assert job["if"] == "github.event_name == 'pull_request'"
    assert "pr-ci-mode" in job["needs"]
    step = next(s for s in job["steps"] if s.get("id") == "decide")
    assert "ci/memory_gate_lane_decide.py" in step["run"]
    assert "--external" in step["run"] and "--labels" in step["run"]
    assert step["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["EXTERNAL"] == "${{ needs.pr-ci-mode.outputs.external }}"
    assert job["outputs"]["run"] == "${{ steps.decide.outputs.run }}"
    checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0  # the three-dot diff needs the merge base


def test_minimal_gate_job_builds_only_the_gate_and_checks_the_baseline() -> None:
    jobs = c_unit_jobs()
    job = jobs["memory-gate"]
    assert "memory-gate-decide" in job["needs"]
    assert job["if"] == "needs.memory-gate-decide.outputs.run == 'true'"
    script = "\n".join(step.get("run", "") for step in job["steps"])
    # The same configuration as the `release` bundle the baseline was recorded from.
    release = next(
        r for r in jobs["build"]["strategy"]["matrix"]["include"] if r["config"] == "release"
    )
    assert release["cmake"] in script
    assert "--target mimalloc-test-memory-gate" in script
    assert "ci/memory_gate.py check result-*.json" in script
    assert "for i in 1 2 3 4 5 6 7 8" in script  # memory_gate.RUNS_EXPECTED
    gate = jobs["pr-test-gate"]
    assert {"memory-gate-decide", "memory-gate"} <= set(gate["needs"])
