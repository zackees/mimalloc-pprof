"""Unit tests for ci/bundle_coverage.py (issue #277 phase B).

The script's only job is to fail when a bundle runs fewer tests than the runner's own
ctest would have. So the tests that matter are: it does fail then, it does not fail when
the bundle runs *more*, and it refuses an input that would make every comparison
trivially pass.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import bundle_coverage


def _write(directory: Path, name: str, test_names: list[str]) -> str:
    path = directory / name
    path.write_text(json.dumps({"tests": [{"name": n} for n in test_names]}), encoding="utf-8")
    return str(path)


class CoverageTest(unittest.TestCase):
    def test_identical_suites_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            native = _write(Path(tmp), "native.json", ["test-api", "test-stress"])
            bundle = _write(Path(tmp), "bundle.json", ["test-stress", "test-api"])
            self.assertEqual(bundle_coverage.main(["--compare", "release", native, bundle]), 0)

    def test_a_test_only_the_native_runner_has_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            native = _write(Path(tmp), "native.json", ["test-api", "test-osx-zone"])
            bundle = _write(Path(tmp), "bundle.json", ["test-api"])
            self.assertEqual(bundle_coverage.main(["--compare", "release", native, bundle]), 1)

    def test_a_test_only_the_bundle_has_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            native = _write(Path(tmp), "native.json", ["test-api"])
            bundle = _write(Path(tmp), "bundle.json", ["test-api", "test-extra"])
            self.assertEqual(bundle_coverage.main(["--compare", "release", native, bundle]), 0)

    def test_an_empty_side_is_refused_rather_than_passing_vacuously(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            native = _write(Path(tmp), "native.json", [])
            bundle = _write(Path(tmp), "bundle.json", ["test-api"])
            self.assertEqual(bundle_coverage.main(["--compare", "release", native, bundle]), 2)

    def test_a_missing_file_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _write(Path(tmp), "bundle.json", ["test-api"])
            self.assertEqual(
                bundle_coverage.main(
                    ["--compare", "release", str(Path(tmp) / "nope.json"), bundle]
                ),
                2,
            )

    def test_the_table_names_the_missing_tests(self) -> None:
        comparison = bundle_coverage.Comparison(
            label="ctest-debug-full", native={"a", "b"}, bundle={"a", "c"}
        )
        table = bundle_coverage.render([comparison])
        self.assertIn("`b`", table)
        self.assertIn("`c`", table)
        self.assertIn("1 test(s) present in the native side are absent", table)

    def test_the_report_can_name_both_sides(self) -> None:
        """#277 phase B2: on macOS the reference side is the arm64 bundle, not a ctest run.

        The heading and column names are the only thing that says which comparison was
        actually made, so a report that still claimed "native ctest" would be a lie.
        """
        comparison = bundle_coverage.Comparison(label="release", native={"a", "b"}, bundle={"a"})
        table = bundle_coverage.render(
            [comparison], heading="arm64 vs x86_64", reference="arm64", candidate="x86_64"
        )
        self.assertIn("### arm64 vs x86_64", table)
        self.assertIn("| config | arm64 | x86_64 | missing from x86_64 |", table)
        self.assertIn("1 test(s) present in the arm64 side are absent from the x86_64 side", table)
        self.assertNotIn("native", table)

    def test_a_ctest_show_only_payload_reads_the_same_as_a_manifest(self) -> None:
        # bundle_tests.py derives one from the other, so one reader covers both shapes.
        show_only = Path(__file__).parent / "fixtures" / "test_bundles" / "ctest-show-only.json"
        with tempfile.TemporaryDirectory() as tmp:
            names = bundle_coverage.read_test_names(show_only)
            bundle = _write(Path(tmp), "bundle.json", sorted(names))
            self.assertEqual(bundle_coverage.main(["--compare", "x", str(show_only), bundle]), 0)
        self.assertIn("test-api", names)


class JUnitCandidateTest(unittest.TestCase):
    """#307: the candidate side is what actually RAN, read from run_test_bundle's JUnit.

    A manifest only proves the bundle *contains* a test. Once the run stage stopped
    producing a native ctest reference of its own, "was it executed" became the property
    worth asserting, so the comparison reads the JUnit the run wrote.
    """

    def _junit(self, directory: Path, name: str, body: str) -> str:
        path = directory / name
        path.write_text(f'<?xml version="1.0"?><testsuite>{body}</testsuite>', encoding="utf-8")
        return str(path)

    def test_executed_names_come_from_junit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api", "test-stress"])
            executed = self._junit(
                Path(tmp),
                "release.xml",
                '<testcase name="test-api" classname="test-api" status="run"/>'
                '<testcase name="test-stress" classname="test-stress" status="run"/>',
            )
            self.assertEqual(bundle_coverage.main(["--compare", "release", reference, executed]), 0)

    def test_a_name_that_was_never_executed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api", "test-stress"])
            executed = self._junit(
                Path(tmp), "release.xml", '<testcase name="test-api" classname="test-api"/>'
            )
            self.assertEqual(bundle_coverage.main(["--compare", "release", reference, executed]), 1)

    def test_env_variants_collapse_onto_their_base_name(self) -> None:
        """`test-x [LABEL]` is another environment for test-x, not extra coverage."""
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api"])
            executed = self._junit(
                Path(tmp),
                "guarded.xml",
                '<testcase name="test-api" classname="test-api" status="run"/>'
                '<testcase name="test-api [forced]" classname="test-api" status="run"/>',
            )
            self.assertEqual(bundle_coverage.read_test_names(Path(executed)), {"test-api"})
            self.assertEqual(bundle_coverage.main(["--compare", "guarded", reference, executed]), 0)

    def test_a_skipped_case_does_not_count_as_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api", "test-osx-zone"])
            executed = self._junit(
                Path(tmp),
                "release.xml",
                '<testcase name="test-api" classname="test-api" status="run"/>'
                '<testcase name="test-osx-zone" classname="test-osx-zone"><skipped/></testcase>',
            )
            self.assertEqual(bundle_coverage.main(["--compare", "release", reference, executed]), 1)

    def test_a_directory_candidate_is_the_union_of_its_files(self) -> None:
        """The run stage is two runners now: the wave's JUnit and the serial group's."""
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api", "test-memory-gate"])
            merged = Path(tmp) / "merged"
            merged.mkdir()
            self._junit(
                merged, "wave.xml", '<testcase name="test-api" classname="test-api" status="run"/>'
            )
            self._junit(
                merged,
                "serial.xml",
                '<testcase name="test-memory-gate" classname="test-memory-gate" status="run"/>',
            )
            self.assertEqual(
                bundle_coverage.main(["--compare", "release", reference, str(merged)]), 0
            )

    def test_a_directory_missing_one_half_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api", "test-memory-gate"])
            merged = Path(tmp) / "merged"
            merged.mkdir()
            self._junit(
                merged, "wave.xml", '<testcase name="test-api" classname="test-api" status="run"/>'
            )
            self.assertEqual(
                bundle_coverage.main(["--compare", "release", reference, str(merged)]), 1
            )

    def test_an_empty_directory_is_refused_rather_than_passing_trivially(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api"])
            empty = Path(tmp) / "empty"
            empty.mkdir()
            self.assertEqual(
                bundle_coverage.main(["--compare", "release", reference, str(empty)]), 2
            )

    def test_an_empty_junit_is_refused_rather_than_passing_trivially(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = _write(Path(tmp), "show-only.json", ["test-api"])
            executed = self._junit(Path(tmp), "empty.xml", "")
            self.assertEqual(bundle_coverage.main(["--compare", "release", reference, executed]), 2)


if __name__ == "__main__":
    unittest.main()
