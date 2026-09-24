"""Keep the full release dispatch bound to one merged source revision."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REQUIRED = ("c-unit", "windows-bundles", "cross", "rust-native")
VERIFIED_SHA = "${{ needs.resolve-candidate.outputs.sha }}"


def test_full_dispatch_checks_out_one_merged_sha_and_preserves_pr_merge_checkout() -> None:
    for workflow_name in REQUIRED:
        workflow = yaml.safe_load((WORKFLOWS / f"{workflow_name}.yml").read_text())
        # PyYAML's YAML 1.1 loader treats the unquoted Actions key `on` as True.
        events = workflow.get("on", workflow.get(True))
        assert events["workflow_dispatch"]["inputs"]["candidate_sha"]["required"] is True
        resolver = workflow["jobs"]["resolve-candidate"]
        resolver_checkout = resolver["steps"][0]["with"]
        resolver_script = resolver["steps"][1]["run"]

        assert workflow["run-name"].startswith(f"{workflow_name}/")
        assert "inputs.candidate_sha || github.sha" in workflow["run-name"]
        assert resolver["name"] == "resolve-candidate (exact SHA ${{ inputs.candidate_sha }})"
        assert resolver_checkout["ref"] == (
            "${{ github.event_name == 'workflow_dispatch' && inputs.candidate_sha || github.sha }}"
        )
        assert "refs/heads/main" in resolver_script
        assert 'git merge-base --is-ancestor "$sha" origin/main' in resolver_script
        assert 'sha="$EVENT_SHA"' in resolver_script
        assert "PR_SHA" not in resolver_script
        assert '[[ "$actual" == "$sha" ]]' in resolver_script

        for job_name, job in workflow["jobs"].items():
            if job_name == "resolve-candidate":
                continue
            needs = job.get("needs", [])
            if isinstance(needs, str):
                needs = [needs]
            assert "resolve-candidate" in needs, (workflow_name, job_name)
            for step in job.get("steps", []):
                if step.get("uses") == "actions/checkout@v4":
                    assert step["with"]["ref"] == VERIFIED_SHA, (workflow_name, job_name)


def test_full_dispatch_makes_windows_comparison_rows_required() -> None:
    cross = yaml.safe_load((WORKFLOWS / "cross.yml").read_text())["jobs"]
    native = yaml.safe_load((WORKFLOWS / "rust-native.yml").read_text())["jobs"]
    for job in (
        cross["build-win-gnu"],
        cross["test-binaries"],
        native["test"],
        native["test-win-gnu"],
    ):
        expression = job["continue-on-error"]
        assert "github.event_name != 'workflow_dispatch'" in expression
