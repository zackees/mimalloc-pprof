"""Unit tests for ci/memory_gate.py's baseline identity (issue #277 phase B).

Before phase B a baseline was keyed on platform + MI_PPROF alone. That was sufficient
while `macos-latest` only ever ran binaries built by Xcode on that same machine. Phase B
puts a second, differently-built arm64 binary on the same runner -- cross-compiled on
Linux by soldr's clang -- and comparing its peak against a number recorded by Apple clang
would be a cross-toolchain comparison presented as a regression check. So the identity a
baseline is matched on gained two optional parts, and these tests pin both halves of that:
a run that declares nothing still matches the file it always matched (no existing
baseline moves, the ubuntu one least of all), and a run that declares a toolchain asks for
its own file instead of silently borrowing another's.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

import memory_gate

BASELINES = Path(memory_gate.__file__).resolve().parent / "memory-baselines"


def _result(**overrides: object) -> memory_gate.Result:
    base: dict[str, object] = {
        "schema": 1,
        "platform": "macos",
        "gated_metric": "peak_rss",
        "mi_pprof": 1,
        "inject_leak": 0,
        "peak_mb": 13.4,
        "peak_rss_mb": 13.4,
        "peak_commit_mb": 58.7,
        "profiler_arena_mb": 0.0,
        "peak_minus_profiler_mb": 13.4,
        "purged_gb": 0.07,
        "counters": {
            "threads_start": 1,
            "threads_end": 1,
            "theaps_start": 0,
            "theaps_end": 1,
            "pages_start": 0,
            "pages_end": 1,
        },
    }
    base.update(overrides)
    return cast("memory_gate.Result", base)


class BaselineIdentityTest(unittest.TestCase):
    def test_an_undeclared_run_keeps_the_legacy_filename(self) -> None:
        self.assertEqual(memory_gate.baseline_path(_result()).name, "macos-pprof1.json")

    def test_the_committed_baselines_still_match_undeclared_runs(self) -> None:
        # The point of making arch/compiler optional: no committed file moves, so the
        # ubuntu and windows gates are untouched by phase B.
        for platform, pprof in (("linux", 1), ("macos", 1), ("windows", 1)):
            path = memory_gate.baseline_path(_result(platform=platform, mi_pprof=pprof))
            self.assertTrue(path.is_file(), f"{path} should be the committed baseline")

    def test_a_declared_toolchain_gets_its_own_filename(self) -> None:
        result = memory_gate.stamp(_result(), "arm64", "soldr-clang-21")
        self.assertEqual(
            memory_gate.baseline_path(result).name, "macos-arm64-soldr-clang-21-pprof1.json"
        )

    def test_the_soldr_lane_has_no_committed_baseline_yet(self) -> None:
        # Phase B's first run is deliberately yellow, not red: memory_gate exits 2 and the
        # workflow turns that into a ::warning:: telling the reader to bootstrap it.
        result = memory_gate.stamp(_result(), "arm64", "soldr-clang-21")
        self.assertFalse(memory_gate.baseline_path(result).exists())

    def test_the_existing_macos_baseline_records_the_native_toolchain(self) -> None:
        record = json.loads((BASELINES / "macos-pprof1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["arch"], "arm64")
        self.assertEqual(record["compiler"], "apple-clang")

    def test_arch_alone_is_a_valid_partial_identity(self) -> None:
        self.assertEqual(
            memory_gate.baseline_path(memory_gate.stamp(_result(), "x86_64", None)).name,
            "macos-x86_64-pprof1.json",
        )


class LoadRunsTest(unittest.TestCase):
    def _write(self, directory: Path, name: str, result: memory_gate.Result) -> str:
        path = directory / name
        path.write_text(json.dumps(result), encoding="utf-8")
        return str(path)

    def test_stamping_is_applied_to_every_loaded_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = [
                self._write(Path(tmp), f"r{i}.json", _result(peak_mb=13.0 + i)) for i in range(3)
            ]
            best, peaks, _ = memory_gate.load_runs(paths, "arm64", "soldr-clang-21")
        self.assertEqual(peaks[0], 13.0)
        self.assertEqual(memory_gate.identity(best, "compiler"), "soldr-clang-21")

    def test_runs_from_different_platforms_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = [
                self._write(Path(tmp), "a.json", _result()),
                self._write(Path(tmp), "b.json", _result(platform="linux")),
            ]
            with self.assertRaises(ValueError):
                memory_gate.load_runs(paths)


class OptionParsingTest(unittest.TestCase):
    """`--arch`/`--compiler` are pulled out of argv wherever they appear, so the existing
    `memory_gate.py check result-*.json` shape (a glob of any length) is unchanged."""

    def test_separate_value_form(self) -> None:
        rest, value = memory_gate.take_option(
            ["memory_gate.py", "check", "--arch", "arm64", "a.json"], "arch"
        )
        self.assertEqual(rest, ["memory_gate.py", "check", "a.json"])
        self.assertEqual(value, "arm64")

    def test_equals_form(self) -> None:
        rest, value = memory_gate.take_option(["check", "--compiler=apple-clang"], "compiler")
        self.assertEqual(rest, ["check"])
        self.assertEqual(value, "apple-clang")

    def test_absent_option_is_none_and_argv_is_untouched(self) -> None:
        rest, value = memory_gate.take_option(["check", "a.json", "b.json"], "arch")
        self.assertEqual(rest, ["check", "a.json", "b.json"])
        self.assertIsNone(value)

    def test_a_dangling_option_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            memory_gate.take_option(["check", "--arch"], "arch")


if __name__ == "__main__":
    unittest.main()
