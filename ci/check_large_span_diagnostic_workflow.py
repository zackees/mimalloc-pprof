#!/usr/bin/env python3
"""Guard the manual-only, artifact-only GitHub-hosted #543 workflow."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, cast

import yaml

from check_benchmark_workflow import check_action_ref

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "benchmark-large-span-diagnostic.yml"
)


class WorkflowPolicyError(RuntimeError):
    """The diagnostic workflow has drifted from its opt-in safety contract."""


@dataclass(frozen=True)
class InputPolicy:
    name: str
    input_type: str
    required: bool
    has_default: bool


@dataclass(frozen=True)
class StepPolicy:
    name: str
    action: str
    run: str
    condition: str
    checkout_ref: str
    persist_credentials_false: bool
    artifact_path: str


@dataclass(frozen=True)
class JobPolicy:
    name: str
    runner: str
    permissions: tuple[tuple[str, str], ...]
    baseline_input_wired: bool
    candidate_input_wired: bool
    run_local_id_wired: bool
    steps: tuple[StepPolicy, ...]


@dataclass(frozen=True)
class WorkflowPolicy:
    trigger_names: tuple[str, ...]
    inputs: tuple[InputPolicy, ...]
    permissions: tuple[tuple[str, str], ...]
    jobs: tuple[JobPolicy, ...]


def fail(message: str) -> NoReturn:
    raise WorkflowPolicyError(message)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        fail(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        fail(f"{label} must have string keys")
    return cast(Mapping[str, object], value)


def _pairs(value: object, label: str) -> tuple[tuple[str, str], ...]:
    mapping = _mapping(value, label)
    pairs: list[tuple[str, str]] = []
    for key, item in cast(Mapping[object, object], mapping).items():
        if not isinstance(key, str) or not isinstance(item, str):
            fail(f"{label} values must be strings")
        pairs.append((key, item))
    return tuple(sorted(pairs))


def parse_policy(value: object) -> WorkflowPolicy:
    if not isinstance(value, Mapping):
        fail("workflow must be an object")
    raw_root_check = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) or key is True for key in raw_root_check):
        fail("workflow keys must be strings")
    root = cast(Mapping[str, object], value)
    raw_root = cast(Mapping[object, object], root)
    triggers_value = root.get("on", raw_root.get(True))
    triggers = _mapping(triggers_value, "workflow triggers")
    dispatch = _mapping(triggers.get("workflow_dispatch"), "workflow_dispatch")
    input_map = _mapping(dispatch.get("inputs"), "workflow inputs")
    input_policies: list[InputPolicy] = []
    for name, raw_spec in cast(Mapping[object, object], input_map).items():
        if not isinstance(name, str):
            fail("workflow input names must be strings")
        spec = _mapping(raw_spec, f"workflow input {name}")
        input_type = spec.get("type")
        if not isinstance(input_type, str):
            fail(f"workflow input {name} needs a string type")
        input_policies.append(
            InputPolicy(name, input_type, spec.get("required") is True, "default" in spec)
        )

    jobs_raw = _mapping(root.get("jobs"), "workflow jobs")
    jobs: list[JobPolicy] = []
    for name, raw_job in cast(Mapping[object, object], jobs_raw).items():
        if not isinstance(name, str):
            fail("job names must be strings")
        job = _mapping(raw_job, f"job {name}")
        env = _mapping(job.get("env"), f"job {name} environment")
        raw_steps = job.get("steps")
        if not isinstance(raw_steps, list):
            fail(f"job {name} steps must be a list")
        steps: list[StepPolicy] = []
        for raw_step in cast(list[object], raw_steps):
            step = _mapping(raw_step, f"job {name} step")
            step_name = step.get("name", "")
            action = step.get("uses", "")
            run = step.get("run", "")
            condition = step.get("if", "")
            if not all(isinstance(item, str) for item in (step_name, action, run, condition)):
                fail(f"job {name} step text fields must be strings")
            step_name_text = cast(str, step_name)
            action_text = cast(str, action)
            run_text = cast(str, run)
            condition_text = cast(str, condition)
            checkout_ref = ""
            credentials_false = False
            artifact_path = ""
            if action_text.startswith("actions/checkout@"):
                checkout_with = _mapping(step.get("with"), "checkout inputs")
                raw_ref = checkout_with.get("ref")
                checkout_ref = raw_ref if isinstance(raw_ref, str) else ""
                credentials_false = checkout_with.get("persist-credentials") is False
            if action_text.startswith("actions/upload-artifact@"):
                upload_with = _mapping(step.get("with"), "artifact upload inputs")
                raw_path = upload_with.get("path")
                artifact_path = raw_path if isinstance(raw_path, str) else ""
            steps.append(
                StepPolicy(
                    step_name_text,
                    action_text,
                    run_text,
                    condition_text,
                    checkout_ref,
                    credentials_false,
                    artifact_path,
                )
            )
        runner = job.get("runs-on")
        if not isinstance(runner, str):
            fail(f"job {name} needs one explicit runner")
        jobs.append(
            JobPolicy(
                name,
                runner,
                _pairs(job.get("permissions"), f"job {name} permissions"),
                env.get("BASELINE_SHA") == "${{ inputs.baseline_sha }}",
                env.get("CANDIDATE_SHA") == "${{ inputs.candidate_sha }}",
                "github.run_id" in str(env.get("RUN_LOCAL_ID"))
                and "github.run_attempt" in str(env.get("RUN_LOCAL_ID")),
                tuple(steps),
            )
        )
    return WorkflowPolicy(
        tuple(str(name) for name in cast(Mapping[object, object], triggers)),
        tuple(input_policies),
        _pairs(root.get("permissions"), "workflow permissions"),
        tuple(jobs),
    )


def validate(policy: WorkflowPolicy) -> None:
    if policy.trigger_names != ("workflow_dispatch",):
        fail("workflow must be manual-only")
    if tuple(sorted(item.name for item in policy.inputs)) != ("baseline_sha", "candidate_sha"):
        fail("workflow must require only explicit baseline and candidate SHA inputs")
    if any(
        item.input_type != "string" or not item.required or item.has_default
        for item in policy.inputs
    ):
        fail("baseline and candidate SHA inputs must be required strings without defaults")
    if policy.permissions != (("contents", "read"),):
        fail("workflow permissions must be contents: read only")
    if tuple(job.name for job in policy.jobs) != ("collect",):
        fail("workflow must use one serialized job and have no publish jobs")
    job = policy.jobs[0]
    if job.runner != "ubuntu-24.04":
        fail("all measurement arms must run on one ubuntu-24.04 hosted VM")
    if job.permissions != (("contents", "read"),):
        fail("job permissions must remain contents: read only")
    if not (job.baseline_input_wired and job.candidate_input_wired and job.run_local_id_wired):
        fail("source SHAs and run-local ID must come from explicit inputs/run identity")

    run_scripts = "\n".join(step.run for step in job.steps)
    names = {step.name for step in job.steps}
    required_fragments = (
        "git rev-parse HEAD",
        "ci/build_benchmark_allocators.py",
        "current-allocator-provenance.json",
        "allocator-lock.json",
        "ci/build_old_fork_latency_provenance.py",
        "ci/perf_access.py",
        '--output "$OUTPUT_DIR/perf-access.json"',
        '--github-env "$GITHUB_ENV"',
        "ci/large_span_diagnostic.py",
        "--reps 7",
        "--deep-output",
        "benchmark-latency-run",
        "--diagnostic-large-object",
        "--diagnostic-old-fork-provenance",
        "benchmark-scaling-run",
        "--blocks 7",
        "ci/large_span_latency_link.py",
        "latency-large-object-diagnostic.json",
        '--summary "$OUTPUT_DIR/large-span-combined-summary.txt"',
        "ci/large_span_refs.py",
    )
    for fragment in required_fragments:
        if fragment not in run_scripts:
            fail(f"workflow steps are missing required operation: {fragment}")
    if not {
        "run seven paired large-span repetitions and untimed deep diagnostics",
        "run matched latency sidecar",
        "run same-job diagnostic scaling references",
        "link same-run latency and contextual scaling artifacts",
    }.issubset(names):
        fail("workflow is missing a required measurement/link step")
    if "git push" in run_scripts or "gh workflow run" in run_scripts:
        fail("workflow must not publish or dispatch another workflow")
    if "sysctl -w" in run_scripts or "perf_event_paranoid" in run_scripts:
        fail("workflow must not modify the host-wide perf permission setting")
    checkout_steps = [step for step in job.steps if step.action.startswith("actions/checkout@")]
    if (
        len(checkout_steps) != 1
        or checkout_steps[0].checkout_ref != "${{ inputs.candidate_sha }}"
        or not checkout_steps[0].persist_credentials_false
    ):
        fail("workflow must check out the candidate SHA without persisted credentials")
    uploads = [step for step in job.steps if step.action.startswith("actions/upload-artifact@")]
    if len(uploads) != 1 or uploads[0].condition != "always()":
        fail("one unconditional raw-artifact upload must run even after measurement failure")
    required_artifacts = (
        "run-context.json",
        "current-allocator-provenance.json",
        "allocator-lock.json",
        "perf-access.json",
        "old-fork-build/allocator-provenance.json",
        "old-fork-build/old-libmimalloc.a",
        "old-fork-build/benchmark-child-old-fork",
        "large-span-*",
        "profile*",
        "latency/",
        "scaling/",
    )
    for artifact in required_artifacts:
        if artifact not in uploads[0].artifact_path:
            fail(f"raw upload is missing required evidence: {artifact}")
    for step in job.steps:
        if step.action:
            try:
                check_action_ref(step.action, "large-span diagnostic workflow action")
            except Exception as error:
                fail(str(error))


def load_policy() -> WorkflowPolicy:
    parsed: object = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return parse_policy(parsed)


def selftest() -> None:
    original = load_policy()
    validate(original)
    job = original.jobs[0]
    mutations = (
        replace(original, trigger_names=("workflow_dispatch", "schedule")),
        replace(original, permissions=(("contents", "write"),)),
        replace(original, jobs=(replace(job, runner="ubuntu-slim"),)),
        replace(original, jobs=(*original.jobs, replace(job, name="publish"))),
    )
    upload_index = next(
        index
        for index, step in enumerate(job.steps)
        if step.action.startswith("actions/upload-artifact@")
    )
    changed_steps = list(job.steps)
    changed_steps[upload_index] = replace(changed_steps[upload_index], condition="success()")
    mutations += (replace(original, jobs=(replace(job, steps=tuple(changed_steps)),)),)
    for mutation in mutations:
        try:
            validate(mutation)
        except WorkflowPolicyError:
            continue
        raise AssertionError("workflow policy checker accepted an unsafe mutation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
    validate(load_policy())
    print(f"PASS {WORKFLOW}: manual-only single-run diagnostic policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
