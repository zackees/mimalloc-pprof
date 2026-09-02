"""Structural gate for `.github/workflows/auto-release.yml` (#277 phase E).

`auto-release.yml` does not run on pull requests -- it runs on `workflow_dispatch` and on
a `v*` tag push -- so a wiring mistake in it is invisible until someone cuts a release and
discovers the assets are missing or the publish job never ran. Issue #55 is exactly that
failure once already. These tests are the substitute for a CI run: they assert the shape
the workflow has to have, from the YAML itself.

What they check:
  * no non-Linux runner anywhere (the owner requirement that macOS never runs natively is
    enforced repository-wide by ci/lint_no_macos_runners.py; this adds "and nothing here
    runs on Windows either, because every shipped binary is cross-built");
  * `release` waits for every job that produces one of its assets;
  * every artifact a build job uploads is actually downloaded by `release`;
  * the release's `files:` list, the rename step and the build matrix all name the same
    set of assets -- three places that have to agree and no compiler to check them;
  * every cross lane names a toolchain file that exists.
"""

from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
import unittest
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "auto-release.yml"

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

    def test_every_job_runs_on_linux(self) -> None:
        for name, job in self.jobs().items():
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

    def test_release_waits_for_every_build_job(self) -> None:
        release = self.jobs()["release"]
        needs = release["needs"]
        self.assertIsInstance(needs, list)
        self.assertIn("build-and-package", needs)
        self.assertIn("build-binaries", needs)

    def test_release_outcome_reports_every_build_job(self) -> None:
        outcome = self.jobs()["release-outcome"]
        self.assertEqual(
            sorted(outcome["needs"]), ["build-and-package", "build-binaries", "release"]
        )
        text = job_run_text(outcome)
        for job in ("build-and-package", "build-binaries", "release"):
            self.assertIn(f"needs.{job}.result", text)

    def test_build_binaries_matrix_is_the_four_cross_lanes(self) -> None:
        rows = self.jobs()["build-binaries"]["strategy"]["matrix"]["include"]
        seen = {row["asset"]: (row["triple"], row["archive"]) for row in rows}
        self.assertEqual(seen, EXPECTED_LANES)

    def test_every_lane_names_an_existing_toolchain_file(self) -> None:
        for triple, _ in EXPECTED_LANES.values():
            toolchain = ROOT / "cmake" / "toolchains" / f"soldr-{triple}.cmake"
            self.assertTrue(toolchain.is_file(), f"missing {toolchain}")

    def test_every_uploaded_artifact_is_downloaded_by_release(self) -> None:
        uploaded: set[str] = set()
        for name, job in self.jobs().items():
            if name == "release":
                continue
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/upload-artifact"):
                    uploaded.add(str(step["with"]["name"]))
        downloaded: set[str] = set()
        patterns: list[str] = []
        for step in self.jobs()["release"]["steps"]:
            if str(step.get("uses", "")).startswith("actions/download-artifact"):
                with_ = cast(dict[str, Any], step["with"])
                if "name" in with_:
                    downloaded.add(str(with_["name"]))
                if "pattern" in with_:
                    patterns.append(str(with_["pattern"]))
        for artifact in uploaded:
            covered = artifact in downloaded or any(
                artifact.startswith(pattern.rstrip("*")) for pattern in patterns
            )
            self.assertTrue(
                covered,
                f"artifact {artifact!r} is uploaded but never downloaded by `release`; it "
                "would not reach the GitHub Release",
            )

    def test_release_files_rename_and_matrix_agree(self) -> None:
        release = self.jobs()["release"]
        gh_release = next(
            step
            for step in release["steps"]
            if str(step.get("uses", "")).startswith("softprops/action-gh-release")
        )
        files = [line.strip() for line in str(gh_release["with"]["files"]).splitlines()]
        files = [line for line in files if line]
        # The architecture-independent amalgamation ZIP plus one archive per cross lane.
        self.assertEqual(len(files), 1 + len(EXPECTED_LANES), files)
        rename_text = job_run_text(release)
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

    def test_the_cross_build_decision_is_documented_in_the_header(self) -> None:
        # #277 row E as first written said release assets "stay built by the platform's own
        # toolchain". Two owner decisions overrode that. The reversal has to be legible in
        # the file that implements it, not only in the issue thread.
        header = WORKFLOW.read_text(encoding="utf-8").split("on:", 1)[0]
        for phrase in ("CROSS-BUILT ON LINUX", "soldr", "native Mac"):
            self.assertIn(phrase, header)


if __name__ == "__main__":
    unittest.main()
