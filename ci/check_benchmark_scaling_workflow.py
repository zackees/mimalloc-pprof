#!/usr/bin/env python3
"""Fail-closed policy checker for the Linux sparse thread-scaling workflow.

`--selftest` is a real test: it mutates a copy of the on-disk workflow once per
rule and requires every mutation to be rejected. A checker that only ever sees a passing input cannot prove it checks
anything.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, NoReturn, cast

import yaml

from benchmark_report import (
    DISTRIBUTION_BLOCKS,
    SCALING_BLOCKS,
    SCALING_PATTERN_IDS,
    SCALING_RSS_SCHEMA,
    SCALING_SCHEMA,
    SCALING_THREAD_POINTS,
    THREAD_CHURN_OFFSETS_MS,
    THREAD_CHURN_RELEASE_TOLERANCE_BYTES,
    THREAD_CHURN_SCHEMA,
    THREAD_CHURN_THREADS,
)
from check_benchmark_workflow import check_action_ref

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "benchmark-scaling.yml"
# The sweep contract is declared twice: the Rust producer emits it and the
# Python validator rejects anything that disagrees. Drift is silent until a
# scheduled run has already spent its budget and fails at overlay time, so the
# two declarations are compared here, in a job that runs on every ci/ PR.
SCALING_SOURCE = (
    Path(__file__).resolve().parents[1] / "rust" / "benchmark-suite" / "src" / "scaling.rs"
)
JOBS = {
    "build",
    "measure",
    "assemble",
    "artifact-audit",
    "publish-branch",
    "package-pages",
    "deploy-pages",
    "publication-audit",
}
# Coverage mode exists to stay cheap; the budget is part of the contract.
MAXIMUM_BUILD_TIMEOUT_MINUTES = 30
MEASURE_TIMEOUT_MINUTES = 120  # #424: approved single-host measurement envelope.
EXPECTED_BLOCKS = 3
# #528: the diagnostic dispatch inputs, and the steps that must stay behind `mode: full`.
DIAGNOSTIC_INPUTS = (
    "diagnostic_env",
    "diagnostic_cppdefs",
    "diagnostic_patterns",
    "diagnostic_threads",
)
INPUTS = {"mode", "run_seed", "blocks", *DIAGNOSTIC_INPUTS}
MODES = ["full", "smoke", "diagnostic"]
FULL_ONLY = "(inputs.mode || 'full') == 'full'"
FULL_ONLY_STEPS = (
    "validate and overlay complete scaling report",
    "render sealed site",
    "upload validation artifact",
    "upload site artifact",
)
PUBLISH_ELIGIBLE = "needs.assemble.outputs.publish_eligible == 'true'"
PUBLISH_JOBS = ("publish-branch", "package-pages", "deploy-pages", "publication-audit")


class ScalingWorkflowError(RuntimeError):
    """A sparse thread-scaling workflow policy assertion failed."""


def fail(message: str) -> NoReturn:
    raise ScalingWorkflowError(message)


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
                check_action_ref(action, "scaling workflow action")
            except Exception as error:
                fail(str(error))
    return result


def validate(workflow: Mapping[str, object]) -> None:
    raw_workflow = cast(Mapping[object, object], workflow)
    on = workflow.get("on", raw_workflow.get(True))  # PyYAML 1.1 treats `on` as true.
    triggers = mapping(on, "workflow.on")
    if set(triggers) != {"workflow_dispatch", "schedule"}:
        fail("workflow.on: only a schedule and manual dispatch are allowed")
    schedule = triggers.get("schedule")
    if not isinstance(schedule, list) or len(cast(list[object], schedule)) != 1:
        fail("workflow.on.schedule: expected exactly one schedule entry")
    if schedule != [{"cron": "23 7 * * *"}]:
        fail("workflow.on.schedule: expected daily cron '23 7 * * *' (#208)")
    dispatch = mapping(triggers.get("workflow_dispatch"), "workflow.on.workflow_dispatch")
    inputs = mapping(dispatch.get("inputs"), "workflow.on.workflow_dispatch.inputs")
    if set(inputs) != INPUTS:
        fail(f"workflow dispatch inputs must be exactly {sorted(INPUTS)}")
    mode = mapping(inputs["mode"], "workflow input mode")
    if mode.get("options") != MODES or mode.get("default") != "full":
        fail(f"workflow input mode must default to full with {MODES} choices")
    for name in DIAGNOSTIC_INPUTS:
        value = mapping(inputs[name], f"workflow input {name}")
        if value.get("type") != "string" or value.get("default") != "":
            fail(f"workflow input {name} must be a string defaulting to empty")
    blocks_input = mapping(inputs["blocks"], "workflow input blocks")
    if blocks_input.get("default") != EXPECTED_BLOCKS:
        fail(f"workflow input blocks must default to {EXPECTED_BLOCKS}")

    concurrency = mapping(workflow.get("concurrency"), "workflow.concurrency")
    if concurrency != {"group": "benchmark-stats-production", "cancel-in-progress": False}:
        fail("scaling workflow must serialize with the production publication group")
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
        limit = MEASURE_TIMEOUT_MINUTES if name == "measure" else MAXIMUM_BUILD_TIMEOUT_MINUTES
        if not isinstance(timeout, int) or timeout > limit:
            fail(f"workflow.jobs.{name}.timeout-minutes: expected <={limit}")
        if "strategy" in job:
            fail(f"workflow.jobs.{name}: parallel matrices are forbidden; measure on one host")
        steps_by_name(job)

    build = mapping(jobs["build"], "build")
    if build.get("timeout-minutes") != MAXIMUM_BUILD_TIMEOUT_MINUTES:
        fail(f"build must enforce the {MAXIMUM_BUILD_TIMEOUT_MINUTES}-minute limit")
    measure = mapping(jobs["measure"], "measure")
    if measure.get("timeout-minutes") != MEASURE_TIMEOUT_MINUTES:
        fail(f"measure must enforce the {MEASURE_TIMEOUT_MINUTES}-minute limit")
    build_steps = steps_by_name(build)
    measure_steps = steps_by_name(measure)
    assemble = mapping(jobs["assemble"], "assemble")
    assemble_steps = steps_by_name(assemble)
    run_step = mapping(measure_steps.get("run sparse scaling sweep"), "run sparse scaling sweep")
    run = run_step.get("run")
    if not isinstance(run, str) or "benchmark-scaling-run" not in run or "--blocks" not in run:
        fail("scaling measurement step must execute benchmark-scaling-run with explicit blocks")
    for required in ("for SHARD in 0 1 2 3 4 5", '--shard-index "$SHARD"', "--shard-count 6"):
        if required not in run:
            fail(f"scaling measurement step is missing {required}")
    if "cargo run" in run or "soldr" in run:
        fail("scaling measurement must execute the prebuilt binary directly")
    if " &" in run or "parallel" in run or "xargs" in run:
        fail("scaling allocators must execute sequentially")
    # Every step, not a named few: the #528 diagnostic inputs are free text.
    for job_name, value in jobs.items():
        for step_name, step in steps_by_name(mapping(value, job_name)).items():
            if "${{ inputs." in str(step.get("run", "")):
                fail(f"{step_name}: workflow inputs must enter shell through env, not source text")
    validate_diagnostic_mode(build, build_steps, measure_steps, assemble_steps)
    seed_step = mapping(build_steps.get("determine run seed"), "determine run seed")
    seed_env = mapping(seed_step.get("env"), "determine run seed.env")
    if "INPUT_RUN_SEED" not in seed_env or "*[!0-9]*" not in str(seed_step.get("run", "")):
        fail("run seed must use an env boundary and strict decimal validation")
    setup_soldr = next(
        (
            step
            for step in cast(list[object], build["steps"])
            if "setup-soldr@" in str(mapping(step, "build step").get("uses", ""))
        ),
        None,
    )
    if setup_soldr is None:
        fail("build must configure setup-soldr")
    soldr_with = mapping(mapping(setup_soldr, "setup-soldr").get("with"), "setup-soldr.with")
    for key, expected in {
        "cache-preset": "full",
        "toolchain-file": "rust/rust-toolchain.toml",
        "lockfile": "rust/Cargo.lock",
        "target-dir": "rust/target",
    }.items():
        if soldr_with.get(key) != expected:
            fail(f"setup-soldr must set {key} to {expected}")
    merge_run = mapping(assemble_steps.get("merge scaling shards"), "merge scaling shards").get(
        "run"
    )
    if (
        not isinstance(merge_run, str)
        or "benchmark-scaling-merge" not in merge_run
        or "--report-out" not in merge_run
    ):
        fail("assemble must merge scaling shards into one raw run")
    raw = mapping(measure_steps.get("upload raw scaling shard"), "upload raw scaling shard")
    if raw.get("if") != "always()":
        fail("raw scaling artifact must upload with if: always()")
    raw_with = mapping(raw.get("with"), "upload raw scaling artifact.with")
    if raw_with.get("retention-days") != 30 or raw_with.get("include-hidden-files") is not True:
        fail("raw scaling artifact must retain all bytes for 30 days")
    raw_name = raw_with.get("name")
    merged_raw = mapping(assemble_steps.get("upload merged raw scaling artifact"), "merged raw")
    merged_name = mapping(merged_raw.get("with"), "merged raw.with").get("name")
    if not isinstance(raw_name, str) or "benchmark-scaling-shards-" not in raw_name:
        fail("measurement shards require a dedicated raw artifact")
    if raw_name == merged_name:
        fail("measurement shards and merged raw report must use distinct artifacts")
    eligibility = mapping(assemble_steps.get("compute publication eligibility"), "eligibility")
    eligibility_run = eligibility.get("run")
    if not isinstance(eligibility_run, str):
        fail("eligibility step needs a shell policy")
    for required in ("refs/heads/main", '[ "$SCALING_MODE" = "full" ]', f"-eq {EXPECTED_BLOCKS}"):
        if required not in eligibility_run:
            fail(f"eligibility step is missing {required!r}")
    # #528: validation, rendering and the site artifact are `mode: full` only, so a
    # diagnostic (or smoke) run can produce nothing the publication jobs could consume.
    for step_name in FULL_ONLY_STEPS:
        condition = mapping(assemble_steps.get(step_name), step_name).get("if")
        if not isinstance(condition, str) or not condition.startswith(FULL_ONLY):
            fail(f"{step_name} must run only in mode: full ({FULL_ONLY})")
    audit_job = mapping(jobs["artifact-audit"], "artifact-audit")
    if audit_job.get("if") != "needs.assemble.outputs.mode == 'full'":
        fail("artifact-audit must run only in mode: full")
    for job_name in PUBLISH_JOBS:
        condition = mapping(jobs[job_name], job_name).get("if")
        if not isinstance(condition, str) or not condition.startswith(PUBLISH_ELIGIBLE):
            fail(f"{job_name} must be gated on {PUBLISH_ELIGIBLE}")

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
        "benchmark-scaling-site-" not in publish_text
        or "benchmark-scaling-site-" not in package_text
    ):
        fail("branch and Pages must consume the same sealed scaling site artifact")
    audit_text = str(jobs["publication-audit"])
    for required in ("validate-revision", "audit-pages"):
        if required not in audit_text:
            fail(f"publication audit is missing {required}")


def validate_diagnostic_mode(
    build: Mapping[str, object],
    build_steps: Mapping[str, dict[str, object]],
    measure_steps: Mapping[str, dict[str, object]],
    assemble_steps: Mapping[str, dict[str, object]],
) -> None:
    """#528: the diagnostic inputs are validated before any build, reach only the
    mimalloc-pprof build and child, and never share the allocator cache."""

    check = mapping(build_steps.get("validate dispatch inputs"), "validate dispatch inputs")
    check_env = mapping(check.get("env"), "validate dispatch inputs.env")
    check_run = str(check.get("run", ""))
    if "ci/scaling_diagnostic.py validate" not in check_run:
        fail("validate dispatch inputs must run ci/scaling_diagnostic.py validate")
    for name in DIAGNOSTIC_INPUTS:
        if f"${{{{ inputs.{name} }}}}" not in check_env.values():
            fail(f"validate dispatch inputs must receive {name} through env")
    names = [
        str(mapping(step, "build step").get("name", ""))
        for step in cast(list[object], build["steps"])
    ]
    if names.index("validate dispatch inputs") > names.index("build native allocator libraries"):
        fail("dispatch inputs must be validated before the allocators are built")

    native = mapping(
        build_steps.get("build native allocator libraries"), "build native allocator libraries"
    )
    native_env = mapping(native.get("env"), "build native allocator libraries.env")
    if native_env.get("DIAGNOSTIC_CPPDEFS") != "${{ inputs.diagnostic_cppdefs }}" or (
        '--fork-cppdefs "$DIAGNOSTIC_CPPDEFS"' not in str(native.get("run", ""))
    ):
        fail("diagnostic_cppdefs must reach the builder as --fork-cppdefs through env")
    cache = next(
        (
            step
            for step in cast(list[object], build["steps"])
            if "actions/cache@" in str(mapping(step, "build step").get("uses", ""))
        ),
        None,
    )
    cache_with = mapping(mapping(cache, "allocator cache").get("with"), "allocator cache.with")
    if "${{ inputs.diagnostic_cppdefs }}" not in str(cache_with.get("key", "")):
        fail("the allocator cache key must include diagnostic_cppdefs")

    sweep = mapping(measure_steps.get("run sparse scaling sweep"), "run sparse scaling sweep")
    sweep_env = mapping(sweep.get("env"), "run sparse scaling sweep.env")
    sweep_run = str(sweep.get("run", ""))
    for variable, name, flag in (
        ("DIAGNOSTIC_ENV", "diagnostic_env", "--diagnostic-env"),
        ("DIAGNOSTIC_PATTERNS", "diagnostic_patterns", "--patterns"),
        ("DIAGNOSTIC_THREADS", "diagnostic_threads", "--thread-points"),
    ):
        if sweep_env.get(variable) != f"${{{{ inputs.{name} }}}}" or (
            f'{flag} "${variable}"' not in sweep_run
        ):
            fail(f"{name} must reach benchmark-scaling-run as {flag} through env")
    if "--diagnostic " not in sweep_run or '[ "$MODE" = "diagnostic" ]' not in sweep_run:
        fail("the sweep must pass --diagnostic exactly when mode is diagnostic")

    summary = mapping(assemble_steps.get("summarize diagnostic run"), "summarize diagnostic run")
    if summary.get("if") != "(inputs.mode || 'full') == 'diagnostic'" or (
        "ci/scaling_diagnostic.py summarize" not in str(summary.get("run", ""))
    ):
        fail("a diagnostic run must be summarized, and only a diagnostic run")


RUST_THREAD_POINTS = re.compile(
    r"pub const SCALING_THREAD_POINTS:\s*\[u32;\s*(?P<length>\d+)\]\s*=\s*\[(?P<points>[^\]]*)\];"
)
RUST_BLOCKS = re.compile(r"pub const SCALING_BLOCKS:\s*u32\s*=\s*(?P<blocks>\d+);")
RUST_DISTRIBUTION_BLOCKS = re.compile(
    r"pub const DISTRIBUTION_BLOCKS:\s*u32\s*=\s*(?P<blocks>\d+);"
)
RUST_SCHEMA = re.compile(r'pub const SCALING_SCHEMA_VERSION:\s*&str\s*=\s*"(?P<schema>[^"]*)";')
RUST_RSS_SCHEMA = re.compile(
    r'pub const SCALING_RSS_SCHEMA_VERSION:\s*&str\s*=\s*"(?P<schema>[^"]*)";'
)
RUST_PATTERNS = re.compile(
    r"pub const SCALING_PATTERNS:\s*\[ScalingPattern;\s*(?P<length>\d+)\]\s*=\s*"
    r"\[(?P<body>[^\]]*)\];"
)
RUST_PATTERN_NAMES = re.compile(
    r"pub const fn as_str\(self\) -> &'static str \{\s*match self \{(?P<arms>.*?)\}\s*\}",
    re.DOTALL,
)
RUST_PATTERN_ARM = re.compile(r'Self::(\w+)\s*=>\s*"([^"]+)"')
# #508: the thread-churn side-car's protocol, declared on both sides as well.
RUST_THREAD_CHURN_SCHEMA = re.compile(
    r'pub const THREAD_CHURN_SCHEMA_VERSION:\s*&str\s*=\s*"(?P<schema>[^"]*)";'
)
RUST_THREAD_CHURN_THREADS = re.compile(
    r"pub const THREAD_CHURN_THREADS:\s*u32\s*=\s*(?P<threads>\d+);"
)
RUST_THREAD_CHURN_OFFSETS = re.compile(
    r"pub const THREAD_CHURN_POST_DRAIN_OFFSETS_MS:\s*\[u64;\s*(?P<length>\d+)\]\s*=\s*"
    r"\[(?P<offsets>[^\]]*)\];"
)
RUST_THREAD_CHURN_TOLERANCE = re.compile(
    r"pub const THREAD_CHURN_RELEASE_TOLERANCE_BYTES:\s*u64\s*=\s*"
    r"(?:(?P<base>\d+)\s*<<\s*(?P<shift>\d+)|(?P<literal>[\d_]+));"
)


def validate_thread_churn_contract(source: str) -> None:
    """The thread-churn constants the Rust producer emits and Python validates."""

    schema = RUST_THREAD_CHURN_SCHEMA.search(source)
    if schema is None or schema.group("schema") != THREAD_CHURN_SCHEMA:
        fail(f"scaling.rs: THREAD_CHURN_SCHEMA_VERSION must be {THREAD_CHURN_SCHEMA!r}")
    threads = RUST_THREAD_CHURN_THREADS.search(source)
    if threads is None or int(threads.group("threads")) != THREAD_CHURN_THREADS:
        fail(f"scaling.rs: THREAD_CHURN_THREADS must be {THREAD_CHURN_THREADS}")
    if THREAD_CHURN_THREADS not in SCALING_THREAD_POINTS:
        fail("benchmark_report.py: THREAD_CHURN_THREADS must be a declared thread point")
    offsets = RUST_THREAD_CHURN_OFFSETS.search(source)
    if offsets is None:
        fail("scaling.rs: THREAD_CHURN_POST_DRAIN_OFFSETS_MS is missing or not a [u64; N] literal")
    raw_offsets = [item.strip() for item in offsets.group("offsets").split(",") if item.strip()]
    if not all(item.isdigit() for item in raw_offsets):
        fail("scaling.rs: THREAD_CHURN_POST_DRAIN_OFFSETS_MS must be literal milliseconds")
    values = tuple(int(item) for item in raw_offsets)
    if int(offsets.group("length")) != len(values) or values != THREAD_CHURN_OFFSETS_MS:
        fail(
            f"scaling.rs: THREAD_CHURN_POST_DRAIN_OFFSETS_MS is {list(values)} but "
            f"benchmark_report.py declares {list(THREAD_CHURN_OFFSETS_MS)}"
        )
    tolerance = RUST_THREAD_CHURN_TOLERANCE.search(source)
    if tolerance is None:
        fail("scaling.rs: THREAD_CHURN_RELEASE_TOLERANCE_BYTES is missing")
    declared = (
        int(tolerance.group("base")) << int(tolerance.group("shift"))
        if tolerance.group("base") is not None
        else int(tolerance.group("literal").replace("_", ""))
    )
    if declared != THREAD_CHURN_RELEASE_TOLERANCE_BYTES:
        fail(
            f"scaling.rs: THREAD_CHURN_RELEASE_TOLERANCE_BYTES must be "
            f"{THREAD_CHURN_RELEASE_TOLERANCE_BYTES}"
        )


def validate_source_contract(source: str) -> None:
    """The Rust producer and the Python validator must declare the same sweep.

    `SCALING_THREAD_POINTS` is part of the metric comparison key, so a
    one-sided edit does not merely mismatch: it silently starts a history
    lineage the other side rejects. Same for the block count and both schema
    strings. Checked by regex rather than by building the crate so the gate
    stays inside the `python-lint` job that already runs on every `ci/` PR.
    """

    points_match = RUST_THREAD_POINTS.search(source)
    if points_match is None:
        fail("scaling.rs: SCALING_THREAD_POINTS is missing or no longer a [u32; N] literal")
    raw_points = [item.strip() for item in points_match.group("points").split(",") if item.strip()]
    if not all(item.isdigit() for item in raw_points):
        fail("scaling.rs: SCALING_THREAD_POINTS must be literal decimal worker counts")
    points = tuple(int(item) for item in raw_points)
    if int(points_match.group("length")) != len(points):
        fail("scaling.rs: SCALING_THREAD_POINTS array length disagrees with its elements")
    if points != SCALING_THREAD_POINTS:
        fail(
            "scaling.rs: SCALING_THREAD_POINTS is "
            f"{list(points)} but benchmark_report.py declares {list(SCALING_THREAD_POINTS)}"
        )
    if sorted(set(points)) != list(points):
        fail("scaling.rs: SCALING_THREAD_POINTS must be strictly increasing and unique")

    blocks_match = RUST_BLOCKS.search(source)
    if blocks_match is None or int(blocks_match.group("blocks")) != SCALING_BLOCKS:
        fail(f"scaling.rs: SCALING_BLOCKS must be {SCALING_BLOCKS}")
    distribution_blocks = RUST_DISTRIBUTION_BLOCKS.search(source)
    if (
        distribution_blocks is None
        or int(distribution_blocks.group("blocks")) != DISTRIBUTION_BLOCKS
    ):
        fail(f"scaling.rs: DISTRIBUTION_BLOCKS must be {DISTRIBUTION_BLOCKS}")
    schema_match = RUST_SCHEMA.search(source)
    if schema_match is None or schema_match.group("schema") != SCALING_SCHEMA:
        fail(f"scaling.rs: SCALING_SCHEMA_VERSION must be {SCALING_SCHEMA!r}")
    rss_match = RUST_RSS_SCHEMA.search(source)
    if rss_match is None or rss_match.group("schema") != SCALING_RSS_SCHEMA:
        fail(f"scaling.rs: SCALING_RSS_SCHEMA_VERSION must be {SCALING_RSS_SCHEMA!r}")

    patterns_match = RUST_PATTERNS.search(source)
    if patterns_match is None:
        fail("scaling.rs: SCALING_PATTERNS is missing or no longer a [ScalingPattern; N] literal")
    variants = [
        item.strip().removeprefix("ScalingPattern::")
        for item in patterns_match.group("body").split(",")
        if item.strip()
    ]
    if int(patterns_match.group("length")) != len(variants):
        fail("scaling.rs: SCALING_PATTERNS array length disagrees with its elements")
    names_match = RUST_PATTERN_NAMES.search(source)
    if names_match is None:
        fail("scaling.rs: ScalingPattern::as_str is missing or no longer matches self by variant")
    arm_map = dict(RUST_PATTERN_ARM.findall(names_match.group("arms")))
    if not arm_map:
        fail("scaling.rs: ScalingPattern::as_str is missing or no longer matches self by variant")
    ordered_names: list[str] = []
    for variant in variants:
        if variant not in arm_map:
            fail(f"scaling.rs: ScalingPattern::{variant} has no as_str arm")
        ordered_names.append(arm_map[variant])
    if tuple(ordered_names) != SCALING_PATTERN_IDS:
        fail(
            "scaling.rs: SCALING_PATTERNS is "
            f"{ordered_names} but benchmark_report.py declares {list(SCALING_PATTERN_IDS)}"
        )
    validate_thread_churn_contract(source)


def load(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return mapping(value, str(path))


def _build_job(workflow: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], cast(dict[str, Any], workflow["jobs"])["build"])


def _step(workflow: dict[str, Any], name: str, job: str | None = None) -> dict[str, Any]:
    """A step by name, from `build` unless another job is named."""
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


MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "weekly scaling schedule": lambda wf: _triggers(wf).__setitem__(
        "schedule", [{"cron": "23 7 * * 0"}]
    ),
    "push trigger added": lambda wf: _triggers(wf).__setitem__("push", {"branches": ["main"]}),
    "shared concurrency group dropped": lambda wf: wf.__setitem__(
        "concurrency", {"group": "scaling-only", "cancel-in-progress": False}
    ),
    "cancel-in-progress enabled": lambda wf: cast(dict[str, Any], wf["concurrency"]).__setitem__(
        "cancel-in-progress", True
    ),
    "workflow write permission": lambda wf: wf.__setitem__("permissions", {"contents": "write"}),
    "budget exceeded": lambda wf: _build_job(wf).__setitem__("timeout-minutes", 45),
    "matrix introduced": lambda wf: _build_job(wf).__setitem__(
        "strategy", {"matrix": {"os": ["ubuntu-24.04"]}}
    ),
    "measure matrix introduced": lambda wf: cast(
        dict[str, Any], cast(dict[str, Any], wf["jobs"])["measure"]
    ).__setitem__("strategy", {"matrix": {"shard": [0, 1, 2, 3, 4, 5]}}),
    "measure shard loop shortened": lambda wf: _step(
        wf, "run sparse scaling sweep", "measure"
    ).__setitem__("run", "benchmark-scaling-run --blocks 3 --shard-index 0 --shard-count 6"),
    "unpinned action": lambda wf: cast(list[dict[str, Any]], _build_job(wf)["steps"])[
        0
    ].__setitem__("uses", "actions/checkout@v4"),
    "setup-soldr cache preset weakened": lambda wf: cast(
        dict[str, Any],
        next(
            step
            for step in cast(list[dict[str, Any]], _build_job(wf)["steps"])
            if "setup-soldr@" in str(step.get("uses", ""))
        )["with"],
    ).__setitem__("cache-preset", "foundation"),
    "allocators run in parallel": lambda wf: _step(
        wf, "run sparse scaling sweep", "measure"
    ).__setitem__("run", "benchmark-scaling-run --blocks 3 &"),
    "shard count removed": lambda wf: _step(wf, "run sparse scaling sweep", "measure").__setitem__(
        "run", "benchmark-scaling-run --blocks 3 --shard-index 0"
    ),
    "input interpolated into shell": lambda wf: _step(wf, "determine run seed").__setitem__(
        "run", "SEED=${{ inputs.run_seed }}"
    ),
    "seed validation removed": lambda wf: _step(wf, "determine run seed").__setitem__(
        "run", "echo seed=1 >> $GITHUB_OUTPUT"
    ),
    "raw artifact conditional": lambda wf: _step(
        wf, "upload raw scaling shard", "measure"
    ).__setitem__("if", "success()"),
    "retention shortened": lambda wf: cast(
        dict[str, Any], _step(wf, "upload raw scaling shard", "measure")["with"]
    ).__setitem__("retention-days", 1),
    "eligibility accepts any ref": lambda wf: _step(
        wf, "compute publication eligibility", "assemble"
    ).__setitem__("run", "echo publish_eligible=true >> $GITHUB_OUTPUT"),
    "blocks default widened": lambda wf: cast(
        dict[str, Any],
        cast(dict[str, Any], _triggers(wf)["workflow_dispatch"])["inputs"]["blocks"],
    ).__setitem__("default", 15),
    "publish job over-permissioned": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "publish-branch"
    ].__setitem__("permissions", {"contents": "write", "packages": "write"}),
    "lease dropped": lambda wf: cast(
        list[dict[str, Any]], cast(dict[str, Any], wf["jobs"])["publish-branch"]["steps"]
    )[-1].__setitem__("run", "git push origin HEAD:$PUBLISH_REF"),
    # #371: by NAME, not by position. This used to mutate `steps[-1]`, which silently
    # stopped testing the audit step the moment another step was appended after it -- the
    # parity assertion did exactly that, and the control failed to fail.
    "publication audit weakened": lambda wf: _step(
        wf, "audit scaling publication", "publication-audit"
    ).__setitem__("run", "echo ok"),
    # #528: a diagnostic run must never reach publication.
    "diagnostic mode made publication-eligible": lambda wf: _step(
        wf, "compute publication eligibility", "assemble"
    ).__setitem__(
        "run",
        str(_step(wf, "compute publication eligibility", "assemble")["run"]).replace(
            '[ "$SCALING_MODE" = "full" ]', '[ "$SCALING_MODE" != "smoke" ]'
        ),
    ),
    "site rendered in diagnostic mode": lambda wf: _step(
        wf, "render sealed site", "assemble"
    ).__setitem__("if", "(inputs.mode || 'full') != 'smoke'"),
    "site artifact uploaded in diagnostic mode": lambda wf: _step(
        wf, "upload site artifact", "assemble"
    ).__setitem__("if", "success()"),
    "artifact audit runs in every mode": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "artifact-audit"
    ].pop("if"),
    "publish job ungated": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "publish-branch"
    ].__setitem__("if", "always()"),
    "diagnostic mode choice removed": lambda wf: cast(
        dict[str, Any],
        cast(dict[str, Any], _triggers(wf)["workflow_dispatch"])["inputs"]["mode"],
    ).__setitem__("options", ["full", "smoke"]),
    "diagnostic env input dropped": lambda wf: cast(
        dict[str, Any], cast(dict[str, Any], _triggers(wf)["workflow_dispatch"])["inputs"]
    ).pop("diagnostic_env"),
    "diagnostic inputs not validated": lambda wf: _build_job(wf).__setitem__(
        "steps",
        [
            step
            for step in cast(list[dict[str, Any]], _build_job(wf)["steps"])
            if step.get("name") != "validate dispatch inputs"
        ],
    ),
    "fork cppdefs interpolated into shell": lambda wf: _step(
        wf, "build native allocator libraries"
    ).__setitem__(
        "run",
        "python3 ci/build_benchmark_allocators.py --fork-cppdefs ${{ inputs.diagnostic_cppdefs }}",
    ),
    "allocator cache shared across cppdefs": lambda wf: cast(
        dict[str, Any],
        next(
            step
            for step in cast(list[dict[str, Any]], _build_job(wf)["steps"])
            if "actions/cache@" in str(step.get("uses", ""))
        )["with"],
    ).__setitem__("key", "benchmark-allocators-${{ runner.os }}"),
    "diagnostic env not passed to the runner": lambda wf: _step(
        wf, "run sparse scaling sweep", "measure"
    ).__setitem__(
        "run",
        str(_step(wf, "run sparse scaling sweep", "measure")["run"]).replace(
            '--diagnostic-env "$DIAGNOSTIC_ENV" ', ""
        ),
    ),
    "diagnostic run not summarized": lambda wf: _step(
        wf, "summarize diagnostic run", "assemble"
    ).__setitem__("if", "always()"),
}


SOURCE_MUTATIONS: dict[str, Callable[[str], str]] = {
    "thread points diverge from the validator": lambda text: text.replace(
        f"[u32; {len(SCALING_THREAD_POINTS)}] = [{', '.join(str(p) for p in SCALING_THREAD_POINTS)}]",
        "[u32; 3] = [1, 4, 16]",
    ),
    "thread point array length lies": lambda text: text.replace(
        f"[u32; {len(SCALING_THREAD_POINTS)}]", "[u32; 99]"
    ),
    "block count diverges": lambda text: text.replace(
        f"SCALING_BLOCKS: u32 = {SCALING_BLOCKS};", "SCALING_BLOCKS: u32 = 1;"
    ),
    "scaling schema renamed on one side": lambda text: text.replace(
        f'SCALING_SCHEMA_VERSION: &str = "{SCALING_SCHEMA}"',
        'SCALING_SCHEMA_VERSION: &str = "throughput-scaling-dense-v2"',
    ),
    "rss schema renamed on one side": lambda text: text.replace(
        f'SCALING_RSS_SCHEMA_VERSION: &str = "{SCALING_RSS_SCHEMA}"',
        'SCALING_RSS_SCHEMA_VERSION: &str = "throughput-scaling-rss-v2"',
    ),
    "thread points declared out of order": lambda text: text.replace(
        f"[{', '.join(str(p) for p in SCALING_THREAD_POINTS)}]",
        f"[{', '.join(str(p) for p in reversed(SCALING_THREAD_POINTS))}]",
    ),
    "thread points stop being literals": lambda text: text.replace(
        f"[{', '.join(str(p) for p in SCALING_THREAD_POINTS)}]",
        "[1, 2, 3, 4, 6, num_cpus()]",
    ),
    "constant deleted outright": lambda text: text.replace(
        "pub const SCALING_THREAD_POINTS", "const SCALING_THREAD_POINTS_UNUSED"
    ),
    "pattern dropped from the sweep": lambda text: text.replace(
        f"[ScalingPattern; {len(SCALING_PATTERN_IDS)}]",
        f"[ScalingPattern; {len(SCALING_PATTERN_IDS) - 1}]",
    ).replace("    ScalingPattern::XmallocTest,\n", "", 1),
    "pattern renamed": lambda text: text.replace('"larson"', '"larson-v2"'),
    "pattern array length lies": lambda text: text.replace(
        f"[ScalingPattern; {len(SCALING_PATTERN_IDS)}]", "[ScalingPattern; 99]"
    ),
    "thread-churn schema renamed on one side": lambda text: text.replace(
        f'THREAD_CHURN_SCHEMA_VERSION: &str = "{THREAD_CHURN_SCHEMA}"',
        'THREAD_CHURN_SCHEMA_VERSION: &str = "thread-churn-rss-v2"',
    ),
    "thread-churn worker count diverges": lambda text: text.replace(
        f"THREAD_CHURN_THREADS: u32 = {THREAD_CHURN_THREADS};", "THREAD_CHURN_THREADS: u32 = 4;"
    ),
    "thread-churn offsets diverge": lambda text: text.replace(
        f"[{', '.join(str(offset) for offset in THREAD_CHURN_OFFSETS_MS)}]",
        f"[{', '.join(str(offset) for offset in THREAD_CHURN_OFFSETS_MS[:-1])}, 5000]",
    ),
    "thread-churn tolerance diverges": lambda text: text.replace(
        "THREAD_CHURN_RELEASE_TOLERANCE_BYTES: u64 = 1 << 20;",
        "THREAD_CHURN_RELEASE_TOLERANCE_BYTES: u64 = 1 << 22;",
    ),
}


def selftest(path: Path, source_path: Path) -> None:
    """Every declared rule must reject at least one concrete mutation."""

    baseline = load(path)
    validate(baseline)
    for label, mutate in MUTATIONS.items():
        candidate = copy.deepcopy(baseline)
        mutate(cast(dict[str, Any], candidate))
        try:
            validate(candidate)
        except ScalingWorkflowError:
            continue
        fail(f"selftest: the checker accepted a workflow with {label}")

    source = source_path.read_text(encoding="utf-8")
    validate_source_contract(source)
    for label, edit in SOURCE_MUTATIONS.items():
        mutated = edit(source)
        if mutated == source:
            fail(f"selftest: the {label!r} mutation did not change scaling.rs")
        try:
            validate_source_contract(mutated)
        except ScalingWorkflowError:
            continue
        fail(f"selftest: the checker accepted scaling.rs with {label}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    parser.add_argument("--source", type=Path, default=SCALING_SOURCE)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    controls = len(MUTATIONS) + len(SOURCE_MUTATIONS)
    if args.selftest:
        selftest(args.workflow, args.source)
        print(f"PASS benchmark scaling workflow policy selftest ({controls} controls)")
    else:
        validate(load(args.workflow))
        validate_source_contract(args.source.read_text(encoding="utf-8"))
        print(f"PASS benchmark scaling workflow policy: {args.workflow}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScalingWorkflowError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
