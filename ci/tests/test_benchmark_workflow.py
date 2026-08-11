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
                    {
                        "name": "validate site",
                        "run": 'python ci/benchmark_report.py validate-site --site-dir "$RUNNER_TEMP/site"',
                    },
                    {"name": "compute publication eligibility", "run": "echo eligible"},
                    {
                        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4880f43607fa02",
                        "name": "upload site artifact",
                        "with": {
                            "name": "x",
                            "path": "${{ runner.temp }}/site/",
                            "include-hidden-files": True,
                            "if-no-files-found": "error",
                        },
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
                        "name": "push benchmark-stats branch",
                        "run": 'python ci/benchmark_report.py prepare-branch --worktree "$WORKTREE" --site-dir "$GITHUB_WORKSPACE/site-temp"\ngit commit -m generated\npython "$GITHUB_WORKSPACE/ci/benchmark_report.py" validate-revision --repository "$WORKTREE" --revision HEAD --site-dir "$GITHUB_WORKSPACE/site-temp"\ngit push origin HEAD:ref --force-with-lease=ref:sha123',
                    }
                ],
            },
            "package-pages": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "uses": "actions/upload-pages-artifact@v4.0.0",
                        "with": {"path": "_site/", "include-hidden-files": True},
                    }
                ],
            },
            "deploy-pages": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"pages": "write", "id-token": "write"},
                "environment": {"name": "github-pages"},
                "outputs": {"page_url": "${{ steps.deployment.outputs.page_url }}"},
                "steps": [
                    {"uses": "actions/deploy-pages@c9ba3c5b5fca82fcf21936bd1efba49f2b8f40a0"}
                ],
            },
            "publication-audit": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "needs": ["publish-branch", "deploy-pages"],
                "steps": [
                    {
                        "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                        "with": {"persist-credentials": False},
                    },
                    {
                        "name": "audit publication",
                        "run": 'git fetch origin refs/heads/benchmark-stats --depth=1\npython ci/benchmark_report.py validate-revision --repository . --revision FETCH_HEAD --site-dir audit/site/\nPAGES_URL="${{ needs.deploy-pages.outputs.page_url }}"\npython ci/benchmark_report.py audit-pages --site-dir audit/site/ --page-url "$PAGES_URL"',
                    },
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

    def test_site_artifact_requires_hidden_files_and_fail_closed_upload(self) -> None:
        bad = _phase5_workflow()
        upload = bad["jobs"]["build-and-measure"]["steps"][4]["with"]
        del upload["include-hidden-files"]
        with self.assertRaises(PolicyError):
            check(bad)

        bad = _phase5_workflow()
        steps = bad["jobs"]["build-and-measure"]["steps"]
        upload = steps.pop(4)
        steps.insert(2, upload)
        with self.assertRaises(PolicyError):
            check(bad)

        bad = _phase5_workflow()
        upload = bad["jobs"]["build-and-measure"]["steps"][4]["with"]
        upload["if-no-files-found"] = "warn"
        with self.assertRaises(PolicyError):
            check(bad)

        bad = _phase5_workflow()
        upload = bad["jobs"]["build-and-measure"]["steps"][4]["with"]
        upload["path"] = "${{ runner.temp }}/"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_pages_artifact_requires_hidden_files(self) -> None:
        bad = _phase5_workflow()
        upload = bad["jobs"]["package-pages"]["steps"][0]["with"]
        del upload["include-hidden-files"]
        with self.assertRaises(PolicyError):
            check(bad)

    def test_unsafe_dotglob_cleanup_is_rejected(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["publish-branch"]["steps"][0]["run"] += "\nshopt -s dotglob\nrm -rf ./*"
        with self.assertRaises(PolicyError):
            check(bad)

    def test_publish_requires_exact_prepare_and_revision_audit_order(self) -> None:
        bad = _phase5_workflow()
        run = bad["jobs"]["publish-branch"]["steps"][0]["run"]
        bad["jobs"]["publish-branch"]["steps"][0]["run"] = run.replace(
            '--worktree "$WORKTREE"', "--worktree wrong"
        )
        with self.assertRaises(PolicyError):
            check(bad)

        bad = _phase5_workflow()
        run = bad["jobs"]["publish-branch"]["steps"][0]["run"]
        lines = run.splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        bad["jobs"]["publish-branch"]["steps"][0]["run"] = "\n".join(lines)
        with self.assertRaises(PolicyError):
            check(bad)

    def test_publication_audit_must_validate_the_remote_revision(self) -> None:
        bad = _phase5_workflow()
        bad["jobs"]["publication-audit"]["steps"][1]["run"] = "echo not-an-audit"
        with self.assertRaises(PolicyError):
            check(bad)

        bad = _phase5_workflow()
        run = bad["jobs"]["publication-audit"]["steps"][1]["run"]
        bad["jobs"]["publication-audit"]["steps"][1]["run"] = run.replace(
            "--revision FETCH_HEAD", "--revision HEAD"
        )
        with self.assertRaises(PolicyError):
            check(bad)

    def test_publication_audit_requires_deployment_output_and_needs(self) -> None:
        bad = _phase5_workflow()
        del bad["jobs"]["deploy-pages"]["outputs"]
        with self.assertRaises(PolicyError):
            check(bad)

        bad = _phase5_workflow()
        bad["jobs"]["publication-audit"]["needs"] = ["publish-branch"]
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
        del steps[3]  # remove eligibility step
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
