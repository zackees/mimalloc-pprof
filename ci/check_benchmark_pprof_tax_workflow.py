#!/usr/bin/env python3
"""Fail-closed policy checker for the pprof compilation/runtime tax workflow.

Mirrors ``check_benchmark_scaling_workflow.py``: ``--selftest`` mutates a copy
of the on-disk workflow once per rule and requires every mutation to be
rejected, and the Rust producer / Python validator contract for
``PPROF_TAX_MIN_BLOCKS`` is compared so a one-sided edit cannot silently
diverge until a scheduled run has already spent its budget.
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

from benchmark_report import PPROF_TAX_MIN_BLOCKS, PPROF_TAX_SCHEMA
from check_benchmark_workflow import check_action_ref

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "benchmark-pprof-tax.yml"
# The block-count floor is declared twice: the Rust producer emits it and the
# Python validator rejects anything short of it. Drift is silent until a
# scheduled run has already spent its budget and fails at overlay time, so the
# two declarations are compared here, in a job that runs on every ci/ PR.
PPROF_TAX_SOURCE = (
    Path(__file__).resolve().parents[1] / "rust" / "benchmark-suite" / "src" / "pprof_tax.rs"
)
JOBS = {
    "build-and-measure",
    "artifact-audit",
    "publish-branch",
    "package-pages",
    "deploy-pages",
    "publication-audit",
}
# Publication chain, in dependency order, whose timeouts are the actual critical path.
CHAIN = (
    "build-and-measure",
    "artifact-audit",
    "publish-branch",
    "package-pages",
    "deploy-pages",
    "publication-audit",
)
# The build/measure job is by far the most expensive; the rest audit and publish bytes
# that already exist. The chain budget is part of the contract, not an accident of
# whatever timeouts happened to be set.
MAXIMUM_BUILD_TIMEOUT_MINUTES = 40
MAXIMUM_CHAIN_TIMEOUT_MINUTES = 60


class PprofTaxWorkflowError(RuntimeError):
    """A pprof-tax workflow policy assertion failed."""


def fail(message: str) -> NoReturn:
    raise PprofTaxWorkflowError(message)


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
                check_action_ref(action, "pprof-tax workflow action")
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
    if schedule != [{"cron": "37 4 * * 0"}]:
        fail("workflow.on.schedule: expected weekly cron '37 4 * * 0' (#187)")
    dispatch = mapping(triggers.get("workflow_dispatch"), "workflow.on.workflow_dispatch")
    inputs = mapping(dispatch.get("inputs"), "workflow.on.workflow_dispatch.inputs")
    if set(inputs) != {"mode", "run_seed", "blocks"}:
        fail("workflow dispatch inputs must be exactly mode/run_seed/blocks")
    mode = mapping(inputs["mode"], "workflow input mode")
    if mode.get("options") != ["full", "smoke"] or mode.get("default") != "full":
        fail("workflow input mode must default to full with full/smoke choices")
    blocks_input = mapping(inputs["blocks"], "workflow input blocks")
    blocks_default = blocks_input.get("default")
    if not isinstance(blocks_default, int) or blocks_default < PPROF_TAX_MIN_BLOCKS:
        fail(f"workflow input blocks must default to at least {PPROF_TAX_MIN_BLOCKS}")

    concurrency = mapping(workflow.get("concurrency"), "workflow.concurrency")
    if concurrency != {"group": "benchmark-stats-production", "cancel-in-progress": False}:
        fail("pprof-tax workflow must serialize with the production publication group")
    if mapping(workflow.get("permissions"), "workflow.permissions") != {"contents": "read"}:
        fail("workflow permissions must be contents: read")

    jobs = mapping(workflow.get("jobs"), "workflow.jobs")
    if set(jobs) != JOBS:
        fail(f"workflow jobs mismatch: expected {sorted(JOBS)}")
    chain_total = 0
    for name, value in jobs.items():
        job = mapping(value, f"workflow.jobs.{name}")
        if job.get("runs-on") != "ubuntu-24.04":
            fail(f"workflow.jobs.{name}.runs-on: expected ubuntu-24.04")
        timeout = job.get("timeout-minutes")
        if not isinstance(timeout, int):
            fail(f"workflow.jobs.{name}.timeout-minutes: expected an integer")
        if "strategy" in job:
            fail(f"workflow.jobs.{name}: parallel matrices are forbidden")
        if name in CHAIN:
            chain_total += timeout
        steps_by_name(job)
    if chain_total > MAXIMUM_CHAIN_TIMEOUT_MINUTES:
        fail(
            "workflow.jobs: publication chain timeout budget exceeds "
            f"{MAXIMUM_CHAIN_TIMEOUT_MINUTES} minutes (got {chain_total})"
        )

    build = mapping(jobs["build-and-measure"], "build-and-measure")
    build_timeout = build.get("timeout-minutes")
    if not isinstance(build_timeout, int) or build_timeout > MAXIMUM_BUILD_TIMEOUT_MINUTES:
        fail(f"build-and-measure must enforce the {MAXIMUM_BUILD_TIMEOUT_MINUTES}-minute limit")
    steps = steps_by_name(build)
    run_step = mapping(steps.get("run pprof-tax matrix"), "run pprof-tax matrix")
    run = run_step.get("run")
    if (
        not isinstance(run, str)
        or "benchmark-pprof-tax-run" not in run
        or "--blocks" not in run
        or "--manifest" not in run
    ):
        fail(
            "pprof-tax measurement step must execute benchmark-pprof-tax-run with "
            "explicit blocks and a manifest"
        )
    if "--reduced-smoke" not in run:
        fail("pprof-tax measurement step must be able to pass --reduced-smoke")
    if "-lt 15" not in run:
        fail("pprof-tax measurement step must reject smoke runs with >=15 blocks")
    if "-ge 15" not in run:
        fail("pprof-tax measurement step must reject full runs with <15 blocks")
    # A lone `&` backgrounds a job; `&&` is ordinary shell "and" (used above for the
    # smoke-mode bound check) and must not trip this.
    if re.search(r"(?<!&)&(?!&)", run) or "parallel" in run or "xargs" in run:
        fail("pprof-tax configurations must execute sequentially")
    for step_name in (
        "determine run seed",
        "run pprof-tax matrix",
        "compute publication eligibility",
    ):
        step = mapping(steps.get(step_name), step_name)
        if "${{ inputs." in str(step.get("run", "")):
            fail(f"{step_name}: workflow inputs must enter shell through env, not source text")
    seed_step = mapping(steps.get("determine run seed"), "determine run seed")
    seed_env = mapping(seed_step.get("env"), "determine run seed.env")
    if "INPUT_RUN_SEED" not in seed_env or "*[!0-9]*" not in str(seed_step.get("run", "")):
        fail("run seed must use an env boundary and strict decimal validation")

    validate_step = mapping(
        steps.get("validate and overlay complete pprof-tax report"), "validate pprof-tax report"
    )
    validate_run = validate_step.get("run")
    if not isinstance(validate_run, str):
        fail("validate pprof-tax report step needs a shell command")
    for required in (
        "benchmark-pprof-tax-validate",
        "--manifest",
        "--raw-artifact-sha256",
        "--base-latest",
    ):
        if required not in validate_run:
            fail(f"validate pprof-tax report step is missing {required!r}")

    raw = mapping(steps.get("upload raw pprof-tax artifact"), "upload raw pprof-tax artifact")
    if raw.get("if") != "always()":
        fail("raw pprof-tax artifact must upload with if: always()")
    raw_with = mapping(raw.get("with"), "upload raw pprof-tax artifact.with")
    if raw_with.get("retention-days") != 30 or raw_with.get("include-hidden-files") is not True:
        fail("raw pprof-tax artifact must retain all bytes for 30 days")
    raw_path = raw_with.get("path")
    if not isinstance(raw_path, str) or "pprof-tax-output-" not in raw_path:
        fail("raw pprof-tax artifact must upload the whole measurement output directory")

    site_upload = mapping(steps.get("upload site artifact"), "upload site artifact")
    site_with = mapping(site_upload.get("with"), "upload site artifact.with")
    site_path = site_with.get("path")
    if not isinstance(site_path, str) or "/site/" not in site_path:
        fail("site artifact must upload only the sealed site directory")
    # profiles/ and the manifest live only under the raw output directory, which must
    # never become the source of the site/Pages artifact.
    for forbidden in ("pprof-tax-output", "profiles", "manifest"):
        if forbidden in site_path:
            fail(f"site artifact path must not reference {forbidden!r}; belongs to raw artifact")

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
    for name, value in jobs.items():
        if name == "publish-branch":
            continue
        job = mapping(value, f"workflow.jobs.{name}")
        permissions = job.get("permissions")
        if isinstance(permissions, Mapping) and (
            cast(Mapping[str, object], permissions).get("contents") == "write"
        ):
            fail(f"workflow.jobs.{name}: only publish-branch may use contents: write")
    deploy = mapping(jobs["deploy-pages"], "deploy-pages")
    if mapping(deploy.get("permissions"), "deploy permissions") != {
        "pages": "write",
        "id-token": "write",
    }:
        fail("deploy-pages requires only pages/id-token write")
    for name, value in jobs.items():
        if name == "deploy-pages":
            continue
        job = mapping(value, f"workflow.jobs.{name}")
        permissions = job.get("permissions")
        granted: Mapping[str, object] = (
            cast(Mapping[str, object], permissions) if isinstance(permissions, Mapping) else {}
        )
        if granted.get("pages") == "write" or granted.get("id-token") == "write":
            fail(f"workflow.jobs.{name}: only deploy-pages may use pages/id-token write")
    if mapping(deploy.get("environment"), "deploy environment").get("name") != "github-pages":
        fail("deploy-pages must target the github-pages environment")
    publish_text = str(publish)
    if "--force-with-lease" not in publish_text or "prepare-branch" not in publish_text:
        fail("branch publication must use exact replacement and a lease")
    package_text = str(jobs["package-pages"])
    if (
        "benchmark-pprof-tax-site-" not in publish_text
        or "benchmark-pprof-tax-site-" not in package_text
    ):
        fail("branch and Pages must consume the same sealed pprof-tax site artifact")
    audit_text = str(jobs["publication-audit"])
    for required in ("validate-revision", "audit-pages"):
        if required not in audit_text:
            fail(f"publication audit is missing {required}")


RUST_MIN_BLOCKS = re.compile(r"pub const PPROF_TAX_MIN_BLOCKS:\s*u32\s*=\s*(?P<blocks>\d+);")
RUST_SCHEMA = re.compile(r'pub const PPROF_TAX_SCHEMA_VERSION:\s*&str\s*=\s*"(?P<schema>[^"]*)";')


def validate_source_contract(source: str) -> None:
    """The Rust producer and the Python validator must declare the same contract.

    ``PPROF_TAX_MIN_BLOCKS`` is the publishable-run threshold both sides
    enforce independently -- the workflow's eligibility gate and the Rust
    validator binary's rejection of under-sized runs. ``PPROF_TAX_SCHEMA`` /
    ``PPROF_TAX_SCHEMA_VERSION`` is the report format tag both sides stamp
    and check. A one-sided edit to either does not merely mismatch: it
    silently starts accepting or rejecting runs, or reports, the other side
    disagrees with. Checked by regex rather than by building the crate so
    the gate stays inside the ``python-lint`` job that already runs on
    every ``ci/`` PR.
    """

    blocks_match = RUST_MIN_BLOCKS.search(source)
    if blocks_match is None:
        fail("pprof_tax.rs: PPROF_TAX_MIN_BLOCKS is missing or no longer a u32 literal")
    if int(blocks_match.group("blocks")) != PPROF_TAX_MIN_BLOCKS:
        fail(
            "pprof_tax.rs: PPROF_TAX_MIN_BLOCKS is "
            f"{blocks_match.group('blocks')} but benchmark_report.py declares "
            f"{PPROF_TAX_MIN_BLOCKS}"
        )
    schema_match = RUST_SCHEMA.search(source)
    if schema_match is None or schema_match.group("schema") != PPROF_TAX_SCHEMA:
        fail(f"pprof_tax.rs: PPROF_TAX_SCHEMA_VERSION must be {PPROF_TAX_SCHEMA!r}")


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


MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "monthly pprof-tax schedule": lambda wf: _triggers(wf).__setitem__(
        "schedule", [{"cron": "37 4 1 * *"}]
    ),
    "push trigger added": lambda wf: _triggers(wf).__setitem__("push", {"branches": ["main"]}),
    "shared concurrency group dropped": lambda wf: wf.__setitem__(
        "concurrency", {"group": "pprof-tax-only", "cancel-in-progress": False}
    ),
    "cancel-in-progress enabled": lambda wf: cast(dict[str, Any], wf["concurrency"]).__setitem__(
        "cancel-in-progress", True
    ),
    "workflow write permission": lambda wf: wf.__setitem__("permissions", {"contents": "write"}),
    "build budget exceeded": lambda wf: _build_job(wf).__setitem__("timeout-minutes", 55),
    "chain budget exceeded": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "publication-audit"
    ].__setitem__("timeout-minutes", 25),
    "matrix introduced": lambda wf: _build_job(wf).__setitem__(
        "strategy", {"matrix": {"configuration": ["a", "b"]}}
    ),
    "unpinned action": lambda wf: cast(list[dict[str, Any]], _build_job(wf)["steps"])[
        0
    ].__setitem__("uses", "actions/checkout@v4"),
    "configurations run in parallel": lambda wf: _step(wf, "run pprof-tax matrix").__setitem__(
        "run", "benchmark-pprof-tax-run --blocks 15 &"
    ),
    "input interpolated into shell": lambda wf: _step(wf, "determine run seed").__setitem__(
        "run", "SEED=${{ inputs.run_seed }}"
    ),
    "seed validation removed": lambda wf: _step(wf, "determine run seed").__setitem__(
        "run", "echo seed=1 >> $GITHUB_OUTPUT"
    ),
    "smoke publishable": lambda wf: _step(wf, "run pprof-tax matrix").__setitem__(
        "run", str(_step(wf, "run pprof-tax matrix")["run"]).replace("-lt 15", "-lt 999")
    ),
    "manifest leaks into the site artifact": lambda wf: cast(
        dict[str, Any], _step(wf, "upload site artifact")["with"]
    ).__setitem__("path", "${{ runner.temp }}/site/manifest-copy/"),
    "raw artifact conditional": lambda wf: _step(wf, "upload raw pprof-tax artifact").__setitem__(
        "if", "success()"
    ),
    "retention shortened": lambda wf: cast(
        dict[str, Any], _step(wf, "upload raw pprof-tax artifact")["with"]
    ).__setitem__("retention-days", 1),
    "eligibility accepts any ref": lambda wf: _step(
        wf, "compute publication eligibility"
    ).__setitem__("run", "echo publish_eligible=true >> $GITHUB_OUTPUT"),
    "blocks default undershoots the floor": lambda wf: cast(
        dict[str, Any],
        cast(dict[str, Any], _triggers(wf)["workflow_dispatch"])["inputs"]["blocks"],
    ).__setitem__("default", PPROF_TAX_MIN_BLOCKS - 1),
    "publish job over-permissioned": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "publish-branch"
    ].__setitem__("permissions", {"contents": "write", "packages": "write"}),
    "another job grants contents write": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "artifact-audit"
    ].__setitem__("permissions", {"contents": "write"}),
    "another job grants pages write": lambda wf: cast(dict[str, Any], wf["jobs"])[
        "package-pages"
    ].__setitem__("permissions", {"contents": "read", "pages": "write"}),
    "lease dropped": lambda wf: cast(
        list[dict[str, Any]], cast(dict[str, Any], wf["jobs"])["publish-branch"]["steps"]
    )[-1].__setitem__("run", "git push origin HEAD:$PUBLISH_REF"),
    "publication audit weakened": lambda wf: _step(
        wf, "audit pprof-tax publication", "publication-audit"
    ).__setitem__("run", "echo ok"),
    "validate step loses raw-artifact provenance": lambda wf: _step(
        wf, "validate and overlay complete pprof-tax report"
    ).__setitem__(
        "run",
        str(_step(wf, "validate and overlay complete pprof-tax report")["run"]).replace(
            "--raw-artifact-sha256", "--skip-sha256"
        ),
    ),
}


SOURCE_MUTATIONS: dict[str, Callable[[str], str]] = {
    "min blocks diverges": lambda text: text.replace(
        f"PPROF_TAX_MIN_BLOCKS: u32 = {PPROF_TAX_MIN_BLOCKS};", "PPROF_TAX_MIN_BLOCKS: u32 = 1;"
    ),
    "min blocks constant deleted outright": lambda text: text.replace(
        "pub const PPROF_TAX_MIN_BLOCKS", "const PPROF_TAX_MIN_BLOCKS_UNUSED"
    ),
    "min blocks type changed away from u32": lambda text: text.replace(
        f"PPROF_TAX_MIN_BLOCKS: u32 = {PPROF_TAX_MIN_BLOCKS};",
        f"PPROF_TAX_MIN_BLOCKS: u64 = {PPROF_TAX_MIN_BLOCKS};",
    ),
    "min blocks stops being a literal": lambda text: text.replace(
        f"PPROF_TAX_MIN_BLOCKS: u32 = {PPROF_TAX_MIN_BLOCKS};", "PPROF_TAX_MIN_BLOCKS: u32 = MIN;"
    ),
    "min blocks widened past the workflow default": lambda text: text.replace(
        f"PPROF_TAX_MIN_BLOCKS: u32 = {PPROF_TAX_MIN_BLOCKS};", "PPROF_TAX_MIN_BLOCKS: u32 = 999;"
    ),
    "schema renamed on one side": lambda text: text.replace(
        f'PPROF_TAX_SCHEMA_VERSION: &str = "{PPROF_TAX_SCHEMA}"',
        'PPROF_TAX_SCHEMA_VERSION: &str = "pprof-compilation-runtime-tax-v2"',
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
        except PprofTaxWorkflowError:
            continue
        fail(f"selftest: the checker accepted a workflow with {label}")

    source = source_path.read_text(encoding="utf-8")
    validate_source_contract(source)
    for label, edit in SOURCE_MUTATIONS.items():
        mutated = edit(source)
        if mutated == source:
            fail(f"selftest: the {label!r} mutation did not change pprof_tax.rs")
        try:
            validate_source_contract(mutated)
        except PprofTaxWorkflowError:
            continue
        fail(f"selftest: the checker accepted pprof_tax.rs with {label}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    parser.add_argument("--source", type=Path, default=PPROF_TAX_SOURCE)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    controls = len(MUTATIONS) + len(SOURCE_MUTATIONS)
    if args.selftest:
        selftest(args.workflow, args.source)
        print(f"PASS benchmark pprof-tax workflow policy selftest ({controls} controls)")
    else:
        validate(load(args.workflow))
        validate_source_contract(args.source.read_text(encoding="utf-8"))
        print(f"PASS benchmark pprof-tax workflow policy: {args.workflow}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PprofTaxWorkflowError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
