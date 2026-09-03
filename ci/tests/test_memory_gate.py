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

import contextlib
import io
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


class WhereTest(unittest.TestCase):
    """`where` is how a workflow asks "does this lane have a baseline yet?" without
    hardcoding a filename that memory_gate.py computes."""

    def _write(self, directory: Path, result: memory_gate.Result) -> str:
        path = directory / "r.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return str(path)

    def test_exit_zero_and_the_path_for_a_committed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), _result())
            self.assertEqual(memory_gate.where([path]), 0)

    def test_exit_three_for_a_lane_with_no_baseline_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), _result())
            self.assertEqual(memory_gate.where([path], arch="arm64", compiler="soldr-clang-21"), 3)

    def test_an_unreadable_run_is_exit_two_not_three(self) -> None:
        # The distinction run-macos gates on: 3 is "this lane is new", 2 is "something is
        # wrong with this run". Collapsing them would let a crashed gate binary be
        # reported as a missing baseline and exit the step green.
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.json")
            self.assertEqual(memory_gate.main(["memory_gate.py", "where", missing]), 2)


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


class ToleranceTest(unittest.TestCase):
    """#298: the threshold, and the two invariants that keep it from being a guess.

    The gate spent months red on commits that changed no allocator code because the
    tolerance (0.15) was smaller than the workload's own run-to-run spread (16-44%). The
    fix was to make the workload deterministic, and these tests pin the two properties
    that make the resulting number defensible rather than merely smaller.
    """

    def _check(self, peaks: list[float], **overrides: object) -> tuple[int, str]:
        base = json.loads((BASELINES / "linux-pprof1.json").read_text(encoding="utf-8"))["peak_mb"]
        with tempfile.TemporaryDirectory() as tmp:
            paths: list[str] = []
            for i, factor in enumerate(peaks):
                path = Path(tmp) / f"r{i}.json"
                result = _result(platform="linux", peak_mb=base * factor, **overrides)
                path.write_text(json.dumps(result), encoding="utf-8")
                paths.append(str(path))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = memory_gate.check(paths, _control=bool(overrides.get("inject_leak")))
        return rc, out.getvalue()

    def test_a_run_inside_the_tolerance_passes(self) -> None:
        rc, _ = self._check([1.0 + memory_gate.PEAK_TOLERANCE * 0.9] * 8)
        self.assertEqual(rc, 0)

    def test_a_run_past_the_tolerance_fails(self) -> None:
        rc, out = self._check([1.0 + memory_gate.PEAK_TOLERANCE * 1.1] * 8)
        self.assertEqual(rc, 1)
        self.assertIn("regressed", out)

    def test_the_measured_control_margin_is_at_least_twice_the_tolerance(self) -> None:
        # Measured on three ubuntu-latest runner VMs (#298): the
        # MI_BENCH_INJECT_LEAK=200000 build reads min-of-8 82.4 MB against a 58.0 MB
        # baseline, i.e. +42.1%. A gate whose control only just fires is a gate one
        # runner-image change away from proving nothing, so the rule is 2x -- and this
        # test turns "the tolerance may not be raised past that" into a failing test
        # rather than a sentence in a comment.
        measured_control_margin = (82.4 - 58.0) / 58.0
        self.assertGreaterEqual(measured_control_margin, 2.0 * memory_gate.PEAK_TOLERANCE)

    def test_every_committed_baseline_is_quieter_than_the_tolerance(self) -> None:
        # A baseline recorded from a measurement noisier than the threshold it will be
        # compared under is the #298 failure mode in miniature: the number is then a
        # sample of the noise, not the allocator. `update` records the spread it saw, so
        # this is checkable rather than a matter of trust.
        for path in sorted(BASELINES.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            spread = record.get("baseline_spread_pct")
            if spread is None:
                continue  # recorded before `update` started stamping it
            with self.subTest(baseline=path.name):
                self.assertLess(spread, 100.0 * memory_gate.PEAK_TOLERANCE, path.name)

    def test_a_control_run_is_not_told_to_raise_the_tolerance(self) -> None:
        # A leak build's spread is meaningless as advice about the real threshold.
        rc, out = self._check([1.4, 1.8, 2.4], inject_leak=200000)
        self.assertEqual(rc, 1)
        self.assertNotIn("raise RUNS_EXPECTED", out)

    def test_a_real_run_still_is(self) -> None:
        rc, out = self._check([1.0, 1.0 + 2 * memory_gate.PEAK_TOLERANCE])
        self.assertEqual(rc, 0)  # min is inside the tolerance ...
        self.assertIn("raise RUNS_EXPECTED", out)  # ... but the spread is not credible


if __name__ == "__main__":
    unittest.main()
