#!/usr/bin/env python3
"""Fail-closed policy checker for the Linux process-memory workflow.

`--selftest` is a real test: it mutates a copy of the on-disk workflow once
per rule and requires every mutation to be rejected. A checker that only ever
sees a passing input cannot prove it checks anything.
"""

from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml

from check_benchmark_workflow import check_action_ref

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "benchmark-memory.yml"
JOBS = {
    "build-and-measure",
    "artifact-audit",
    "publish-branch",
    "package-pages",
    "deploy-pages",
    "publication-audit",
}


class MemoryWorkflowError(RuntimeError):
    """A process-memory workflow policy assertion failed."""


def fail(message: str) -> NoReturn:
    raise MemoryWorkflowError(message)


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
                check_action_ref(action, "memory workflow action")
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
        fail("memory workflow must serialize with the production publication group")
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
    run_step = mapping(steps.get("run external memory suite"), "run external memory suite")
    run = run_step.get("run")
    if not isinstance(run, str) or "benchmark-memory-run" not in run or "--blocks" not in run:
        fail("memory measurement step must execute benchmark-memory-run with explicit blocks")
    if " &" in run or "parallel" in run or "xargs" in run:
        fail("memory allocators must execute sequentially")
    for step_name in (
        "determine run seed",
        "run external memory suite",
        "compute publication eligibility",
    ):
        step = mapping(steps.get(step_name), step_name)
        if "${{ inputs." in str(step.get("run", "")):
            fail(f"{step_name}: workflow inputs must enter shell through env, not source text")
    seed_step = mapping(steps.get("determine run seed"), "determine run seed")
    seed_env = mapping(seed_step.get("env"), "determine run seed.env")
    if "INPUT_RUN_SEED" not in seed_env or "*[!0-9]*" not in str(seed_step.get("run", "")):
        fail("run seed must use an env boundary and strict decimal validation")
    raw = mapping(steps.get("upload raw proc artifact"), "upload raw proc artifact")
    if raw.get("if") != "always()":
        fail("raw proc artifact must upload with if: always()")
    raw_with = mapping(raw.get("with"), "upload raw proc artifact.with")
    if raw_with.get("retention-days") != 30 or raw_with.get("include-hidden-files") is not True:
        fail("raw proc artifact must retain all bytes for 30 days")
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
    if "benchmark-memory-site-" not in publish_text or "benchmark-memory-site-" not in package_text:
        fail("branch and Pages must consume the same sealed memory site artifact")
    audit_text = str(jobs["publication-audit"])
    for required in ("validate-revision", "audit-pages"):
        if required not in audit_text:
            fail(f"publication audit is missing {required}")


def load(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return mapping(value, str(path))


def _build_job(workflow: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], cast(dict[str, Any], workflow["jobs"])["build-and-measure"])


def _step(workflow: dict[str, Any], name: str, job: str | None = None) -> dict[str, Any]:
    """A step by name, from `build-and-measure` unless another job is named."""
    steps = (
        cast(list[dict[str, Any]], cast(dict[str, Any], workflow["jobs"])[job]["steps"])
        if job is not None
        else cast(list[dict[str, Any]], _build_job(workflow)["steps"])
    )
    for step in steps:
        if step.get("name") == name:
            return step
    raise KeyError(name)


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 parses the bare `on:` key as the boolean True.
    raw = cast(dict[Any, Any], workflow)
    return cast(dict[str, Any], raw["on"] if "on" in raw else raw[True])


def _rename_pages_site_artifact(workflow: dict[str, Any]) -> None:
    """Point `package-pages`'s download step at a different artifact name."""
    steps = cast(
        list[dict[str, Any]], cast(dict[str, Any], workflow["jobs"])["package-pages"]["steps"]
    )
    for step in steps:
        with_block = step.get("with")
        if not isinstance(with_block, dict):
            continue
        with_mapping = cast(dict[str, Any], with_block)
        if "benchmark-memory-site-" in str(with_mapping.get("name", "")):
            with_mapping["name"] = "other-site"
            return
    raise KeyError("package-pages download step")


MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "push trigger added": lambda wf: _triggers(wf).__setitem__("push", {"branches": ["main"]}),
    "second schedule entry added": lambda wf: _triggers(wf).__setitem__(
        "schedule", [{"cron": "41 8 * * 0"}, {"cron": "0 0 * * 1"}]
    ),
    "extra dispatch input added": lambda wf: cast(
        dict[str, Any],
        cast(dict[str, Any], _triggers(wf)["workflow_dispatch"])["inputs"],
    ).__setitem__("allocator", {"type": "string"}),
    "mode default changed to smoke": lambda wf: cast(
        dict[str, Any],
        cast(dict[str, Any], _triggers(wf)["workflow_dispatch"])["inputs"]["mode"],
    ).__setitem__("default", "smoke"),
    "shared concurrency group dropped": lambda wf: wf.__setitem__(
        "concurrency", {"group": "memory-only", "cancel-in-progress": False}
    ),
    "cancel-in-progress enabled": lambda wf: cast(dict[str, Any], wf["concurrency"]).__setitem__(
        "cancel-in-progress", True
    ),
    "workflow write permission": lambda wf: wf.__setitem__("permissions", {"contents": "write"}),
    "runs-on changed on build job": lambda wf: _build_job(wf).__setitem__(
        "runs-on", "ubuntu-latest"
    ),
    "budget exceeded": lambda wf: _build_job(wf).__setitem__("timeout-minutes", 61),
    "build timeout below the required 60": lambda wf: _build_job(wf).__setitem__(
        "timeout-minutes", 45
    ),
    "matrix introduced": lambda wf: _build_job(wf).__setitem__(
        "strategy", {"matrix": {"allocator": ["a", "b"]}}
    ),
    "unpinned action": lambda wf: cast(list[dict[str, Any]], _build_job(wf)["steps"])[
        0
    ].__setitem__("uses", "actions/checkout@v4"),
    "allocators run in parallel": lambda wf: _step(wf, "run external memory suite").__setitem__(
        "run", "benchmark-memory-run --blocks 15 &"
    ),
    "measurement step without explicit blocks": lambda wf: _step(
        wf, "run external memory suite"
    ).__setitem__("run", "benchmark-memory-run"),
    "input interpolated into shell": lambda wf: _step(wf, "determine run seed").__setitem__(
        "run", "SEED=${{ inputs.run_seed }}"
    ),
    "seed validation removed": lambda wf: _step(wf, "determine run seed").__setitem__(
        "run", "echo seed=1 >> $GITHUB_OUTPUT"
    ),
    "raw artifact conditional": lambda wf: _step(wf, "upload raw proc artifact").__setitem__(
        "if", "success()"
    ),
    "retention shortened": lambda wf: cast(
        dict[str, Any], _step(wf, "upload raw proc artifact")["with"]
    ).__setitem__("retention-days", 1),
    "hidden files dropped": lambda wf: cast(
        dict[str, Any], _step(wf, "upload raw proc artifact")["with"]
    ).__setitem__("include-hidden-files", False),
    "eligibility accepts any ref": lambda wf: _step(
        wf, "compute publication eligibility"
    ).__setitem__("run", "echo publish_eligible=true >> $GITHUB_OUTPUT"),
    "publish job over-permissioned": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "publish-branch"
    ].__setitem__("permissions", {"contents": "write", "packages": "write"}),
    "deploy environment renamed": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "deploy-pages"
    ].__setitem__("environment", {"name": "staging"}),
    "deploy over-permissioned": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "deploy-pages"
    ].__setitem__("permissions", {"pages": "write", "id-token": "write", "contents": "write"}),
    "lease dropped": lambda wf: _step(
        wf, "publish exact memory site revision", "publish-branch"
    ).__setitem__("run", "git push origin HEAD:$PUBLISH_REF"),
    "Pages consumes a different artifact": _rename_pages_site_artifact,
    "publication audit weakened": lambda wf: _step(
        wf, "audit memory publication", "publication-audit"
    ).__setitem__("run", "echo ok"),
}


def selftest(path: Path) -> None:
    """Every declared rule must reject at least one concrete mutation."""

    baseline = load(path)
    validate(baseline)
    for label, mutate in MUTATIONS.items():
        candidate = copy.deepcopy(baseline)
        mutate(cast(dict[str, Any], candidate))
        try:
            validate(candidate)
        except MemoryWorkflowError:
            continue
        fail(f"selftest: the checker accepted a workflow with {label}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        selftest(args.workflow)
        print(f"PASS benchmark memory workflow policy selftest ({len(MUTATIONS)} controls)")
    else:
        validate(load(args.workflow))
        print(f"PASS benchmark memory workflow policy: {args.workflow}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MemoryWorkflowError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
