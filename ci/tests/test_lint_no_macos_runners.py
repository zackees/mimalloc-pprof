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
    def test_production_workflows_have_no_macos_runner(self) -> None:
        """The requirement itself: issue #277 phase B2, no native Mac anywhere."""
        self.assertEqual(lint.check(WORKFLOWS), 0)

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
