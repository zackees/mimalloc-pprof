from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import unittest
from pathlib import Path

import yaml

import lint_no_macos_runners as lint

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


class LintNoMacosRunnersTests(unittest.TestCase):
    def test_production_workflows_have_no_macos_runner(self) -> None:
        """The requirement itself: issue #277 phase B2, no native Mac anywhere."""
        self.assertEqual(lint.check(WORKFLOWS), 0)

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

    def test_empty_directory_is_an_error_not_a_pass(self) -> None:
        """A lint that passes because it scanned nothing is the bug it exists to prevent."""
        import tempfile

        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(lint.check(Path(empty)), 2)


if __name__ == "__main__":
    unittest.main()
