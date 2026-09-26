"""Structural gate for `.github/workflows/auto-release.yml` (#277 phase E).

`auto-release.yml` runs only on `workflow_dispatch`, so a wiring mistake is invisible until someone cuts a release and
discovers the assets are missing or the publish job never ran. Issue #55 is exactly that
failure once already. These tests are the substitute for a CI run: they assert the shape
the workflow has to have, from the YAML itself.

What they check:
  * build and publication stay on Linux; the release-local test gate runs on native hosts;
  * `release` waits for every job that produces one of its assets;
  * shipped artifacts are downloaded by `release`; test bundles by the native gate;
  * the release's `files:` list, the rename step and the build matrix all name the same
    set of assets -- three places that have to agree and no compiler to check them;
  * every cross lane names a toolchain file that exists.
"""

from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "auto-release.yml"
MACOS_WORKFLOW = ROOT / ".github" / "workflows" / "macos-bundles.yml"

# The four cross lanes, and the archive extension each one ships. Spelled out here rather
# than read from the matrix so that a lane silently disappearing from the matrix fails.
EXPECTED_LANES: dict[str, tuple[str, str]] = {
    "macos-arm64": ("aarch64-apple-darwin", "tar.gz"),
    "macos-x86_64": ("x86_64-apple-darwin", "tar.gz"),
    "windows-x64-gnu": ("x86_64-pc-windows-gnu", "zip"),
    "windows-x64-msvc": ("x86_64-pc-windows-msvc", "zip"),
}


def load() -> dict[str, Any]:
    with WORKFLOW.open(encoding="utf-8") as handle:
        return cast(dict[str, Any], yaml.safe_load(handle))


def job_run_text(job: dict[str, Any]) -> str:
    return "\n".join(
        step["run"] for step in job.get("steps", []) if isinstance(step.get("run"), str)
    )


