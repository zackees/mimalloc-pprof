#!/usr/bin/env -S uv run --script
"""Measure GitHub Actions queue wait and execution time per runner label.

Issue #277 phase 0. Every push spawns one fresh VM per job and nothing cancels the
previous push's jobs, so a busy PR piles superseded runs onto the scarce macOS pool --
`cross.yml` run 33055011757 shows an Apple row that waited hours after its build had
already finished. `concurrency: cancel-in-progress` is supposed to fix that, and this
script is how we tell whether it did: run it before the change, run it after, compare.

Two numbers per job, both straight from the API:

    queue wait  = started_at   - created_at
    execution   = completed_at - started_at

grouped by the job's `labels` (what `runs-on:` resolved to), because the whole point is
that `macos-latest` behaves nothing like `ubuntu-latest`.

CAVEAT, and it matters for `cross.yml`: a job that `needs:` another is *created* when the
run is created, not when its dependency finishes. Its "queue wait" therefore includes the
time its dependencies spent building. `c-unit.yml` and `rust-native.yml` have no `needs:`
edges, so their rows are pure pool-wait; `cross.yml`'s `test (*)` rows are not. Rows are
grouped per workflow, never pooled across workflows, so the two kinds never end up in one
average -- but read a `cross.yml` row as "wait + upstream build", not as pool wait alone.

Usage:

    uv run ci/ci_queue_wait.py --limit 20 c-unit.yml cross.yml rust-native.yml
    uv run ci/ci_queue_wait.py --branch main --limit 20 c-unit.yml

Needs only `gh` (already authenticated in CI and on developer machines); no Python deps.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

REPO_DEFAULT = "zackees/mimalloc-pprof"

# Conclusions whose timestamps describe something other than "a job ran to completion".
# A cancelled job's completed_at is when the cancellation landed, and a skipped job never
# started at all; averaging either into an execution time is how a measurement lies.
_EXCLUDED_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral", "stale"})


# --------------------------------------------------------------------------------------
# JSON narrowing. The API responses are `object` as far as the type checker is concerned;
# every field is proven before use rather than indexed on faith (docs/ci-gates.md).
# --------------------------------------------------------------------------------------


def _as_object(value: object) -> dict[str, object] | None:
    # json.loads only ever produces str-keyed objects, so the cast restates a guarantee
    # the parser already made; it is not a claim about the field's contents, which every
    # accessor below re-checks.
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _as_array(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _as_str(obj: dict[str, object], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _as_int(obj: dict[str, object], key: str) -> int | None:
    value = obj.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_str_list(obj: dict[str, object], key: str) -> list[str]:
    return [item for item in _as_array(obj.get(key)) if isinstance(item, str)]


def _parse_ts(text: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (`2026-09-01T12:34:56Z`)."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobRecord:
    """One completed job with all three timestamps present."""

    workflow: str
    run_id: int
    job_id: int
    name: str
    labels: tuple[str, ...]
    created_at: datetime
    started_at: datetime
    completed_at: datetime

    @property
    def queue_seconds(self) -> float:
        return max(0.0, (self.started_at - self.created_at).total_seconds())

    @property
    def exec_seconds(self) -> float:
        return max(0.0, (self.completed_at - self.started_at).total_seconds())

    @property
    def runner(self) -> str:
        return ",".join(self.labels) if self.labels else "(unlabelled)"


@dataclass(frozen=True)
class GroupSummary:
    workflow: str
    runner: str
    count: int
    queue_p50: float
    queue_p90: float
    queue_max: float
    exec_p50: float
    exec_p90: float
    exec_max: float


def parse_jobs(workflow: str, payload: object) -> list[JobRecord]:
    """Convert one `/actions/runs/<id>/jobs` response into records.

    Jobs missing any of the three timestamps, or whose conclusion says they never really
    ran, are dropped -- silently by design, but `--verbose` reports the count.
    """
    root = _as_object(payload)
    if root is None:
        return []
    records: list[JobRecord] = []
    for raw in _as_array(root.get("jobs")):
        job = _as_object(raw)
        if job is None:
            continue
        if _as_str(job, "status") != "completed":
            continue
        conclusion = _as_str(job, "conclusion")
        if conclusion is None or conclusion in _EXCLUDED_CONCLUSIONS:
            continue
        created = _parse_ts(_as_str(job, "created_at"))
        started = _parse_ts(_as_str(job, "started_at"))
        completed = _parse_ts(_as_str(job, "completed_at"))
        if created is None or started is None or completed is None:
            continue
        run_id = _as_int(job, "run_id")
        job_id = _as_int(job, "id")
        name = _as_str(job, "name")
        if run_id is None or job_id is None or name is None:
            continue
        records.append(
            JobRecord(
                workflow=workflow,
                run_id=run_id,
                job_id=job_id,
                name=name,
                labels=tuple(_as_str_list(job, "labels")),
                created_at=created,
                started_at=started,
                completed_at=completed,
            )
        )
    return records


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile on an unsorted sequence. Empty input is 0.0.

    Nearest-rank (rather than interpolated) because these samples are few and the
    question is "how long did a real job actually wait", not a smoothed estimate.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]


def summarise(records: Sequence[JobRecord]) -> list[GroupSummary]:
    """Group by (workflow, runner labels) and reduce to percentiles."""
    buckets: dict[tuple[str, str], list[JobRecord]] = {}
    for record in records:
        buckets.setdefault((record.workflow, record.runner), []).append(record)
    summaries: list[GroupSummary] = []
    for (workflow, runner), group in buckets.items():
        queue = [job.queue_seconds for job in group]
        execs = [job.exec_seconds for job in group]
        summaries.append(
            GroupSummary(
                workflow=workflow,
                runner=runner,
                count=len(group),
                queue_p50=percentile(queue, 0.50),
                queue_p90=percentile(queue, 0.90),
                queue_max=max(queue),
                exec_p50=percentile(execs, 0.50),
                exec_p90=percentile(execs, 0.90),
                exec_max=max(execs),
            )
        )
    summaries.sort(key=lambda s: (s.workflow, s.runner))
    return summaries


def human_seconds(seconds: float) -> str:
    """Compact duration: `12s`, `4m30s`, `5h29m`."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def format_table(summaries: Sequence[GroupSummary]) -> str:
    """Render a fixed-width table. Markdown-pasteable inside a fenced block."""
    header = (
        "workflow",
        "runs-on labels",
        "n",
        "queue p50",
        "queue p90",
        "queue max",
        "exec p50",
        "exec p90",
        "exec max",
    )
    rows: list[tuple[str, ...]] = [header]
    for s in summaries:
        rows.append(
            (
                s.workflow,
                s.runner,
                str(s.count),
                human_seconds(s.queue_p50),
                human_seconds(s.queue_p90),
                human_seconds(s.queue_max),
                human_seconds(s.exec_p50),
                human_seconds(s.exec_p90),
                human_seconds(s.exec_max),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(rows[0])).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(header))))
    for row in rows[1:]:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


