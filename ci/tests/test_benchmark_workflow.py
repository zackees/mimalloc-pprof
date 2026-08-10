"""Workflow policy checker tests — RED -> GREEN for the Phase 4 dry-run guard."""

# pyright: reportMissingTypeArgument=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportIndexIssue=false

import sys
import unittest
from pathlib import Path

# The production script is intentionally standalone, not an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from check_benchmark_workflow import PolicyError, check


def _valid_workflow() -> dict[str, object]:
    return {
        "name": "benchmark-stats",
        "on": {
            "workflow_dispatch": {
                "inputs": {
                    "mode": {
                        "type": "choice",
                        "options": ["full", "smoke"],
                        "default": "full",
                    },
                    "run_seed": {"type": "string", "required": False},
                    "blocks": {"type": "number", "default": 15, "required": False},
                }
            }
        },
        "concurrency": {
            "group": "benchmark-stats-production",
            "cancel-in-progress": False,
        },
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
                    },
                ],
            },
        },
    }


class TestWorkflowPolicyValidator(unittest.TestCase):
    def test_valid_fixture_passes(self) -> None:
        check(_valid_workflow())

    def test_contents_write_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["permissions"]["contents"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_schedule_trigger_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["on"]["schedule"] = [{"cron": "0 0 * * *"}]
        with self.assertRaises(PolicyError):
            check(bad)

    def test_tag_only_action_ref_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["jobs"]["build-and-measure"]["steps"].insert(0, {"uses": "actions/checkout@v4"})
        with self.assertRaises(PolicyError):
            check(bad)

    def test_missing_artifact_retention_is_rejected(self) -> None:
        bad = _valid_workflow()
        del bad["jobs"]["build-and-measure"]["steps"][2]["retention-days"]
        with self.assertRaises(PolicyError):
            check(bad)

    def test_pages_write_permission_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["permissions"]["pages"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_cancel_in_progress_true_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["concurrency"]["cancel-in-progress"] = True
        with self.assertRaises(PolicyError):
            check(bad)

    def test_hidden_git_push_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["jobs"]["build-and-measure"]["steps"].insert(
            1, {"name": "evil", "run": "git push origin benchmark-stats"}
        )
        with self.assertRaises(PolicyError):
            check(bad)

    def test_publish_input_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["on"]["workflow_dispatch"]["inputs"]["publish"] = {
            "type": "boolean",
            "default": False,
        }
        with self.assertRaises(PolicyError):
            check(bad)

    def test_id_token_permission_is_rejected(self) -> None:
        bad = _valid_workflow()
        bad["permissions"]["id-token"] = "write"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_checkout_without_persist_credentials_rejected(self) -> None:
        bad = _valid_workflow()
        bad["jobs"]["build-and-measure"]["steps"][0] = {
            "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        }
        with self.assertRaises(PolicyError):
            check(bad)

    def test_wrong_concurrency_group_rejected(self) -> None:
        bad = _valid_workflow()
        bad["concurrency"]["group"] = "something-else"
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
