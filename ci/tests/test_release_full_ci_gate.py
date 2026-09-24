"""Release proof must reject a green run that omits a required cell."""

import json
import unittest
from pathlib import Path
from typing import Any, cast

import yaml
from ci.release_full_ci_gate import GateError, check

SHA = "a" * 40
MANIFEST = cast(
    dict[str, Any],
    json.loads(
        (Path(__file__).resolve().parents[1] / "release_full_ci_manifest.v1.json").read_text()
    ),
)
RUN_IDS: dict[str, int] = {name: index + 10 for index, name in enumerate(MANIFEST["workflows"])}
WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def expanded_job_names(filename: str) -> list[str]:
    """Expand required jobs so changing a workflow matrix cannot outrun the manifest."""
    jobs = yaml.safe_load((WORKFLOWS / filename).read_text())["jobs"]
    optional = {
        "macos-bundles.yml": {
            "decide",
            "run-macos-x64-selective",
            "run-macos-x64-recovery",
        }
    }.get(filename, set())
    names: list[str] = []
    for job_id, job in jobs.items():
        if job_id == "resolve-candidate" or job_id in optional:
            continue
        template = job.get("name", job_id)
        matrix = job.get("strategy", {}).get("matrix")
        if not matrix:
            names.append(template)
            continue
        rows = matrix.get("include")
        if rows is None:
            key, values = next(iter(matrix.items()))
            rows = [{key: value} for value in values]
        for row in rows:
            if "${{ matrix." in template:
                rendered = template
                for key, value in row.items():
                    rendered = rendered.replace(f"${{{{ matrix.{key} }}}}", str(value))
                names.append(rendered)
            else:
                names.append(f"{template} ({', '.join(str(value) for value in row.values())})")
    return names


def evidence() -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    for filename, run_id in RUN_IDS.items():
        stem = filename.removesuffix(".yml")
        responses[f"actions/runs/{run_id}"] = {
            "workflow_id": run_id,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "display_title": f"{stem}/full/{SHA}",
            "status": "completed",
            "conclusion": "success",
        }
        responses[f"actions/workflows/{run_id}"] = {"path": f".github/workflows/{filename}"}
        spec = MANIFEST["workflows"][filename]
        names = [spec["verifier"].format(sha=SHA), *spec["jobs"]]
        responses[f"actions/runs/{run_id}/jobs?per_page=100&page=1"] = {
            "jobs": [
                {"name": name, "status": "completed", "conclusion": "success"} for name in names
            ]
        }
    return responses


def verify(
    responses: dict[str, dict[str, Any]], *, run_ids: dict[str, int] = RUN_IDS, sha: str = SHA
) -> None:
    check(
        candidate_sha=sha,
        run_ids=run_ids,
        manifest=MANIFEST,
        get=responses.__getitem__,
    )


class FullCiGateTests(unittest.TestCase):
    def test_manifest_matches_every_required_expanded_workflow_job(self):
        for filename, spec in MANIFEST["workflows"].items():
            with self.subTest(workflow=filename):
                self.assertCountEqual(spec["jobs"], expanded_job_names(filename))

    def test_complete_evidence(self):
        verify(evidence())

    def test_missing_run_id(self):
        ids = dict(RUN_IDS)
        ids.pop("cross.yml")
        with self.assertRaises(GateError):
            verify(evidence(), run_ids=ids)

    def test_duplicate_run_id(self):
        ids = dict(RUN_IDS)
        ids["cross.yml"] = ids["rust-native.yml"]
        with self.assertRaises(GateError):
            verify(evidence(), run_ids=ids)

    def test_wrong_workflow_path_and_failed_run(self):
        data = evidence()
        data["actions/workflows/10"]["path"] = ".github/workflows/other.yml"
        with self.assertRaises(GateError):
            verify(data)
        data = evidence()
        data["actions/runs/10"]["conclusion"] = "failure"
        with self.assertRaises(GateError):
            verify(data)

    def test_untrusted_source_and_candidate(self):
        for field, value in [
            ("head_branch", "feature"),
            ("event", "pull_request"),
            ("display_title", "macos-bundles/full/" + "b" * 40),
        ]:
            with self.subTest(field=field):
                data = evidence()
                data["actions/runs/10"][field] = value
                with self.assertRaises(GateError):
                    verify(data)

    def test_job_failure_modes(self):
        job_path = "actions/runs/10/jobs?per_page=100&page=1"
        for mode in ["missing", "duplicate", "skipped", "neutral", "failure", "in_progress"]:
            with self.subTest(mode=mode):
                data = evidence()
                jobs = data[job_path]["jobs"]
                if mode == "missing":
                    jobs.pop()
                elif mode == "duplicate":
                    jobs.append(dict(jobs[-1]))
                elif mode == "in_progress":
                    jobs[-1]["status"] = "in_progress"
                else:
                    jobs[-1]["conclusion"] = mode
                with self.assertRaises(GateError):
                    verify(data)

    def test_verifier_is_candidate_bound(self):
        data = evidence()
        jobs = data["actions/runs/11/jobs?per_page=100&page=1"]["jobs"]
        jobs[0]["name"] = "resolve-candidate (exact SHA " + "b" * 40 + ")"
        with self.assertRaises(GateError):
            verify(data)
        data = evidence()
        data["actions/runs/11/jobs?per_page=100&page=1"]["jobs"][0]["conclusion"] = "skipped"
        with self.assertRaises(GateError):
            verify(data)

    def test_informational_row_is_mandatory_on_full_dispatch(self):
        data = evidence()
        job_path = "actions/runs/13/jobs?per_page=100&page=1"
        jobs = data[job_path]["jobs"]
        jobs[:] = [job for job in jobs if job["name"] != "test (x86_64-pc-windows-gnu)"]
        with self.assertRaises(GateError):
            verify(data)

    def test_pagination(self):
        data = evidence()
        path = "actions/runs/10/jobs?per_page=100&page=1"
        required = data[path]["jobs"]
        data[path] = {
            "jobs": required[:-1]
            + [
                {"name": f"unrelated-{index}", "status": "completed", "conclusion": "success"}
                for index in range(100 - len(required) + 1)
            ]
        }
        data["actions/runs/10/jobs?per_page=100&page=2"] = {"jobs": [required[-1]]}
        verify(data)


if __name__ == "__main__":
    unittest.main()
