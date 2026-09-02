"""Unit tests for ci/ci_queue_wait.py (issue #277 phase 0).

The script decides whether `concurrency: cancel-in-progress` actually bought anything, so
its arithmetic has to be checkable without hitting the API: everything below drives the
pure functions from a fixture in the real `/actions/runs/<id>/jobs` shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast

import ci_queue_wait

FIXTURE = Path(__file__).parent / "fixtures" / "queue_wait" / "jobs.json"
SCRIPT = Path(__file__).resolve().parents[1] / "ci_queue_wait.py"


def _fixture_payload() -> object:
    payloads = cast("list[object]", json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert len(payloads) == 1
    return payloads[0]


class ParseJobsTest(unittest.TestCase):
    def test_keeps_only_completed_jobs_with_all_three_timestamps(self) -> None:
        records = ci_queue_wait.parse_jobs("cross.yml", _fixture_payload())
        # 8 jobs in the fixture; dropped: cancelled (6), skipped/null ts (7), in_progress (8).
        self.assertEqual([r.job_id for r in records], [1, 2, 3, 4, 5])

    def test_failed_jobs_are_kept(self) -> None:
        records = ci_queue_wait.parse_jobs("cross.yml", _fixture_payload())
        failed = [r for r in records if r.job_id == 3]
        self.assertEqual(len(failed), 1, "a red job still waited in the queue and still ran")

    def test_queue_and_exec_split(self) -> None:
        records = {r.job_id: r for r in ci_queue_wait.parse_jobs("cross.yml", _fixture_payload())}
        self.assertEqual(records[4].queue_seconds, 5 * 3600 + 26 * 60)
        self.assertEqual(records[4].exec_seconds, 40.0)
        self.assertEqual(records[5].queue_seconds, 4.0)

    def test_runner_label_grouping_key(self) -> None:
        records = {r.job_id: r for r in ci_queue_wait.parse_jobs("cross.yml", _fixture_payload())}
        self.assertEqual(records[1].runner, "macos-latest")
        self.assertEqual(records[5].runner, "ubuntu-latest")

    def test_multi_label_runs_on_joins_labels(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": 11,
                    "run_id": 1,
                    "name": "j",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "2026-09-01T00:00:00Z",
                    "started_at": "2026-09-01T00:00:10Z",
                    "completed_at": "2026-09-01T00:00:20Z",
                    "labels": ["self-hosted", "macOS", "ARM64"],
                }
            ]
        }
        records = ci_queue_wait.parse_jobs("x.yml", payload)
        self.assertEqual(records[0].runner, "self-hosted,macOS,ARM64")

    def test_garbage_payloads_yield_no_records_rather_than_raising(self) -> None:
        payloads: tuple[object, ...] = (None, [], {}, {"jobs": "nope"}, {"jobs": [None, 7, "x"]})
        for payload in payloads:
            self.assertEqual(ci_queue_wait.parse_jobs("x.yml", payload), [])


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(ci_queue_wait.percentile(values, 0.50), 20.0)
        self.assertEqual(ci_queue_wait.percentile(values, 0.90), 40.0)
        self.assertEqual(ci_queue_wait.percentile(values, 0.25), 10.0)

    def test_single_sample_and_empty(self) -> None:
        self.assertEqual(ci_queue_wait.percentile([7.0], 0.9), 7.0)
        self.assertEqual(ci_queue_wait.percentile([], 0.5), 0.0)

    def test_unsorted_input(self) -> None:
        self.assertEqual(ci_queue_wait.percentile([40.0, 10.0, 30.0, 20.0], 0.5), 20.0)


class SummariseTest(unittest.TestCase):
    def test_groups_by_workflow_and_runner(self) -> None:
        records = ci_queue_wait.parse_jobs("cross.yml", _fixture_payload())
        summaries = {s.runner: s for s in ci_queue_wait.summarise(records)}
        self.assertEqual(sorted(summaries), ["macos-latest", "ubuntu-latest"])
        mac = summaries["macos-latest"]
        self.assertEqual(mac.count, 4)
        # queue waits 60, 120, 300, 19560 -> nearest-rank p50 = 120, p90 = 19560
        self.assertEqual(mac.queue_p50, 120.0)
        self.assertEqual(mac.queue_p90, 19560.0)
        self.assertEqual(mac.queue_max, 19560.0)
        self.assertEqual(mac.exec_p50, 20.0)
        self.assertEqual(mac.exec_max, 40.0)

    def test_same_runner_in_two_workflows_stays_separate(self) -> None:
        """cross.yml rows include upstream `needs:` build time; c-unit rows do not."""
        payload = _fixture_payload()
        records = ci_queue_wait.parse_jobs("cross.yml", payload) + ci_queue_wait.parse_jobs(
            "c-unit.yml", payload
        )
        summaries = ci_queue_wait.summarise(records)
        self.assertEqual(
            [(s.workflow, s.runner) for s in summaries],
            [
                ("c-unit.yml", "macos-latest"),
                ("c-unit.yml", "ubuntu-latest"),
                ("cross.yml", "macos-latest"),
                ("cross.yml", "ubuntu-latest"),
            ],
        )


class FormattingTest(unittest.TestCase):
    def test_human_seconds(self) -> None:
        self.assertEqual(ci_queue_wait.human_seconds(0), "0s")
        self.assertEqual(ci_queue_wait.human_seconds(59.4), "59s")
        self.assertEqual(ci_queue_wait.human_seconds(60), "1m00s")
        self.assertEqual(ci_queue_wait.human_seconds(150), "2m30s")
        self.assertEqual(ci_queue_wait.human_seconds(19560), "5h26m")

    def test_table_has_one_row_per_group(self) -> None:
        records = ci_queue_wait.parse_jobs("cross.yml", _fixture_payload())
        table = ci_queue_wait.format_table(ci_queue_wait.summarise(records))
        lines = table.split("\n")
        self.assertIn("runs-on labels", lines[0])
        self.assertEqual(len(lines), 4, "header + rule + two groups")
        self.assertIn("5h26m", table)


class CliTest(unittest.TestCase):
    def test_jobs_json_mode_needs_no_network(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--jobs-json", str(FIXTURE)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cross.yml", proc.stdout)
        self.assertIn("macos-latest", proc.stdout)
        self.assertIn("5 jobs", proc.stdout)

    def test_no_workflows_and_no_fixture_is_a_usage_error(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
