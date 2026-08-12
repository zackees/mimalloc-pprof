#!/usr/bin/env python3
"""Fail-closed policy checker for the Linux transaction-latency workflow."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn, cast

import yaml

from check_benchmark_workflow import check_action_ref

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "benchmark-latency.yml"
JOBS = {
    "build-and-measure",
    "artifact-audit",
    "publish-branch",
    "package-pages",
    "deploy-pages",
    "publication-audit",
}


class LatencyWorkflowError(RuntimeError):
    """A transaction-latency workflow policy assertion failed."""


def fail(message: str) -> NoReturn:
    raise LatencyWorkflowError(message)


def mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{label}: expected object")
    return cast(dict[str, object], value)


def steps_by_name(job: Mapping[str, object]) -> dict[str, dict[str, object]]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        fail("job.steps: expected array")
    result: dict[str, dict[str, object]] = {}
    for value in cast(list[object], steps):
        step = mapping(value, "job step")
        name = step.get("name")
        if isinstance(name, str):
            result[name] = step
        action = step.get("uses")
        if isinstance(action, str):
            try:
                check_action_ref(action, "latency workflow action")
            except Exception as error:
                fail(str(error))
    return result


def validate(workflow: Mapping[str, object]) -> None:
    raw_workflow = cast(Mapping[object, object], workflow)
    on = workflow.get("on", raw_workflow.get(True))  # PyYAML 1.1 treats `on` as true.
    triggers = mapping(on, "workflow.on")
    if set(triggers) != {"workflow_dispatch", "schedule"}:
        fail("workflow.on: only weekly schedule and manual dispatch are allowed")
    schedule = triggers.get("schedule")
    if not isinstance(schedule, list) or len(cast(list[object], schedule)) != 1:
        fail("workflow.on.schedule: expected one weekly schedule")
    dispatch = mapping(triggers.get("workflow_dispatch"), "workflow.on.workflow_dispatch")
    inputs = mapping(dispatch.get("inputs"), "workflow.on.workflow_dispatch.inputs")
    if set(inputs) != {"mode", "run_seed", "blocks"}:
        fail("workflow dispatch inputs must be exactly mode/run_seed/blocks")
    mode = mapping(inputs["mode"], "workflow input mode")
    if mode.get("options") != ["full", "smoke"] or mode.get("default") != "full":
        fail("workflow input mode must default to full with full/smoke choices")

    concurrency = mapping(workflow.get("concurrency"), "workflow.concurrency")
    if concurrency != {"group": "benchmark-stats-production", "cancel-in-progress": False}:
        fail("latency workflow must serialize with the production publication group")
    if mapping(workflow.get("permissions"), "workflow.permissions") != {"contents": "read"}:
        fail("workflow permissions must be contents: read")

    jobs = mapping(workflow.get("jobs"), "workflow.jobs")
    if set(jobs) != JOBS:
        fail(f"workflow jobs mismatch: expected {sorted(JOBS)}")
    for name, value in jobs.items():
        job = mapping(value, f"workflow.jobs.{name}")
        if job.get("runs-on") != "ubuntu-24.04":
            fail(f"workflow.jobs.{name}.runs-on: expected ubuntu-24.04")
        timeout = job.get("timeout-minutes")
        if not isinstance(timeout, int) or timeout > 60:
            fail(f"workflow.jobs.{name}.timeout-minutes: expected <=60")
        if "strategy" in job:
            fail(f"workflow.jobs.{name}: parallel matrices are forbidden")
        steps_by_name(job)

    build = mapping(jobs["build-and-measure"], "build-and-measure")
    if build.get("timeout-minutes") != 60:
        fail("build-and-measure must enforce the 60-minute hard limit")
    steps = steps_by_name(build)
    run_step = mapping(steps.get("run transaction latency suite"), "run transaction latency suite")
    run = run_step.get("run")
    if not isinstance(run, str) or "benchmark-latency-run" not in run or "--blocks" not in run:
        fail("latency measurement step must execute benchmark-latency-run with explicit blocks")
    if " &" in run or "parallel" in run or "xargs" in run:
        fail("latency allocators must execute sequentially")
    for step_name in (
        "determine run seed",
        "run transaction latency suite",
        "compute publication eligibility",
    ):
        step = mapping(steps.get(step_name), step_name)
        if "${{ inputs." in str(step.get("run", "")):
            fail(f"{step_name}: workflow inputs must enter shell through env, not source text")
    seed_step = mapping(steps.get("determine run seed"), "determine run seed")
    seed_env = mapping(seed_step.get("env"), "determine run seed.env")
    if "INPUT_RUN_SEED" not in seed_env or "*[!0-9]*" not in str(seed_step.get("run", "")):
        fail("run seed must use an env boundary and strict decimal validation")
    raw = mapping(steps.get("upload raw latency artifact"), "upload raw latency artifact")
    if raw.get("if") != "always()":
        fail("raw latency artifact must upload with if: always()")
    raw_with = mapping(raw.get("with"), "upload raw latency artifact.with")
    if raw_with.get("retention-days") != 30 or raw_with.get("include-hidden-files") is not True:
        fail("raw latency/control artifact must retain all bytes for 30 days")
    eligibility = mapping(steps.get("compute publication eligibility"), "eligibility")
    eligibility_run = eligibility.get("run")
    if not isinstance(eligibility_run, str):
        fail("eligibility step needs a shell policy")
    for required in ("refs/heads/main", "full", "-ge 15"):
        if required not in eligibility_run:
            fail(f"eligibility step is missing {required!r}")

    publish = mapping(jobs["publish-branch"], "publish-branch")
    if mapping(publish.get("permissions"), "publish permissions") != {"contents": "write"}:
        fail("only publish-branch may use contents: write")
    deploy = mapping(jobs["deploy-pages"], "deploy-pages")
    if mapping(deploy.get("permissions"), "deploy permissions") != {
        "pages": "write",
        "id-token": "write",
    }:
        fail("deploy-pages requires only pages/id-token write")
    if mapping(deploy.get("environment"), "deploy environment").get("name") != "github-pages":
        fail("deploy-pages must target the github-pages environment")
    publish_text = str(publish)
    if "--force-with-lease" not in publish_text or "prepare-branch" not in publish_text:
        fail("branch publication must use exact replacement and a lease")
    package_text = str(jobs["package-pages"])
    if (
        "benchmark-latency-site-" not in publish_text
        or "benchmark-latency-site-" not in package_text
    ):
        fail("branch and Pages must consume the same sealed latency site artifact")
    audit_text = str(jobs["publication-audit"])
    for required in ("validate-revision", "audit-pages"):
        if required not in audit_text:
            fail(f"publication audit is missing {required}")


def load(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return mapping(value, str(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    validate(load(args.workflow))
    if args.selftest:
        print("PASS benchmark latency workflow policy selftest")
    else:
        print(f"PASS benchmark latency workflow policy: {args.workflow}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LatencyWorkflowError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