class AutoReleaseStructureTests(unittest.TestCase):
    doc: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = load()

    def jobs(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.doc["jobs"])

    def test_build_and_publication_jobs_run_on_linux(self) -> None:
        for name, job in self.jobs().items():
            if name in ("smoke-shipped-assets", "test-shipped-assets"):
                continue
            runs_on = job.get("runs-on")
            self.assertEqual(
                runs_on,
                "ubuntu-latest",
                f"auto-release job {name!r} runs on {runs_on!r}; every release asset is "
                "cross-built on Linux (#277 phase E)",
            )
            matrix = cast(dict[str, Any], job.get("strategy", {})).get("matrix", {})
            keys = set(matrix) | {k for row in matrix.get("include", []) for k in row}
            self.assertNotIn(
                "os",
                keys,
                f"auto-release job {name!r} still has an `os` matrix dimension; the "
                "platform is chosen by the cross toolchain now, not by the runner",
            )

    def test_publication_waits_for_archive_collation_and_both_native_gates(self) -> None:
        release = self.jobs()["release"]
        preflight = self.jobs()["preflight-assets"]
        smoke = self.jobs()["smoke-shipped-assets"]
        self.assertEqual(
            sorted(preflight["needs"]),
            ["build-and-package", "build-binaries", "test-shipped-assets"],
        )
        self.assertEqual(smoke["needs"], ["preflight-assets"])
        self.assertEqual(sorted(release["needs"]), ["preflight-assets", "smoke-shipped-assets"])

    def test_release_test_gate_uses_all_native_hosts_and_exact_bytes(self) -> None:
        gate = self.jobs()["test-shipped-assets"]
        self.assertEqual(gate["needs"], ["build-binaries"])
        self.assertEqual(gate["runs-on"], "${{ matrix.runner }}")
        self.assertEqual(
            {row["asset"]: row["runner"] for row in gate["strategy"]["matrix"]["include"]},
            {
                "macos-arm64": "macos-15",
                "macos-x86_64": "macos-15-intel",
                "windows-x64-gnu": "windows-latest",
                "windows-x64-msvc": "windows-latest",
            },
        )
        script = job_run_text(gate)
        self.assertIn("ci/verify_release_test_bundle.py", script)
        self.assertIn("ci/run_test_bundle.py", script)
        self.assertIn("--exclude test-osx-zone-introspect-remote", script)
        self.assertIn("--only test-osx-zone-introspect-remote", script)
        self.assertIn('sudo -n "$(command -v python3)"', script)
        downloads = [
            step["with"]["name"]
            for step in gate["steps"]
            if str(step.get("uses", "")).startswith("actions/download-artifact")
        ]
        self.assertEqual(
            downloads,
            ["release-binaries-${{ matrix.asset }}", "release-test-bundle-${{ matrix.asset }}"],
        )

    def test_release_outcome_reports_every_build_job(self) -> None:
        outcome = self.jobs()["release-outcome"]
        self.assertEqual(
            sorted(outcome["needs"]), ["build-and-package", "build-binaries", "release"]
        )
        text = job_run_text(outcome)
        for job in ("build-and-package", "build-binaries", "release"):
            self.assertIn(f"needs.{job}.result", text)
        self.assertEqual(outcome["if"], "always() && inputs.dry_run == false")

    def test_build_binaries_matrix_is_the_four_cross_lanes(self) -> None:
        rows = self.jobs()["build-binaries"]["strategy"]["matrix"]["include"]
        seen = {row["asset"]: (row["triple"], row["archive"]) for row in rows}
        self.assertEqual(seen, EXPECTED_LANES)

    def test_every_lane_names_an_existing_toolchain_file(self) -> None:
        for triple, _ in EXPECTED_LANES.values():
            toolchain = ROOT / "cmake" / "toolchains" / f"soldr-{triple}.cmake"
            self.assertTrue(toolchain.is_file(), f"missing {toolchain}")

    def test_archive_inputs_are_collated_then_exact_result_reaches_release(self) -> None:
        uploaded: set[str] = set()
        for name, job in self.jobs().items():
            if name in ("release", "smoke-shipped-assets"):
                continue
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/upload-artifact"):
                    uploaded.add(str(step["with"]["name"]))
        downloaded: set[str] = set()
        patterns: list[str] = []
        for step in self.jobs()["preflight-assets"]["steps"]:
            if str(step.get("uses", "")).startswith("actions/download-artifact"):
                with_ = cast(dict[str, Any], step["with"])
                if "name" in with_:
                    downloaded.add(str(with_["name"]))
                if "pattern" in with_:
                    patterns.append(str(with_["pattern"]))
        for artifact in uploaded:
            if artifact.startswith(
                ("release-preflight-", "release-test-bundle-", "release-crate-")
            ):
                continue
            covered = artifact in downloaded or any(
                artifact.startswith(pattern.rstrip("*")) for pattern in patterns
            )
            self.assertTrue(
                covered,
                f"artifact {artifact!r} is uploaded but never downloaded by `preflight-assets`; it "
                "would not reach the GitHub Release",
            )
        release_downloads = [
            step["with"]["name"]
            for step in self.jobs()["release"]["steps"]
            if str(step.get("uses", "")).startswith("actions/download-artifact")
        ]
        self.assertEqual(
            release_downloads,
            [
                "release-preflight-${{ inputs.candidate_sha }}",
                "release-crate-${{ inputs.candidate_sha }}",
            ],
        )
        self.assertIn("ci/release.py verify-artifacts", job_run_text(self.jobs()["release"]))

    def test_every_attempt_smokes_shipped_archives_before_publication(self) -> None:
        smoke = self.jobs()["smoke-shipped-assets"]
        self.assertNotIn("if", smoke)
        self.assertEqual(smoke["needs"], ["preflight-assets"])
        self.assertIn("smoke-shipped-assets", self.jobs()["release"]["needs"])
        self.assertEqual(
            {row["asset"]: row["runner"] for row in smoke["strategy"]["matrix"]["include"]},
            {
                "macos-arm64": "macos-15",
                "macos-x86_64": "macos-15-intel",
                "windows-x64-gnu": "windows-latest",
                "windows-x64-msvc": "windows-latest",
            },
        )
        steps = smoke["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ inputs.candidate_sha }}")
        self.assertEqual(steps[1]["with"]["python-version"], "3.12")
        self.assertEqual(steps[2]["with"]["name"], "release-preflight-${{ inputs.candidate_sha }}")
        self.assertIn('"$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', steps[3]["run"])
        self.assertIn("ci/smoke_release_archive.py", steps[3]["run"])
        self.assertIn('--asset "$ASSET"', steps[3]["run"])

    def test_recorded_dry_success_waits_for_native_smoke(self) -> None:
        record = self.jobs()["record-attempt-outcome"]
        self.assertIn("smoke-shipped-assets", record["needs"])
        self.assertIn("test-shipped-assets", record["needs"])
        step = next(step for step in record["steps"] if "record-outcome" in step.get("run", ""))
        self.assertEqual(step["env"]["SMOKE"], "${{ needs.smoke-shipped-assets.result }}")
        self.assertEqual(step["env"]["TEST_SHIPPED"], "${{ needs.test-shipped-assets.result }}")
        script = step["run"]
        self.assertIn('"$SMOKE" == success', script)
        self.assertIn('"$TEST_SHIPPED" == success', script)
        self.assertIn("smoke=$SMOKE", script)
        self.assertIn("state=dry-passed", script)
        self.assertIn("state=real-passed", script)
        self.assertEqual(
            self.jobs()["release-outcome"]["needs"],
            ["build-and-package", "build-binaries", "release"],
        )

    def test_release_files_rename_and_matrix_agree(self) -> None:
        from ci import release as release_script

        files = release_script.ASSET_TEMPLATES
        # The architecture-independent amalgamation ZIP plus one archive per cross lane.
        self.assertEqual(len(files), 1 + len(EXPECTED_LANES), files)
        rename_text = job_run_text(self.jobs()["preflight-assets"])
        for asset, (_, ext) in EXPECTED_LANES.items():
            attached = [f for f in files if f"mimalloc-pprof-{asset}-" in f]
            self.assertEqual(
                len(attached), 1, f"{asset} is attached {len(attached)} times: {files}"
            )
            self.assertTrue(
                attached[0].endswith(f".{ext}"),
                f"{asset} is attached as {attached[0]}, expected a .{ext}",
            )
            self.assertIn(
                asset,
                rename_text,
                f"the rename/verify step does not mention {asset}; a missing lane would "
                "reach action-gh-release as a shorter file list",
            )
        self.assertTrue(any("mimalloc-pprof-c-" in f for f in files))

    def test_dry_run_has_no_external_write_steps(self) -> None:
        workflow = self.doc
        # PyYAML 1.1 treats `on` as boolean, so inspect both possible key types.
        trigger_keys = cast(dict[object, Any], workflow)
        triggers = cast(dict[str, Any], trigger_keys.get("on", trigger_keys.get(True)))
        self.assertTrue(triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"])
        release = self.jobs()["release"]
        steps = release["steps"]
        for step in steps:
            source = " ".join(str(step.get(key, "")) for key in ("name", "uses", "run"))
            if any(
                token in source
                for token in (
                    "crates-io-auth-action",
                    "ci.release_live --real",
                )
            ):
                self.assertEqual(step.get("if"), "env.IS_DRY_RUN != 'true'", source)
        self.assertIn(
            "soldr cargo publish --dry-run -p mimalloc-pprof --locked",
            job_run_text(release),
        )
        self.assertNotIn("softprops/action-gh-release", str(steps))
        self.assertNotIn("soldr cargo publish -p mimalloc-pprof --locked", str(steps))

    def test_tag_push_cannot_publish_and_real_run_needs_full_sha_evidence(self) -> None:
        trigger_keys = cast(dict[object, Any], self.doc)
        triggers = cast(dict[str, Any], trigger_keys.get("on", trigger_keys.get(True)))
        self.assertNotIn("push", triggers)
        inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertIn("candidate_sha", inputs)
        self.assertIn("full_run_id", inputs)
        release = self.jobs()["release"]
        self.assertNotIn("GITHUB_REF_NAME", job_run_text(release))
        gate = next(
            step
            for step in release["steps"]
            if step.get("name")
            == "Require exact-SHA full native macOS evidence before any release write"
        )
        self.assertEqual(gate["if"], "env.IS_DRY_RUN != 'true'")
        script = gate["run"]
        for required in (
            '"$(git rev-parse HEAD)" = "$CANDIDATE_SHA"',
            'git merge-base --is-ancestor "$CANDIDATE_SHA" origin/main',
            ".github/workflows/macos-bundles.yml",
            ".display_title",
            "macos-bundles/full/$CANDIDATE_SHA",
            "workflow_dispatch",
            ".head_branch",
            "resolve-candidate (exact SHA)",
            ".conclusion",
            ".status",
            "run-macos-native-full ($arch)",
            "gh api --paginate",
            "--jq '.jobs[] | {name, status, conclusion}'",
            "jq -s",
        ):
            self.assertIn(required, script)
        for job_name in ("build-and-package", "build-binaries", "release"):
            job = self.jobs()[job_name]
            checkout = next(
                step
                for step in job["steps"]
                if str(step.get("uses", "")).startswith("actions/checkout")
            )
            self.assertEqual(checkout["with"]["ref"], "${{ inputs.candidate_sha }}")
        release_checkout = next(
            step
            for step in release["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout")
        )
        self.assertEqual(release_checkout["with"]["fetch-depth"], 0)
        for job_name in ("build-and-package", "build-binaries"):
            self.assertIn(
                '"$(git rev-parse HEAD)" = "$CANDIDATE_SHA"',
                job_run_text(self.jobs()[job_name]),
            )
        self.assertIn(
            "commit:   $(git rev-parse HEAD)",
            job_run_text(self.jobs()["build-binaries"]),
        )
        first_write = next(
            i
            for i, step in enumerate(release["steps"])
            if "ci.release_live --real" in str(step.get("run", ""))
        )
        self.assertLess(release["steps"].index(gate), first_write)
        full_gate = next(
            step
            for step in release["steps"]
            if "ci/release_full_ci_gate.py" in str(step.get("run", ""))
        )
        self.assertLess(release["steps"].index(full_gate), first_write)
        preflight = next(
            step
            for step in release["steps"]
            if "ci.release_destinations" in str(step.get("run", ""))
        )
        self.assertLess(release["steps"].index(preflight), first_write)

    def test_full_evidence_rejects_wrong_candidate_and_missing_verifier(self) -> None:
        gate = next(
            step
            for step in self.jobs()["release"]["steps"]
            if step.get("name")
            == "Require exact-SHA full native macOS evidence before any release write"
        )
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        jobs = [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in (
                "resolve-candidate (exact SHA)",
                "run-macos-native-full (arm64)",
                "run-macos-native-full (x64)",
            )
        ]
        mock = """gh() {
          case "$*" in
            *'/jobs?'*) printf '%s\\n' "$MOCK_JOBS" ;;
            *'/workflows/'*) printf '%s\\n' '{"path":".github/workflows/macos-bundles.yml"}' ;;
            *'/runs/'*) printf '%s\\n' "$MOCK_RUN" ;;
            *) return 1 ;;
          esac
        }
        git() {
          if [ "$1" = merge-base ]; then
            [ "$MOCK_UNMERGED" != 1 ]
            return $?
          fi
          command git "$@"
        }
        """

        def run_gate(
            candidate: str,
            job_rows: list[dict[str, str]],
            branch: str = "main",
            merged: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            env = dict(os.environ)
            env.update(
                {
                    "CANDIDATE_SHA": candidate,
                    "FULL_RUN_ID": "123",
                    "GITHUB_REPOSITORY": "zackees/mimalloc-pprof",
                    "MOCK_UNMERGED": "0" if merged else "1",
                    "MOCK_RUN": json.dumps(
                        {
                            "workflow_id": 7,
                            "event": "workflow_dispatch",
                            "head_branch": branch,
                            "display_title": f"macos-bundles/full/{head}",
                            "conclusion": "success",
                        }
                    ),
                    "MOCK_JOBS": "\n".join(json.dumps(row) for row in job_rows),
                }
            )
            return subprocess.run(
                ["bash", "-c", mock + gate["run"]],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(run_gate(head, jobs).returncode, 0)
        self.assertNotEqual(run_gate("a" * 40, jobs).returncode, 0)
        self.assertNotEqual(run_gate(head, jobs[1:]).returncode, 0)
        self.assertNotEqual(run_gate(head, jobs, branch="feat/untrusted").returncode, 0)
        self.assertNotEqual(run_gate(head, jobs, merged=False).returncode, 0)

    def test_verifier_name_matches_full_workflow_display_name(self) -> None:
        with MACOS_WORKFLOW.open(encoding="utf-8") as handle:
            macos = yaml.safe_load(handle)
        verifier_name = macos["jobs"]["resolve-candidate"]["name"]
        gate = next(
            step
            for step in self.jobs()["release"]["steps"]
            if step.get("name")
            == "Require exact-SHA full native macOS evidence before any release write"
        )
        self.assertEqual(verifier_name, "resolve-candidate (exact SHA)")
        self.assertIn(f"verifier_name='{verifier_name}'", gate["run"])

    def test_the_cross_build_decision_is_documented_in_the_header(self) -> None:
        # #277 row E as first written said release assets "stay built by the platform's own
        # toolchain". Two owner decisions overrode that. The reversal has to be legible in
        # the file that implements it, not only in the issue thread.
        header = WORKFLOW.read_text(encoding="utf-8").split("on:", 1)[0]
        for phrase in ("CROSS-BUILT ON LINUX", "soldr", "Native Mac"):
            self.assertIn(phrase, header)


if __name__ == "__main__":
    unittest.main()
