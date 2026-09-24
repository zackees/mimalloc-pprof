from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import unittest
from pathlib import Path

import yaml

import lint_no_macos_runners as lint

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
AZURE = ROOT / "azure-pipelines.yml"


class LintNoMacosRunnersTests(unittest.TestCase):
    def test_production_workflows_have_only_opt_in_native_macos_runners(self) -> None:
        """#444 permits the full lane only; all other Mac runner labels remain red."""
        self.assertEqual(lint.check(WORKFLOWS), 0)

    def test_full_lane_has_both_arches_and_exact_opt_in_gate(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "macos-bundles.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["run-macos-native-full"]
        self.assertEqual(
            {(row["arch"], row["runner"]) for row in job["strategy"]["matrix"]["include"]},
            {("arm64", "macos-15"), ("x64", "macos-15-intel")},
        )
        gate = job["if"]
        self.assertIn("contains(github.event.pull_request.labels.*.name, 'ci-full')", gate)
        self.assertIn("inputs.ci-mode == 'full'", gate)
        self.assertIn("github.event_name == 'pull_request'", gate)
        self.assertNotIn("github.event_name == 'push'", gate)
        self.assertIn("build-macos", job["needs"])
        self.assertIn("build-rust-apple", job["needs"])
        steps = "\n".join(str(step) for step in job["steps"])
        self.assertIn("ci/run_test_bundle.py", steps)
        self.assertIn("ci/bundle_coverage.py", steps)
        self.assertIn("rust-test-bins-${{ matrix.triple }}", steps)
        self.assertIn("if-no-files-found': 'error'", steps)
        self.assertIn("ci/check_macos_memory_control.py", steps)
        self.assertIn("macos-${{ matrix.arch }}-leak/mimalloc-test-memory-gate", steps)

    def test_full_lane_runs_remote_zone_as_root_for_each_bundle_and_arch(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "macos-bundles.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["run-macos-native-full"]
        self.assertEqual(
            {row["arch"] for row in job["strategy"]["matrix"]["include"]}, {"arm64", "x64"}
        )
        step = next(
            s
            for s in job["steps"]
            if s.get("name") == "Execute the shipped C bundles on native macOS"
        )
        script = step["run"]
        self.assertIn("for config in release debug-full; do", script)
        self.assertIn("--exclude test-osx-zone-introspect-remote", script)
        self.assertIn("--only test-osx-zone-introspect-remote", script)
        self.assertIn('sudo -n "$python" ci/run_test_bundle.py', script)
        self.assertIn("import sys; print(sys.executable)", script)
        self.assertIn('"bundles/$name/tests.json" "results/$name"', script)
        self.assertIn("ordinary.xml", script)
        self.assertIn("remote-root.xml", script)

    def test_every_job_checks_out_the_resolved_candidate(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "macos-bundles.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        resolver = jobs["resolve-candidate"]
        source = "\n".join(str(step) for step in resolver["steps"])
        self.assertIn("^[0-9a-f]{40}$", source)
        self.assertIn("git rev-parse HEAD", source)
        self.assertIn("DISPATCH_SHA", source)
        self.assertIn("PR_SHA", source)
        self.assertIn("candidate_sha", str(workflow))
        for name, job in jobs.items():
            if name == "resolve-candidate":
                continue
            self.assertIn("resolve-candidate", job["needs"], name)
            checkouts = [s for s in job["steps"] if s.get("uses") == "actions/checkout@v4"]
            self.assertEqual(len(checkouts), 1, name)
            self.assertEqual(
                checkouts[0]["with"]["ref"], "${{ needs.resolve-candidate.outputs.sha }}"
            )

    def test_full_lane_exception_fails_when_gate_is_removed(self) -> None:
        from copy import deepcopy

        workflow = yaml.safe_load((WORKFLOWS / "macos-bundles.yml").read_text(encoding="utf-8"))
        weakened = deepcopy(workflow)
        weakened["jobs"]["run-macos-native-full"]["if"] = "github.event_name == 'pull_request'"
        offenders = list(lint.offenders(weakened))
        self.assertEqual(len(offenders), 2)
        self.assertTrue(
            all(
                not lint.allowed_full_runner(WORKFLOWS / "macos-bundles.yml", weakened, path, label)
                for path, label in offenders
            )
        )

    def test_release_smoke_exception_requires_prewrite_dag(self) -> None:
        from copy import deepcopy

        path = WORKFLOWS / "auto-release.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        offenders = list(lint.offenders(workflow))
        smoke_rows = [
            row for row in offenders if row[0] == "jobs.smoke-shipped-assets.strategy.matrix"
        ]
        self.assertEqual(len(smoke_rows), 2)
        self.assertTrue(all(lint.allowed_full_runner(path, workflow, *row) for row in smoke_rows))

        for job_name, key, value in (
            ("smoke-shipped-assets", "needs", ["release"]),
            ("smoke-shipped-assets", "if", "inputs.dry_run == true"),
            ("release", "needs", ["preflight-assets"]),
        ):
            weakened = deepcopy(workflow)
            weakened["jobs"][job_name][key] = value
            self.assertTrue(
                all(not lint.allowed_full_runner(path, weakened, *row) for row in smoke_rows),
                f"weakening {job_name}.{key} should revoke the hosted Mac allowance",
            )

    def test_the_inherited_azure_pipeline_is_scanned_and_clean(self) -> None:
        """It carried macOS-14/macOS-15 jobs; a lint that skipped it would call that clean."""
        self.assertTrue(AZURE.exists())
        self.assertEqual(lint.check(AZURE), 0)

    def test_the_default_targets_cover_azure(self) -> None:
        self.assertIn("azure-pipelines.yml", lint.DEFAULT_TARGETS)

    def _offenders(self, text: str) -> list[str]:
        return [label for _, label in lint.offenders(yaml.safe_load(text))]

    def test_plain_runs_on_is_caught(self) -> None:
        self.assertEqual(
            self._offenders("jobs:\n  a:\n    runs-on: macos-latest\n"), ["macos-latest"]
        )

    def test_matrix_include_runner_is_caught(self) -> None:
        """cross.yml's shape: the label is in the matrix, `runs-on` only interpolates it."""
        text = (
            "jobs:\n  a:\n    runs-on: ${{ matrix.runner }}\n"
            "    strategy:\n      matrix:\n        include:\n"
            "          - target: x86_64-apple-darwin\n            runner: macos-15-intel\n"
        )
        self.assertEqual(self._offenders(text), ["macos-15-intel"])

    def test_matrix_os_list_is_caught(self) -> None:
        text = (
            "jobs:\n  a:\n    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest, windows-latest, macos-latest]\n"
        )
        self.assertEqual(self._offenders(text), ["macos-latest"])

    def test_capitalised_and_versioned_labels_are_caught(self) -> None:
        """release.yaml used `macOS-latest`; test.yaml used `macos-14`."""
        text = "jobs:\n  a:\n    strategy:\n      matrix:\n        os: [macOS-latest, macos-14]\n"
        self.assertEqual(self._offenders(text), ["macOS-latest", "macos-14"])

    def test_prose_and_conditions_are_not_caught(self) -> None:
        """The reason this is a parser and not a grep.

        Every string here contains "macos"/"macOS" and none of them schedules anything:
        a job name, a step name, and a `runner.os` condition. A grep-based lint would have
        to be muzzled until it matched nothing, at which point it would also stop matching
        a real regression.
        """
        text = (
            "jobs:\n"
            "  run-macos-x64-dockur:\n"
            "    name: run-macos-x64 (dockurr/macos on Linux)\n"
            "    runs-on: ubuntu-24.04\n"
            "    steps:\n"
            "      - name: ctest (macos-latest) equivalent\n"
            "        if: runner.os != 'macOS'\n"
            "        run: echo macos-latest\n"
        )
        self.assertEqual(self._offenders(text), [])

    def test_bare_macos_label_in_a_self_hosted_list_is_caught(self) -> None:
        """`runs-on: [self-hosted, macOS, X64]` -- no -latest/-14 suffix to match on."""
        text = "jobs:\n  a:\n    runs-on: [self-hosted, macOS, X64]\n"
        self.assertEqual(self._offenders(text), ["macOS"])

    def test_bare_macos_outside_a_scheduling_key_is_not_caught(self) -> None:
        """Which is why the bare-token rule is scoped to runs-on and friends."""
        text = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    env:\n      PLATFORM: macos\n"
        self.assertEqual(self._offenders(text), [])

    def test_azure_vmimage_is_caught(self) -> None:
        text = "jobs:\n- job:\n  pool:\n    vmImage:\n      macOS-14\n"
        self.assertEqual(self._offenders(text), ["macOS-14"])

    def _unverifiable(self, text: str) -> list[str]:
        return [expression for _, expression in lint.unverifiable(yaml.safe_load(text))]

    def test_a_vars_expression_is_reported_but_does_not_fail(self) -> None:
        """It picks a runner this script cannot see, so a clean scan must not imply safety."""
        text = "jobs:\n  a:\n    runs-on: ${{ vars.RUNNER }}\n"
        self.assertEqual(self._offenders(text), [])
        self.assertEqual(self._unverifiable(text), ["${{ vars.RUNNER }}"])

    def test_a_matrix_interpolation_is_not_reported_as_unverifiable(self) -> None:
        """`${{ matrix.os }}` resolves inside the same file, so the matrix check covers it."""
        text = (
            "jobs:\n  a:\n    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n      matrix:\n        os: [ubuntu-latest]\n"
        )
        self.assertEqual(self._unverifiable(text), [])

    def test_an_external_reusable_workflow_is_reported(self) -> None:
        text = "jobs:\n  a:\n    uses: other-org/repo/.github/workflows/build.yml@v1\n"
        self.assertEqual(
            self._unverifiable(text), ["other-org/repo/.github/workflows/build.yml@v1"]
        )

    def test_a_local_reusable_workflow_is_not_reported(self) -> None:
        """It is scanned like any other file in .github/workflows."""
        text = "jobs:\n  a:\n    uses: ./.github/workflows/build.yml\n"
        self.assertEqual(self._unverifiable(text), [])

    def test_empty_directory_is_an_error_not_a_pass(self) -> None:
        """A lint that passes because it scanned nothing is the bug it exists to prevent."""
        import tempfile

        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(lint.check(Path(empty)), 2)

    def test_a_real_offender_makes_check_fail(self) -> None:
        """The lint's own positive control: it must go red on a file that reintroduces one."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yml"
            bad.write_text("jobs:\n  a:\n    runs-on: macos-latest\n", encoding="utf-8")
            self.assertEqual(lint.check(Path(tmp)), 1)


if __name__ == "__main__":
    unittest.main()
