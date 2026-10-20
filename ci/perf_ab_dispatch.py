#!/usr/bin/env python3
"""Dispatch perf-ab, wait for it, and print the rule-12 ledger (#573 B4, B8).

    uv run ci/perf_ab_dispatch.py --ref perf/my-branch --base origin/main --reps 15 --null-arm
    uv run ci/perf_ab_dispatch.py --from-json samples.json [more.json ...]

The first form dispatches `.github/workflows/perf-ab.yml`, waits with `gh run watch`, downloads
the `perf-ab-samples` artifact and prints the ledger computed from its raw samples. The second
re-judges artifacts already downloaded. Several artifacts are pooled repetition by repetition,
but only when they come from the same CPU model and the same base and head commits: numbers from
different hosts are not comparable (#573: a knob screen once ran on four CPU models).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import perf_ab
import perf_ab_ledger

WORKFLOW = "perf-ab.yml"
ARTIFACT = "perf-ab-samples"
SAMPLES_FILE = "perf-ab-samples.json"
FOUND_ATTEMPTS = 12  # x FOUND_WAIT_S: how long a dispatched run may take to show up
FOUND_WAIT_S = 5

Runner = Callable[[Sequence[str]], str]


def gh(cmd: Sequence[str]) -> str:
    return subprocess.run(["gh", *cmd], check=True, capture_output=True, text=True).stdout


def pool(datas: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One artifact from several: same CPU, base and head commits, workloads and arms, whose
    repetitions are concatenated. Anything else is refused."""
    if not datas:
        raise SystemExit("nothing to pool")
    if any(data.get("complete") is False for data in datas):
        raise SystemExit("incomplete perf-ab samples are diagnostic only; refusing to pool")
    first = datas[0]
    for other in datas[1:]:
        for key in (
            "cpu",
            "base",
            "head",
            "logical_cpus",
            "latency_protocol",
            "arm_order_protocol",
        ):
            if other.get(key) != first.get(key):
                raise SystemExit(
                    f"refusing to pool runs with a different {key}: "
                    f"{first.get(key)!r} vs {other.get(key)!r} (numbers from different hosts or commits "
                    "are not comparable)"
                )
        if other["fields"] != first["fields"] or other["variants"] != first["variants"]:
            raise SystemExit("refusing to pool runs with different fields or variants")
    pooled: dict[str, Any] = json.loads(json.dumps(first))
    for other in datas[1:]:
        for workload, arms in other["samples"].items():
            if workload not in pooled["samples"]:
                raise SystemExit(f"refusing to pool: {workload!r} is missing from the first run")
            for arm, reps in arms.items():
                pooled["samples"][workload][arm] += reps
    pooled["reps"] = sum(d["reps"] for d in datas)
    return pooled


def ledger_lines(data: dict[str, Any]) -> list[str]:
    """The ledger table of every head arm in an artifact, computed from its raw samples."""
    if data.get("complete") is False:
        raise SystemExit("incomplete perf-ab samples are diagnostic only; no regression verdict")
    if data["fields"] != [*perf_ab.METRICS, "ideal RSS MiB"]:
        raise SystemExit("artifact fields do not match this harness; p99 must not be invented")
    lines: list[str] = []
    for variant in data["variants"]:
        rows: dict[str, tuple[float, list[perf_ab_ledger.Cell]]] = {}
        for workload, arms in data["samples"].items():
            null = arms.get("null")
            rows[workload] = perf_ab.ledger_row(arms["base"], arms[variant["arm"]], null)
        if len(data["variants"]) > 1:
            lines += [
                "",
                f"### {variant['arm']}: env {variant['env']} defines {variant['cppdefs']}",
            ]
        lines += perf_ab_ledger.table(rows)
    header = (
        f"base `{data['base']['sha'][:12]}` head `{data['head']['sha'][:12]}` on {data['cpu']}, "
        f"{data['reps']} reps"
        + (f" (up to {data['max_reps']})" if data.get("max_reps", 0) > data["reps"] else "")
    )
    if data.get("logical_cpus"):
        header += (
            f"; {data['logical_cpus']} logical CPUs, "
            f"8-worker cells have {8 / data['logical_cpus']:.2f} workers per logical CPU "
            "(above 1 means oversubscription)"
        )
    order = data.get("arm_order_protocol")
    header += f"; arm order: {order['version'] if order else 'legacy alternating endpoints'}"
    return [header, *lines]