def gh_api(path: str) -> object:
    """One `gh api` GET. Raises RuntimeError with gh's own stderr on failure."""
    proc = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {path} failed ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def fetch_run_ids(repo: str, workflow: str, limit: int, branch: str | None) -> list[int]:
    query = f"per_page={min(limit, 100)}&status=completed"
    if branch:
        query += f"&branch={branch}"
    payload = gh_api(f"/repos/{repo}/actions/workflows/{workflow}/runs?{query}")
    root = _as_object(payload)
    if root is None:
        return []
    ids: list[int] = []
    for raw in _as_array(root.get("workflow_runs")):
        run = _as_object(raw)
        if run is None:
            continue
        run_id = _as_int(run, "id")
        if run_id is not None:
            ids.append(run_id)
    return ids[:limit]


def fetch_records(
    repo: str, workflow: str, limit: int, branch: str | None, verbose: bool
) -> list[JobRecord]:
    records: list[JobRecord] = []
    run_ids = fetch_run_ids(repo, workflow, limit, branch)
    if verbose:
        print(f"# {workflow}: {len(run_ids)} runs", file=sys.stderr)
    for run_id in run_ids:
        payload = gh_api(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        records.extend(parse_jobs(workflow, payload))
    return records


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("workflows", nargs="*", help="workflow file names, e.g. c-unit.yml")
    parser.add_argument("--repo", default=REPO_DEFAULT)
    parser.add_argument("--limit", type=int, default=20, help="runs per workflow (default 20)")
    parser.add_argument("--branch", default=None, help="restrict to one branch (default: all)")
    parser.add_argument(
        "--jobs-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="read a saved [{workflow, jobs: [...]}, ...] payload instead of calling gh",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    records: list[JobRecord] = []
    if args.jobs_json is not None:
        saved = _as_array(json.loads(Path(args.jobs_json).read_text(encoding="utf-8")))
        for raw in saved:
            entry = _as_object(raw)
            if entry is None:
                continue
            workflow = _as_str(entry, "workflow") or str(args.jobs_json.name)
            records.extend(parse_jobs(workflow, entry))
    else:
        if not args.workflows:
            parser.error("give at least one workflow file name (or --jobs-json)")
        for workflow in args.workflows:
            records.extend(
                fetch_records(
                    str(args.repo), str(workflow), int(args.limit), args.branch, bool(args.verbose)
                )
            )

    if not records:
        print("no completed jobs with usable timestamps", file=sys.stderr)
        return 1

    scope = f"branch {args.branch}" if args.branch else "all branches"
    print(f"# {len(records)} jobs, last {args.limit} runs per workflow, {scope}")
    print(format_table(summarise(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
