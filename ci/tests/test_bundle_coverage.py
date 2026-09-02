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
        self.assertIn("1 test(s) the native runner executes are absent", table)

    def test_a_ctest_show_only_payload_reads_the_same_as_a_manifest(self) -> None:
        # bundle_tests.py derives one from the other, so one reader covers both shapes.
        show_only = Path(__file__).parent / "fixtures" / "test_bundles" / "ctest-show-only.json"
        with tempfile.TemporaryDirectory() as tmp:
            names = bundle_coverage.read_test_names(show_only)
            bundle = _write(Path(tmp), "bundle.json", sorted(names))
            self.assertEqual(bundle_coverage.main(["--compare", "x", str(show_only), bundle]), 0)
        self.assertIn("test-api", names)


if __name__ == "__main__":
    unittest.main()
