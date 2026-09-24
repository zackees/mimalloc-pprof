"""Fail-closed GitHub Actions evidence check for a release candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, cast

MANIFEST = Path(__file__).with_name("release_full_ci_manifest.v1.json")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class GateError(ValueError):
    """Release evidence is missing or unsafe."""


def api_get(path: str, *, repo: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def check(
    *,
    candidate_sha: str,
    run_ids: dict[str, int],
    manifest: dict[str, Any],
    get: Callable[[str], dict[str, Any]],
) -> None:
    if not SHA.fullmatch(candidate_sha):
        raise GateError("candidate_sha must be a complete lowercase commit SHA")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("workflows"), dict):
        raise GateError("unsupported release CI manifest")
    workflows = manifest["workflows"]
    if not workflows or set(run_ids) != set(workflows):
        raise GateError("exactly one run ID is required for every manifest workflow")
    if len(set(run_ids.values())) != len(run_ids) or any(
        type(value) is not int or value <= 0 for value in run_ids.values()
    ):
        raise GateError("run IDs must be distinct positive integers")

    for filename, spec in workflows.items():
        if not re.fullmatch(r"[a-z0-9-]+\.yml", filename):
            raise GateError(f"unsafe workflow filename: {filename}")
        expected_raw = spec.get("jobs")
        verifier = spec.get("verifier", "").format(sha=candidate_sha)
        if (
            not isinstance(expected_raw, list)
            or not expected_raw
            or not all(isinstance(name, str) for name in cast(list[object], expected_raw))
        ):
            raise GateError(f"invalid required jobs for {filename}")
        expected = cast(list[str], expected_raw)
        if len(set(expected)) != len(expected) or verifier in expected:
            raise GateError(f"invalid required jobs for {filename}")
        run_id = run_ids[filename]
        run = get(f"actions/runs/{run_id}")
        workflow = get(f"actions/workflows/{run['workflow_id']}")
        required_run = f"{filename.removesuffix('.yml')}/full/{candidate_sha}"
        if workflow.get("path") != f".github/workflows/{filename}":
            raise GateError(f"run {run_id} is not {filename}")
        if (run.get("event"), run.get("head_branch"), run.get("display_title")) != (
            "workflow_dispatch",
            "main",
            required_run,
        ):
            raise GateError(
                f"run {run_id} is not a trusted main-sourced full dispatch for {candidate_sha}"
            )
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise GateError(f"run {run_id} did not complete successfully")

        jobs: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = get(f"actions/runs/{run_id}/jobs?per_page=100&page={page}")
            chunk = batch.get("jobs")
            if not isinstance(chunk, list):
                raise GateError(f"invalid jobs response for run {run_id}")
            typed_chunk = cast(list[dict[str, Any]], chunk)
            jobs.extend(typed_chunk)
            if len(typed_chunk) < 100:
                break
            page += 1
            if page > 100:
                raise GateError(f"too many job pages for run {run_id}")
        counts = Counter(job.get("name") for job in jobs)
        for name in [verifier, *expected]:
            matches = [job for job in jobs if job.get("name") == name]
            if (
                counts[name] != 1
                or matches[0].get("status") != "completed"
                or matches[0].get("conclusion") != "success"
            ):
                raise GateError(
                    f"{filename}: required job {name!r} missing, duplicated, skipped, or failed"
                )
        print(
            f"{filename}: {len(expected)} mandatory jobs and exact candidate verifier passed (run {run_id})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument(
        "--run-ids-json", required=True, help="JSON object: workflow filename to run ID"
    )
    args = parser.parse_args()
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GH_TOKEN"]
        run_ids = json.loads(args.run_ids_json)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        check(
            candidate_sha=args.candidate_sha,
            run_ids=run_ids,
            manifest=manifest,
            get=lambda path: api_get(path, repo=repo, token=token),
        )
    except (GateError, KeyError, TypeError, ValueError, urllib.error.URLError) as exc:
        print(f"::error::release full CI gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
