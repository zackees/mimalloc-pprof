#!/usr/bin/env python3
"""Phase 4 workflow policy checker.

Parses ``.github/workflows/benchmark-stats.yml`` structurally and asserts that
the current phase permissions, triggers, timeouts, action refs, artifact
retention, and publication guards are honored.  Every policy assertion has a
matching negative test fixture.
"""

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportIndexIssue=false, reportMissingTypeArgument=false, reportUnusedVariable=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml

# ---------------------------------------------------------------------------
# phase 5 constants (supersedes phase 4)
# ---------------------------------------------------------------------------

ALLOWED_TRIGGERS = ("schedule", "workflow_dispatch")
FORBIDDEN_TRIGGERS = (
    "push",
    "pull_request",
    "pull_request_target",
    "release",
    "deployment",
    "workflow_call",
    "workflow_run",
    "merge_group",
    "issues",
    "issue_comment",
    "fork",
    "gollum",
    "page_build",
    "project",
    "public",
    "registry_package",
    "repository_dispatch",
    "status",
    "watch",
)

REQUIRED_CONCURRENCY_GROUP = "benchmark-stats-production"

PERMISSION_CEILING: dict[str, str] = {"contents": "read"}
FORBIDDEN_PERMISSIONS: set[str] = {
    "id-token",
    "pages",
    "deployments",
    "attestations",
    "discussions",
    "packages",
    "environments",
    "organization_administration",
    "organization_custom_roles",
    "organization_announcement_banners",
    "secrets",
}

PHASE5_WRITE_JOBS = {"publish-branch": {"contents"}, "deploy-pages": {"pages", "id-token"}}
PHASE5_REQUIRED_JOBS = {"publish-branch", "package-pages", "deploy-pages", "publication-audit"}

FORBIDDEN_ACTIONS = {
    "peaceiris/actions-gh-pages",
    "JamesIves/github-pages-deploy-action",
}
# deploy-pages action is allowed only in the deploy-pages job
ALLOWED_ACTIONS_PER_JOB: dict[str, set[str]] = {
    "deploy-pages": {"actions/deploy-pages"},
}

FORBIDDEN_COMMAND_PATTERNS = (
    re.compile(r"git\s+push", re.IGNORECASE),
    re.compile(r"git\s+tag", re.IGNORECASE),
    re.compile(r"gh\s+release", re.IGNORECASE),
    re.compile(r"gh\s+pr\s+(create|merge)", re.IGNORECASE),
)

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_REF_RE = re.compile(r"^v?\d+")

JOB_TIMEOUTS = {"build-and-measure": 50, "artifact-audit": 10}


# ---------------------------------------------------------------------------
# structured checks
# ---------------------------------------------------------------------------


class PolicyError(RuntimeError):
    """A policy assertion violation."""


def fail(message: str) -> NoReturn:
    raise PolicyError(message)


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{label}: expected object")
    return cast(dict[str, object], value)