def dispatch_args(args: argparse.Namespace) -> list[str]:
    inputs = {
        "base": args.base,
        "head_sha": args.head_sha,
        "reps": str(args.reps),
        "workloads": args.workloads,
        "head_env": args.head_env,
        "head_cppdefs": args.head_cppdefs,
        "holes_report": str(args.holes_report).lower(),
        "null_arm": str(args.null_arm).lower(),
        "max_reps": str(args.max_reps),
        "sanity_gate": str(not args.no_sanity_gate).lower(),
    }
    cmd = ["workflow", "run", WORKFLOW, "--ref", args.ref]
    for key, value in inputs.items():
        if value != "":
            cmd += ["-f", f"{key}={value}"]
    return cmd


def find_run(runner: Runner, ref: str, since: float, sleep: Callable[[float], None]) -> int:
    """The database id of the run this dispatch started: the newest dispatch on `ref` created
    at or after `since` (epoch seconds)."""
    for _ in range(FOUND_ATTEMPTS):
        listing = json.loads(
            runner(
                [
                    "run",
                    "list",
                    "--workflow",
                    WORKFLOW,
                    "--event",
                    "workflow_dispatch",
                    "--branch",
                    ref,
                    "--limit",
                    "5",
                    "--json",
                    "databaseId,createdAt",
                ]
            )
        )
        for run in listing:
            created = (
                datetime.strptime(run["createdAt"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            if created >= since - 2:
                return int(run["databaseId"])
        sleep(FOUND_WAIT_S)
    raise SystemExit(f"the dispatched {WORKFLOW} run on {ref} did not appear")


def run_and_fetch(
    args: argparse.Namespace, runner: Runner = gh, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    since = time.time()
    runner(dispatch_args(args))
    run_id = find_run(runner, args.ref, since, sleep)
    print(f"perf-ab run {run_id}: https://github.com/{args.repo}/actions/runs/{run_id}", flush=True)
    try:
        runner(["run", "watch", str(run_id), "--exit-status"])
    except subprocess.CalledProcessError:
        print(f"the run failed; see the sanity gate or job log: gh run view {run_id} --log-failed")
        raise SystemExit(1) from None
    with tempfile.TemporaryDirectory() as tmp:
        runner(["run", "download", str(run_id), "-n", ARTIFACT, "-D", tmp])
        return json.loads((Path(tmp) / SAMPLES_FILE).read_text())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from-json", nargs="+", type=Path, help="judge these artifacts instead of dispatching"
    )
    parser.add_argument(
        "--ref", default="", help="branch to dispatch on (default: the current one)"
    )
    parser.add_argument("--repo", default="zackees/mimalloc-pprof")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--reps", type=int, default=15)
    parser.add_argument("--max-reps", type=int, default=0)
    parser.add_argument(
        "--workloads", default="", help="name substrings separated by '|'; '=name' matches exactly"
    )
    parser.add_argument("--head-env", default="")
    parser.add_argument("--head-cppdefs", default="")
    parser.add_argument("--holes-report", action="store_true")
    parser.add_argument("--null-arm", action="store_true")
    parser.add_argument("--no-sanity-gate", action="store_true")
    parser.add_argument("--save", type=Path, help="keep the downloaded artifact here")
    args = parser.parse_args()
    if args.from_json:
        data = pool([json.loads(p.read_text()) for p in args.from_json])
    else:
        if not args.ref:
            args.ref = subprocess.run(
                ["git", "branch", "--show-current"], check=True, capture_output=True, text=True
            ).stdout.strip()
        data = run_and_fetch(args)
        if args.save:
            args.save.write_text(json.dumps(data, indent=1) + "\n")
    print("\n".join(ledger_lines(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
