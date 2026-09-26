"""Fail a PR check if a selected CI job was skipped or did not succeed."""

from __future__ import annotations

import argparse
import json
import os
from typing import cast

FULL_JOBS: dict[str, set[str]] = {
    "c-unit": {
        "build",
        "build-bun-objects",
        "build-windows-native",
        "run-linux",
        "run-linux-serial",
        "coverage",
        "run-windows-native",
        "fastpath-identity",
        "isa-baseline-arm64",
    },
    "cross": {"build-linux", "build-cross", "build-win-gnu", "test-binaries"},
    "rust-native": {"test", "test-win-gnu", "test-no-pprof"},
    "windows-bundles": {
        "build-windows-gnu",
        "build-rust-windows-gnu",
        "build-windows-msvc",
        "build-rust-windows-msvc",
        "run-windows",
    },
    "macos-bundles": {"build-macos", "build-rust-apple", "run-macos-native-full"},
    "asan": {"asan"},
    "fuzz": {"fuzz"},
    "purge-teardown-regression": {"build", "reproduce"},
}
TEST_TIER = frozenset({"c-unit", "asan", "fuzz", "purge-teardown-regression"})


def required_jobs(
    workflow: str, external: bool, ci_test: bool, ci_full: bool, selective_macos: bool
) -> set[str]:
    expected = {"pr-ci-mode"}
    if workflow in {"c-unit", "cross", "rust-native", "windows-bundles", "macos-bundles"}:
        expected.add("resolve-candidate")
    if workflow == "macos-bundles":
        expected.update({"decide", "build-macos"})
        if selective_macos:
            expected.add("run-macos-x64-selective")
    if external or ci_full or (ci_test and workflow in TEST_TIER):
        expected.update(FULL_JOBS[workflow])
    return expected


def failures(
    workflow: str, needs: dict[str, object], ci_test: bool, ci_full: bool, expected_sha: str = ""
) -> list[str]:
    mode_value = needs.get("pr-ci-mode")
    mode = cast("dict[str, object]", mode_value) if isinstance(mode_value, dict) else {}
    if mode.get("result") != "success":
        return ["pr-ci-mode: missing or unsuccessful"]
    outputs_value = mode.get("outputs")
    outputs = cast("dict[str, object]", outputs_value) if isinstance(outputs_value, dict) else {}
    external = outputs.get("external")
    if external not in {"true", "false"}:
        return ["pr-ci-mode: external decision missing or invalid"]
    decide_value = needs.get("decide")
    decide = cast("dict[str, object]", decide_value) if isinstance(decide_value, dict) else {}
    decide_outputs_value = decide.get("outputs")
    decide_outputs = (
        cast("dict[str, object]", decide_outputs_value)
        if isinstance(decide_outputs_value, dict)
        else {}
    )
    selective = decide_outputs.get("run") == "true"
    expected = required_jobs(workflow, external == "true", ci_test, ci_full, selective)
    errors: list[str] = []
    if "resolve-candidate" in expected and expected_sha:
        resolver_value = needs.get("resolve-candidate")
        resolver = (
            cast("dict[str, object]", resolver_value) if isinstance(resolver_value, dict) else {}
        )
        candidate_outputs_value = resolver.get("outputs")
        candidate_outputs = (
            cast("dict[str, object]", candidate_outputs_value)
            if isinstance(candidate_outputs_value, dict)
            else {}
        )
        candidate_sha = candidate_outputs.get("sha")
        if candidate_sha != expected_sha:
            errors.append(f"resolve-candidate: expected {expected_sha}, got {candidate_sha}")
    for job in sorted(expected):
        status_value = needs.get(job)
        status = cast("dict[str, object]", status_value) if isinstance(status_value, dict) else {}
        result = status.get("result", "missing")
        if result != "success":
            errors.append(f"{job}: {result}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", choices=FULL_JOBS, required=True)
    args = parser.parse_args()
    needs_value: object = json.loads(os.environ["NEEDS_JSON"])
    if not isinstance(needs_value, dict):
        raise ValueError("NEEDS_JSON must be an object")
    needs = cast("dict[str, object]", needs_value)
    errors = failures(
        args.workflow,
        needs,
        os.environ.get("CI_TEST") == "true",
        os.environ.get("CI_FULL") == "true",
        os.environ["EXPECTED_SHA"],
    )
    if errors:
        for error in errors:
            print(f"::error::{error}")
        raise SystemExit(1)
    print(f"{args.workflow}: all selected PR jobs succeeded")


if __name__ == "__main__":
    main()