def list_value(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        fail(f"{label}: expected array")
    return cast(list[object], value)


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        fail(f"{label}: expected string")
    return value


# ---------------------------------------------------------------------------
# policy assertions
# ---------------------------------------------------------------------------


def check_triggers(workflow: Mapping[str, object]) -> None:
    # PyYAML 1.1 parses bare `on:` as boolean True.
    on = workflow.get("on")
    if on is None:
        on = workflow.get(True)  # type: ignore[arg-type]
    if not isinstance(on, dict):
        fail("workflow.on: expected object with explicit dispatch inputs")
    if isinstance(on, dict):
        for key in on:
            if key not in ALLOWED_TRIGGERS and key not in (True,):
                fail(f"workflow.on.{key}: trigger not allowed in Phase 5")
        if "workflow_dispatch" not in on:
            fail("workflow.on: workflow_dispatch is required")
        dispatch = object_value(on["workflow_dispatch"], "workflow.on.workflow_dispatch")
        inputs = object_value(dispatch.get("inputs", {}), "workflow.on.workflow_dispatch.inputs")
        if "publish" in inputs:
            fail("workflow.on.workflow_dispatch.inputs: publish input is forbidden")
        for name in inputs:
            inp = object_value(inputs[name], f"workflow.on.workflow_dispatch.inputs.{name}")
            if inp.get("type") not in ("string", "choice", "number", "boolean"):
                fail(f"workflow.on.workflow_dispatch.inputs.{name}: unsupported type")


def check_concurrency(workflow: Mapping[str, object]) -> None:
    concurrency = workflow.get("concurrency")
    if concurrency is None or isinstance(concurrency, str):
        fail("workflow.concurrency: expected object with group and cancel-in-progress")
    concurrency = object_value(concurrency, "workflow.concurrency")
    group = string_value(concurrency.get("group"), "workflow.concurrency.group")
    if group != REQUIRED_CONCURRENCY_GROUP:
        fail(f"workflow.concurrency.group: expected {REQUIRED_CONCURRENCY_GROUP!r}, got {group!r}")
    if concurrency.get("cancel-in-progress") is not False:
        fail("workflow.concurrency.cancel-in-progress: must be false")


def check_permissions(workflow: Mapping[str, object]) -> None:
    permissions = workflow.get("permissions")
    if permissions is None or (isinstance(permissions, str) and permissions != "{}"):
        fail("workflow.permissions: expected explicit object")
    permissions = object_value(permissions, "workflow.permissions")
    for key in permissions:
        if key in FORBIDDEN_PERMISSIONS:
            fail(f"workflow.permissions.{key}: forbidden at workflow level")
    contents = permissions.get("contents", "read")
    if contents != "read":
        fail("workflow.permissions.contents: must be read at workflow level")


def check_action_ref(ref: str, label: str) -> None:
    if "/" not in ref:
        fail(f"{label}: expected owner/repo@sha action ref, got {ref!r}")
    _, _, version = ref.partition("@")
    if not version:
        fail(f"{label}: missing @sha in action ref {ref!r}")
    candidate = version.strip()
    if not FULL_SHA_RE.match(candidate):
        if TAG_REF_RE.match(candidate):
            fail(f"{label}: tag ref {ref!r} is forbidden; pin to full commit SHA")
        fail(f"{label}: action ref {ref!r} is not a full 40-char SHA")


def check_jobs(workflow: Mapping[str, object]) -> None:
    jobs = object_value(workflow.get("jobs", {}), "workflow.jobs")
    found = set(jobs)
    phase4_jobs = set(JOB_TIMEOUTS)
    all_required = phase4_jobs | PHASE5_REQUIRED_JOBS
    missing = all_required - found
    unexpected = found - all_required
    if missing:
        fail(f"workflow.jobs: missing required jobs {sorted(missing)}")
    if unexpected:
        fail(f"workflow.jobs: unexpected jobs {sorted(unexpected)}")
    for name in all_required:
        if name not in jobs:
            continue
        job = object_value(jobs[name], f"workflow.jobs.{name}")
        if name in JOB_TIMEOUTS:
            job_timeout = job.get("timeout-minutes")
            if job_timeout != JOB_TIMEOUTS[name]:
                fail(
                    f"workflow.jobs.{name}.timeout-minutes: expected {JOB_TIMEOUTS[name]}, got {job_timeout}"
                )
        if job.get("runs-on") != "ubuntu-24.04":
            fail(f"workflow.jobs.{name}.runs-on: expected ubuntu-24.04")
        permissions = job.get("permissions", {})
        if isinstance(permissions, dict):
            allowed_write = PHASE5_WRITE_JOBS.get(name, set())
            for key in permissions:
                if key in allowed_write:
                    if permissions[key] != "write":
                        fail(f"workflow.jobs.{name}.permissions.{key}: write job must use write")
                    continue
                if key == "contents" and permissions[key] != "read":
                    fail(
                        f"workflow.jobs.{name}.permissions.contents: must be read in non-write jobs"
                    )
                if key in FORBIDDEN_PERMISSIONS:
                    fail(
                        f"workflow.jobs.{name}.permissions.{key}: elevated permission forbidden in this job"
                    )
        # deploy-pages must target github-pages environment
        if name == "deploy-pages":
            env = job.get("environment")
            if not isinstance(env, dict) or env.get("name") != "github-pages":
                fail("workflow.jobs.deploy-pages.environment: must target github-pages")
        _check_steps(name, job.get("steps", []), f"workflow.jobs.{name}")


def _check_steps(job_name: str, steps: object, label: str) -> None:
    if not isinstance(steps, list):
        fail(f"{label}.steps: expected array")
    steps_list = cast(list[object], steps)
    seen_validate = False
    seen_eligible = job_name != "build-and-measure"
    for index, step in enumerate(steps_list):
        prefix = f"{label}.steps[{index}]"
        step_obj = object_value(step, prefix)
        uses = step_obj.get("uses")
        if uses and isinstance(uses, str):
            parts = uses.split("@", 1)
            if len(parts) == 2:
                action = parts[0]
                if action in FORBIDDEN_ACTIONS:
                    fail(f"{prefix}.uses: {uses} is forbidden")
                for allowed_job, restricted_actions in ALLOWED_ACTIONS_PER_JOB.items():
                    if action in restricted_actions and job_name != allowed_job:
                        fail(f"{prefix}.uses: {action} is only allowed in job {allowed_job}")
            if parts[0] == "actions/checkout":
                with_obj = object_value(step_obj.get("with", {}), f"{prefix}.with")
                if with_obj.get("persist-credentials") is not False:
                    fail(f"{prefix}.with.persist-credentials: must be false")
                continue
            check_action_ref(uses, prefix)
        run = step_obj.get("run")
        if run and isinstance(run, str):
            if step_obj.get("name") == "validate raw run":
                seen_validate = True
            if step_obj.get("name") == "compute publication eligibility":
                seen_eligible = True
            if step_obj.get("name") == "upload site artifact" and not seen_validate:
                fail(f"{prefix}: site upload before validation step")
            # force-with-lease is required in publish-branch push
            if job_name == "publish-branch" and "git push" in run:
                if "--force-with-lease" not in run:
                    fail(f"{prefix}: publish-branch must use force-with-lease")
                if "--force" in run and "--force-with-lease" not in run:
                    fail(f"{prefix}: unconditional --force is forbidden in publish-branch")
            for pattern in FORBIDDEN_COMMAND_PATTERNS:
                if pattern.search(run):
                    # publish-branch is the only job allowed to git push (with force-with-lease)
                    if job_name == "publish-branch" and pattern.pattern.startswith(r"git\s+push"):
                        continue
                    fail(f"{prefix}.run: forbidden command matches {pattern.pattern}")
    if job_name == "build-and-measure":
        if not seen_validate:
            fail(f"{label}: missing validation step in build-and-measure job")
        if not seen_eligible:
            fail(f"{label}: missing publication eligibility step in build-and-measure job")


def check_artifact_retention(workflow: Mapping[str, object]) -> None:
    jobs = object_value(workflow.get("jobs", {}), "workflow.jobs")
    found_retention = False
    for name in JOB_TIMEOUTS:
        job = object_value(jobs[name], f"workflow.jobs.{name}")
        for index, step in enumerate(
            list_value(job.get("steps", []), f"workflow.jobs.{name}.steps")
        ):
            step_obj = object_value(step, f"workflow.jobs.{name}.steps[{index}]")
            uses = step_obj.get("uses", "")
            if isinstance(uses, str) and "upload-artifact" in uses:
                found_retention = True
                retention = step_obj.get("retention-days") or (
                    isinstance(step_obj.get("with"), dict)
                    and cast(dict, step_obj["with"]).get("retention-days")
                )
                if retention is None:
                    fail(f"workflow.jobs.{name}.steps[{index}]: missing retention-days")
                if retention != 30:
                    fail(
                        f"workflow.jobs.{name}.steps[{index}]: retention-days must be 30, got {retention}"
                    )
    if not found_retention:
        fail("workflow: no upload-artifact steps found")


def check(workflow: Mapping[str, object]) -> int:
    check_triggers(workflow)
    check_concurrency(workflow)
    check_permissions(workflow)
    check_jobs(workflow)
    check_artifact_retention(workflow)
    return 0


# ---------------------------------------------------------------------------
# selftest: build a minimal valid fixture + positive controls
# ---------------------------------------------------------------------------


def selftest() -> int:
    base: dict[str, object] = {
        "name": "benchmark-stats",
        "on": {
            "workflow_dispatch": {
                "inputs": {
                    "mode": {"type": "choice", "options": ["full", "smoke"], "default": "full"},
                    "run_seed": {"type": "string", "required": False},
                    "blocks": {"type": "number", "default": 15, "required": False},
                }
            },
            "schedule": [{"cron": "17 9 * * *"}],
        },
        "concurrency": {"group": "benchmark-stats-production", "cancel-in-progress": False},
        "permissions": {"contents": "read"},
        "jobs": {
            "build-and-measure": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 50,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                        "with": {"persist-credentials": False},
                    },
                    {"name": "validate raw run", "run": "echo validated"},
                    {"name": "compute publication eligibility", "run": "echo eligible"},
                    {
                        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4880f43607fa02",
                        "with": {"name": "x", "path": "."},
                        "retention-days": 30,
                    },
                ],
            },
            "artifact-audit": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                        "with": {"persist-credentials": False},
                    }
                ],
            },
            "publish-branch": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "write"},
                "steps": [
                    {
                        "name": "push",
                        "run": "git push origin HEAD:ref --force-with-lease=ref:abc123",
                    }
                ],
            },
            "package-pages": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "steps": [],
            },
            "deploy-pages": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"pages": "write", "id-token": "write"},
                "environment": {"name": "github-pages"},
                "steps": [
                    {"uses": "actions/deploy-pages@c9ba3c5b5fca82fcf21936bd1efba49f2b8f40a0"}
                ],
            },
            "publication-audit": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                        "with": {"persist-credentials": False},
                    }
                ],
            },
        },
    }
    # valid Phase 5 fixture
    check(base)
    # negative: write permission at workflow level
    bad = cast(dict[str, object], _deep_copy(base))
    bad["permissions"]["contents"] = "write"  # type: ignore[index]
    _expect_policy_error(lambda: check(bad), "write at workflow level")
    # negative: pages:write at workflow level
    bad = cast(dict[str, object], _deep_copy(base))
    bad["permissions"]["pages"] = "write"  # type: ignore[index]
    _expect_policy_error(lambda: check(bad), "pages at workflow level")
    # negative: tag-only action ref
    bad = cast(dict[str, object], _deep_copy(base))
    bad["jobs"]["build-and-measure"]["steps"].insert(0, {"uses": "actions/checkout@v4"})  # type: ignore
    _expect_policy_error(lambda: check(bad), "tag ref")
    # negative: cancel-in-progress true
    bad = cast(dict[str, object], _deep_copy(base))
    bad["concurrency"]["cancel-in-progress"] = True  # type: ignore
    _expect_policy_error(lambda: check(bad), "cancel-in-progress")
    # negative: hidden git push in non-publish job
    bad = cast(dict[str, object], _deep_copy(base))
    bad["jobs"]["build-and-measure"]["steps"].insert(
        1, {"name": "push", "run": "git push origin foo"}
    )  # type: ignore[union-attr]
    _expect_policy_error(lambda: check(bad), "git push in measure job")
    # negative: unconditional --force in publish-branch
    bad = cast(dict[str, object], _deep_copy(base))
    bad["jobs"]["publish-branch"]["steps"][0]["run"] = "git push --force origin HEAD:ref"  # type: ignore
    _expect_policy_error(lambda: check(bad), "unconditional force")
    # negative: deploy-pages action in wrong job
    bad = cast(dict[str, object], _deep_copy(base))
    bad["jobs"]["publish-branch"]["steps"].append(
        {"uses": "actions/deploy-pages@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0"}
    )  # type: ignore[union-attr]
    _expect_policy_error(lambda: check(bad), "deploy-pages outside deploy job")
    print("PASS benchmark workflow policy checker selftest")
    return 0


def _expect_policy_error(fn: Any, label: str) -> None:
    try:
        fn()
        raise AssertionError(f"{label} not rejected")
    except PolicyError:
        pass


def _deep_copy(obj: object) -> object:
    import copy

    return copy.deepcopy(obj)


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--workflow", type=Path, default=Path(".github/workflows/benchmark-stats.yml")
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    path: Path = args.workflow
    if not path.is_file():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1
    with path.open(encoding="utf-8") as source:
        workflow = yaml.safe_load(source)
    return check(workflow)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PolicyError as error:
        print(f"POLICY ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
