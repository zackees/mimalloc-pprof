"""Workflow policy checker tests — RED -> GREEN for Phases 4-5."""

# pyright: reportMissingTypeArgument=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportIndexIssue=false

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from check_benchmark_workflow import PolicyError, check


def _phase5_workflow() -> dict[str, object]:
    return {
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
                        "run": "git push origin HEAD:ref --force-with-lease=ref:sha123",
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


class TestPhase5Policy(unittest.TestCase):
    def test_valid_phase5_fixture_passes(self) -> None:
        check(_phase5_workflow())

    def test_workflow_level_write_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["permissions"]["contents"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_workflow_level_pages_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["permissions"]["pages"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_workflow_level_id_token_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["permissions"]["id-token"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_tag_only_action_ref_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["build-and-measure"]["steps"].insert(0, {"uses": "actions/checkout@v4"})
        with self.assertRaises(PolicyError):
            check(bad)

    def test_cancel_in_progress_true_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["concurrency"]["cancel-in-progress"] = True
        with self.assertRaises(PolicyError):
            check(bad)

    def test_write_permission_on_measure_job_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["build-and-measure"]["permissions"]["contents"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_hidden_git_push_in_measure_job_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["build-and-measure"]["steps"].insert(
            1, {"name": "push", "run": "git push origin foo"}
        )
        with self.assertRaises(PolicyError):
            check(bad)

    def test_unconditional_force_in_publish_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["publish-branch"]["steps"][0]["run"] = "git push --force origin HEAD:ref"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_deploy_pages_outside_deploy_job_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["publish-branch"]["steps"].append({"uses": "actions/deploy-pages@v4.0.5"})
        with self.assertRaises(PolicyError):
            check(bad)

    def test_missing_deploy_environment_rejected(self) -> None:
        bad = _phase5_workflow()
        del bad["jobs"]["deploy-pages"]["environment"]
        with self.assertRaises(PolicyError):
            check(bad)

    def test_missing_eligibility_step_rejected(self) -> None:
        bad = _phase5_workflow()
        steps = bad["jobs"]["build-and-measure"]["steps"]
        del steps[2]  # remove eligibility step
        with self.assertRaises(PolicyError):
            check(bad)

    def test_real_workflow_passes(self) -> None:
        path = Path(".github/workflows/benchmark-stats.yml")
        if path.is_file():
            with path.open(encoding="utf-8") as source:
                workflow = yaml.safe_load(source)
            check(workflow)
        else:
            self.skipTest("workflow file not present")


if __name__ == "__main__":
    unittest.main()
