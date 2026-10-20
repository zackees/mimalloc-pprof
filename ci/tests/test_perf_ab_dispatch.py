"""ci/perf_ab_dispatch.py: pooling refuses other hosts, the ledger comes from the raw samples, and
the dispatch waits for the run it started (#573 B4, B8)."""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import argparse
import json
import re
import subprocess
import time
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import perf_ab
import perf_ab_dispatch

FIELDS = [*perf_ab.METRICS, "ideal RSS MiB"]


def reps(cpu: float, peak: float, n: int = 7) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(n):
        scale = 1 + 0.001 * i
        out.append(
            [1000.0, cpu * scale, cpu * scale, 1000.0, peak * scale, 50.0, 50.0, 0.0, 1000.0, 40.0]
        )
    return out


def artifact(cpu_model: str = "Test CPU") -> dict[str, Any]:
    return {
        "version": 1,
        "base": {"ref": "B", "sha": "b" * 40},
        "head": {"ref": "H", "sha": "h" * 40},
        "cpu": cpu_model,
        "reps": 7,
        "max_reps": 7,
        "release_bound_ms": 500,
        "fields": FIELDS,
        "variants": [{"arm": "head", "env": {}, "cppdefs": []}],
        "workloads": {"row/8": {"kind": "plain", "params": {"threads": 8}}},
        "samples": {"row/8": {"base": reps(10.0, 200.0), "head": reps(11.0, 100.0)}},
    }


def test_ledger_lines_are_computed_from_the_raw_samples() -> None:
    text = "\n".join(perf_ab_dispatch.ledger_lines(artifact()))
    assert "base `bbbbbbbbbbbb` head `hhhhhhhhhhhh` on Test CPU, 7 reps" in text
    assert re.search(r"\| row/8 \| 6[23]% \|", text)  # (200 - 100) / (200 - 40) of the gap
    assert "**PASS**" in text  # +10% CPU is within the 21% a 63% saving allows


def test_pool_concatenates_repetitions_of_the_same_host_and_commits() -> None:
    pooled = perf_ab_dispatch.pool([artifact(), artifact()])
    assert pooled["reps"] == 14
    assert len(pooled["samples"]["row/8"]["base"]) == 14


@pytest.mark.parametrize("key", ["cpu", "base", "head"])
def test_pool_refuses_a_different_host_or_commit(key: str) -> None:
    other = deepcopy(artifact())
    other[key] = "another CPU" if key == "cpu" else {"ref": "X", "sha": "x" * 40}
    with pytest.raises(SystemExit, match=key):
        perf_ab_dispatch.pool([artifact(), other])


def namespace(**kw: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "ref": "perf/x", "repo": "o/r", "base": "origin/main", "head_sha": "", "reps": 15,
        "max_reps": 25, "workloads": "", "head_env": "A=1 || A=2", "head_cppdefs": "",
        "holes_report": False, "null_arm": True, "no_sanity_gate": False,
    }  # fmt: skip
    return argparse.Namespace(**{**defaults, **kw})


def test_dispatch_passes_only_the_inputs_that_are_set() -> None:
    cmd = perf_ab_dispatch.dispatch_args(namespace())
    assert cmd[:5] == ["workflow", "run", "perf-ab.yml", "--ref", "perf/x"]
    pairs = dict(zip(cmd[5::2], cmd[6::2]))
    assert pairs["-f"] is not None
    inputs = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in cmd[6::2]}
    assert inputs["head_env"] == "A=1 || A=2" and inputs["null_arm"] == "true"
    assert inputs["max_reps"] == "25" and inputs["sanity_gate"] == "true"
    assert "workloads" not in inputs and "head_sha" not in inputs and "head_cppdefs" not in inputs


def test_run_and_fetch_waits_for_the_run_it_started(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    data = artifact()
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1))
    listings = [
        [{"databaseId": 1, "createdAt": stale}],
        [{"databaseId": 2, "createdAt": fresh}, {"databaseId": 1, "createdAt": stale}],
    ]

    def runner(cmd: Sequence[str]) -> str:
        calls.append(list(cmd))
        if cmd[:2] == ["run", "list"]:
            return json.dumps(listings.pop(0))
        if cmd[:2] == ["run", "download"]:
            (Path(cmd[cmd.index("-D") + 1]) / perf_ab_dispatch.SAMPLES_FILE).write_text(
                json.dumps(data)
            )
        return ""

    slept: list[float] = []
    got = perf_ab_dispatch.run_and_fetch(namespace(), runner, slept.append)
    assert got == data
    assert slept == [perf_ab_dispatch.FOUND_WAIT_S]  # the first listing only had the old run
    assert ["run", "watch", "2", "--exit-status"] in calls
    assert any(c[:3] == ["run", "download", "2"] for c in calls)


def test_a_failed_run_is_reported_not_swallowed() -> None:
    def runner(cmd: Sequence[str]) -> str:
        if cmd[:2] == ["run", "list"]:
            fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1))
            return json.dumps([{"databaseId": 9, "createdAt": fresh}])
        if cmd[:2] == ["run", "watch"]:
            raise subprocess.CalledProcessError(1, "gh")
        return ""

    with pytest.raises(SystemExit) as exit_info:
        perf_ab_dispatch.run_and_fetch(namespace(), runner, lambda _s: None)
    assert exit_info.value.code == 1


def test_old_artifact_cannot_get_a_synthetic_p99() -> None:
    data = artifact()
    data["fields"] = [f for f in data["fields"] if f != "p99 operation ns"]
    with pytest.raises(SystemExit, match="p99 must not be invented"):
        perf_ab_dispatch.ledger_lines(data)


@pytest.mark.parametrize("key", ["logical_cpus", "latency_protocol"])
def test_pool_refuses_different_contention_or_sampling(key: str) -> None:
    first, second = artifact(), artifact()
    if key == "logical_cpus":
        first[key], second[key] = 4, 8
    else:
        first[key], second[key] = {"version": "one"}, {"version": "two"}
    with pytest.raises(SystemExit, match=key):
        perf_ab_dispatch.pool([first, second])
