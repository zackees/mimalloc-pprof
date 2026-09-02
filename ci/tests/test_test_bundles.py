"""Unit tests for ci/bundle_tests.py and ci/run_test_bundle.py (issue #277 phase A).

The bundle format's whole job is to carry a test suite to a machine that has no CMake and
no checkout, without quietly changing what any test asserts. Two failure modes matter and
both are covered here:

  * a command shape the bundler cannot lower is *dropped*, so the bundle reports green on
    fewer tests than ctest ran -- every unsupported shape must raise instead
  * a negative control is lowered into something that passes for the wrong reason (a zero
    exit, a timeout, or the wrong message) -- run-negative.cmake's exact semantics are
    pinned below
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import bundle_tests
import run_test_bundle

FIXTURES = Path(__file__).parent / "fixtures" / "test_bundles"
SHOW_ONLY = FIXTURES / "ctest-show-only.json"
BUNDLE_TESTS = Path(__file__).resolve().parents[1] / "bundle_tests.py"
RUN_BUNDLE = Path(__file__).resolve().parents[1] / "run_test_bundle.py"

BUILD = Path("/build") if os.name != "nt" else Path("C:/build")
BUNDLE = bundle_tests.BUNDLE_PLACEHOLDER


def _payload() -> object:
    text = SHOW_ONLY.read_text(encoding="utf-8")
    if os.name == "nt":
        text = text.replace('"/build', '"C:/build').replace('"/src', '"C:/src')
    return json.loads(text)


def _convert() -> dict[str, bundle_tests.BundledTest]:
    tests, _ = bundle_tests.convert(_payload(), BUILD)
    return {test.name: test for test in tests}


def _test_entry(name: str, command: list[str], properties: object = None) -> dict[str, object]:
    return {"name": name, "command": command, "properties": properties or []}


def _wrap(*entries: dict[str, object]) -> dict[str, object]:
    return {"kind": "ctestInfo", "version": {"major": 1, "minor": 0}, "tests": list(entries)}


class LoweringTest(unittest.TestCase):
    def test_plain_executable_passes_through(self) -> None:
        test = _convert()["test-api"]
        self.assertEqual(test.argv, ["${BUNDLE}/mimalloc-test-api"])
        self.assertFalse(test.expect_nonzero)
        self.assertIsNone(test.expect_text)
        self.assertEqual(test.cwd, "${BUNDLE}")

    def test_environment_property_becomes_env(self) -> None:
        test = _convert()["test-zero-tracking-enabled"]
        self.assertEqual(test.env, {"MIMALLOC_PURGE_ZEROES": "1", "MIMALLOC_PURGE_DELAY": "0"})
        self.assertEqual(test.timeout, 900.0)

    def test_default_timeout_is_ctests_own(self) -> None:
        self.assertEqual(_convert()["test-api"].timeout, bundle_tests.DEFAULT_TIMEOUT_SECONDS)

    def test_cmake_e_env_lowers_to_env_and_the_real_argv(self) -> None:
        test = _convert()["test-stress-dynamic"]
        self.assertEqual(test.argv, ["${BUNDLE}/mimalloc-test-stress-dynamic"])
        self.assertEqual(test.env["MIMALLOC_VERBOSE"], "1")
        # The absolute build-tree path inside an ENV VALUE must be relativised too.
        self.assertEqual(test.env["LD_PRELOAD"], "${BUNDLE}/libmimalloc-debug.so.3.4")

    def test_cmake_e_env_stops_at_the_first_non_assignment(self) -> None:
        lowered = bundle_tests.lower_command(
            ["/usr/bin/cmake", "-E", "env", "A=1", "/build/exe", "B=2"], "t"
        )
        self.assertEqual(lowered.env, {"A": "1"})
        self.assertEqual(lowered.argv, ["/build/exe", "B=2"])

    def test_cmake_e_env_options_are_refused_not_ignored(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.lower_command(
                ["/usr/bin/cmake", "-E", "env", "--unset=FOO", "/build/exe"], "t-unset"
            )
        self.assertIn("t-unset", str(caught.exception))
        self.assertIn("--unset=FOO", str(caught.exception))

    def test_other_cmake_e_modes_are_refused(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.lower_command(["/usr/bin/cmake", "-E", "rm", "-f", "x"], "t-rm")
        self.assertIn("cmake -E rm", str(caught.exception))


class NegativeControlTest(unittest.TestCase):
    """test/run-negative.cmake semantics, pinned field by field."""

    def test_lowers_exe_arg_text_and_timeout(self) -> None:
        test = _convert()["test-lock-reentrancy"]
        self.assertEqual(test.argv, ["${BUNDLE}/mimalloc-test-lock-reentrancy", "reentrant"])
        self.assertTrue(test.expect_nonzero)
        self.assertEqual(test.expect_text, "reentrant_internal_lock_acquisition")
        # The 10 s the cmake script wraps execute_process in, NOT the test's TIMEOUT 15.
        self.assertEqual(test.timeout, bundle_tests.NEGATIVE_TIMEOUT_SECONDS)

    def test_test_arg_is_optional(self) -> None:
        lowered = bundle_tests.lower_command(
            [
                "/usr/bin/cmake",
                "-DTEST_EXE=/build/exe",
                "-DEXPECTED_TEXT=boom",
                "-P",
                "/src/test/run-negative.cmake",
            ],
            "t",
        )
        self.assertEqual(lowered.argv, ["/build/exe"])
        self.assertEqual(lowered.expect_text, "boom")

    def test_missing_expected_text_is_an_error(self) -> None:
        with self.assertRaises(bundle_tests.BundleError):
            bundle_tests.lower_command(
                ["/usr/bin/cmake", "-DTEST_EXE=/build/exe", "-P", "/src/test/run-negative.cmake"],
                "t",
            )

    def test_an_unknown_cmake_script_is_refused(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.lower_command(
                ["/usr/bin/cmake", "-DX=1", "-P", "/src/test/something-else.cmake"], "t-script"
            )
        self.assertIn("run-negative.cmake", str(caught.exception))

    def test_unknown_define_is_refused(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.lower_command(
                [
                    "/usr/bin/cmake",
                    "-DTEST_EXE=/build/exe",
                    "-DEXPECTED_TEXT=boom",
                    "-DEXTRA=surprise",
                    "-P",
                    "/src/test/run-negative.cmake",
                ],
                "t",
            )
        self.assertIn("EXTRA", str(caught.exception))


class TextCheckTest(unittest.TestCase):
    """test/run-text-check.cmake semantics (issue #268), the zero-exit mirror of
    run-negative.cmake: REQUIRE lowers to expect_text, FORBID lowers to forbid_text, and
    both leave expect_nonzero False (the opposite exit-code contract)."""

    def test_lowers_require_mode(self) -> None:
        lowered = bundle_tests.lower_command(
            [
                "/usr/bin/cmake",
                "-DTEST_EXE=/build/exe",
                "-DEXPECTED_TEXT=process done",
                "-DMODE=REQUIRE",
                "-P",
                "/src/test/run-text-check.cmake",
            ],
            "t",
        )
        self.assertEqual(lowered.argv, ["/build/exe"])
        self.assertFalse(lowered.expect_nonzero)
        self.assertEqual(lowered.expect_text, "process done")
        self.assertIsNone(lowered.forbid_text)
        self.assertEqual(lowered.timeout, bundle_tests.NEGATIVE_TIMEOUT_SECONDS)

    def test_lowers_forbid_mode(self) -> None:
        lowered = bundle_tests.lower_command(
            [
                "/usr/bin/cmake",
                "-DTEST_EXE=/build/exe",
                "-DEXPECTED_TEXT=process done",
                "-DMODE=FORBID",
                "-P",
                "/src/test/run-text-check.cmake",
            ],
            "t",
        )
        self.assertFalse(lowered.expect_nonzero)
        self.assertIsNone(lowered.expect_text)
        self.assertEqual(lowered.forbid_text, "process done")

    def test_missing_mode_is_an_error(self) -> None:
        with self.assertRaises(bundle_tests.BundleError):
            bundle_tests.lower_command(
                [
                    "/usr/bin/cmake",
                    "-DTEST_EXE=/build/exe",
                    "-DEXPECTED_TEXT=boom",
                    "-P",
                    "/src/test/run-text-check.cmake",
                ],
                "t",
            )

    def test_invalid_mode_is_an_error(self) -> None:
        with self.assertRaises(bundle_tests.BundleError):
            bundle_tests.lower_command(
                [
                    "/usr/bin/cmake",
                    "-DTEST_EXE=/build/exe",
                    "-DEXPECTED_TEXT=boom",
                    "-DMODE=SIDEWAYS",
                    "-P",
                    "/src/test/run-text-check.cmake",
                ],
                "t",
            )

    def test_unknown_define_is_refused(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.lower_command(
                [
                    "/usr/bin/cmake",
                    "-DTEST_EXE=/build/exe",
                    "-DEXPECTED_TEXT=boom",
                    "-DMODE=REQUIRE",
                    "-DEXTRA=surprise",
                    "-P",
                    "/src/test/run-text-check.cmake",
                ],
                "t",
            )
        self.assertIn("EXTRA", str(caught.exception))


class PathRewritingTest(unittest.TestCase):
    def test_build_tree_paths_become_placeholders_and_register_assets(self) -> None:
        _, assets = bundle_tests.convert(_payload(), BUILD)
        self.assertIn("mimalloc-test-api", assets)
        self.assertIn("libmimalloc-debug.so.3.4", assets)

    def test_paths_outside_the_build_tree_are_left_alone(self) -> None:
        rewriter = bundle_tests.PathRewriter(BUILD)
        self.assertEqual(rewriter.rewrite("MIMALLOC_VERBOSE=1"), "MIMALLOC_VERBOSE=1")
        self.assertEqual(rewriter.rewrite("/usr/lib/libc.so"), "/usr/lib/libc.so")
        self.assertEqual(rewriter.assets, {})

    def test_dyld_insert_libraries_is_relativised_like_ld_preload(self) -> None:
        """macOS's env var carries the same absolute build-tree path (#277 review, item 4)."""
        payload = _wrap(
            _test_entry(
                "test-stress-dynamic",
                [
                    "/usr/bin/cmake",
                    "-E",
                    "env",
                    "MIMALLOC_VERBOSE=1",
                    f"DYLD_INSERT_LIBRARIES={BUILD.as_posix()}/libmimalloc-debug.dylib",
                    f"{BUILD.as_posix()}/mimalloc-test-stress-dynamic",
                ],
            )
        )
        tests, assets = bundle_tests.convert(payload, BUILD)
        self.assertEqual(tests[0].env["DYLD_INSERT_LIBRARIES"], "${BUNDLE}/libmimalloc-debug.dylib")
        self.assertIn("libmimalloc-debug.dylib", assets)

    def test_basename_collision_is_an_error_not_a_silent_overwrite(self) -> None:
        payload = _wrap(
            _test_entry("a", [f"{BUILD.as_posix()}/one/mimalloc-test-api"]),
            _test_entry("b", [f"{BUILD.as_posix()}/two/mimalloc-test-api"]),
        )
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        self.assertIn("mimalloc-test-api", str(caught.exception))

    def test_an_executable_outside_the_build_tree_is_refused(self) -> None:
        payload = _wrap(_test_entry("t-outside", ["/usr/bin/true"]))
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        self.assertIn("t-outside", str(caught.exception))


class LeakedAbsolutePathTest(unittest.TestCase):
    """Phase A only validated argv[0]; phase B ships bundles to another machine.

    A path outside the build tree is not rewritten (PathRewriter only knows the build
    prefix), so before this scan it was copied into the manifest verbatim and pointed at a
    directory the macOS/Windows runner does not have. Review follow-up on PR #279.
    """

    def test_an_absolute_path_in_a_later_argv_element_is_refused(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-argv", [f"{BUILD.as_posix()}/mimalloc-test-api", "/home/runner/work/src/x.txt"]
            )
        )
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        self.assertIn("t-argv", str(caught.exception))
        self.assertIn("argv[1]", str(caught.exception))

    def test_an_absolute_path_in_an_env_value_is_refused(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-env",
                [f"{BUILD.as_posix()}/mimalloc-test-api"],
                [{"name": "ENVIRONMENT", "value": ["SOME_DIR=/opt/toolchain/lib"]}],
            )
        )
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        self.assertIn("env[SOME_DIR]", str(caught.exception))

    def test_an_absolute_working_directory_is_refused(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-cwd",
                [f"{BUILD.as_posix()}/mimalloc-test-api"],
                [{"name": "WORKING_DIRECTORY", "value": "/some/source/tree"}],
            )
        )
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        self.assertIn("cwd", str(caught.exception))

    def test_a_pathsep_joined_value_reports_the_leaking_element(self) -> None:
        joined = os.pathsep.join([f"{BUNDLE}/lib", "/opt/toolchain/lib"])
        test = bundle_tests.BundledTest(name="t", argv=[f"{BUNDLE}/exe"], env={"MY_PATH": joined})
        self.assertEqual(bundle_tests.leaked_paths(test), ["env[MY_PATH]: /opt/toolchain/lib"])

    def test_a_windows_drive_path_is_absolute_even_on_linux(self) -> None:
        # Every cross bundle is produced on Linux, where os.path.isabs("C:/build/x") is
        # False -- so a Windows bundle's leak would pass unnoticed without this.
        self.assertTrue(bundle_tests.looks_absolute("C:/build/x"))
        self.assertTrue(bundle_tests.looks_absolute("C:\\build\\x"))
        self.assertTrue(bundle_tests.looks_absolute("\\\\server\\share"))
        self.assertFalse(bundle_tests.looks_absolute("relative/path"))
        self.assertFalse(bundle_tests.looks_absolute("MIMALLOC_VERBOSE=1"))

    def test_relative_and_placeholder_values_are_accepted(self) -> None:
        test = bundle_tests.BundledTest(
            name="t",
            argv=[f"{BUNDLE}/exe", "reentrant"],
            env={"A": "1", "B": f"{BUNDLE}/libmimalloc.dylib"},
            cwd=BUNDLE,
        )
        self.assertEqual(bundle_tests.leaked_paths(test), [])

    def test_a_flag_prefixed_absolute_path_is_refused(self) -> None:
        # Deferred phase B review finding: the scan matched a *bare* absolute path, so
        # `--out=/abs` and `-I/abs` -- the shapes a CMake test command most plausibly
        # carries -- read as relative and shipped verbatim.
        for argument in ("--out=/opt/toolchain/x", "-I/opt/toolchain/include", "-L/abs/lib"):
            test = bundle_tests.BundledTest(name="t", argv=[f"{BUNDLE}/exe", argument])
            self.assertEqual(bundle_tests.leaked_paths(test), [f"argv[1]: {argument}"], argument)

    def test_a_flag_without_a_path_is_not_a_leak(self) -> None:
        test = bundle_tests.BundledTest(
            name="t", argv=[f"{BUNDLE}/exe", "--threads=4", "-O2", "--out=relative/x"]
        )
        self.assertEqual(bundle_tests.leaked_paths(test), [])

    def test_a_windows_path_inside_a_separated_list_is_refused(self) -> None:
        # The phase B splitter split on os.pathsep, which is ":" on the Linux host every
        # Windows bundle is built on -- so `C:\\build\\x` became "C" and "\\build\\x",
        # neither of which looks absolute, and the leak scanned clean.
        test = bundle_tests.BundledTest(
            name="t",
            argv=[f"{BUNDLE}/exe"],
            env={"MY_PATH": f"{BUNDLE};C:\\build\\x"},
        )
        self.assertEqual(bundle_tests.leaked_paths(test), ["env[MY_PATH]: C:\\build\\x"])

    def test_splitting_never_cuts_a_drive_letter_in_half(self) -> None:
        self.assertEqual(bundle_tests.split_path_list("C:\\build\\x"), ["C:\\build\\x"])
        self.assertEqual(bundle_tests.split_path_list("C:/a;D:/b"), ["C:/a", "D:/b"])
        self.assertEqual(bundle_tests.split_path_list("/tmp/x:C:\\y"), ["/tmp/x", "C:\\y"])
        self.assertEqual(bundle_tests.split_path_list("/a:/b"), ["/a", "/b"])
        self.assertEqual(bundle_tests.split_path_list("1"), ["1"])

    def test_the_manifest_records_no_absolute_build_directory(self) -> None:
        # `generated_from.build_dir` used to be the one host path left in tests.json,
        # which made "the manifest contains zero absolute paths" untrue as stated.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            out.mkdir()
            bundle_tests.write_manifest(out, [], Path(tmp) / "build-debug-full", "Debug")
            manifest = json.loads((out / "tests.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["generated_from"]["build_dir"], "build-debug-full")


class PropertyHandlingTest(unittest.TestCase):
    def test_unsupported_property_is_refused(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-regex",
                [f"{BUILD.as_posix()}/exe"],
                [{"name": "PASS_REGULAR_EXPRESSION", "value": ["ok"]}],
            )
        )
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        self.assertIn("PASS_REGULAR_EXPRESSION", str(caught.exception))

    def test_will_fail_lowers_to_expect_nonzero(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-willfail", [f"{BUILD.as_posix()}/exe"], [{"name": "WILL_FAIL", "value": True}]
            )
        )
        tests, _ = bundle_tests.convert(payload, BUILD)
        self.assertTrue(tests[0].expect_nonzero)

    def test_labels_are_carried(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-lab", [f"{BUILD.as_posix()}/exe"], [{"name": "LABELS", "value": ["slow"]}]
            )
        )
        tests, _ = bundle_tests.convert(payload, BUILD)
        self.assertEqual(tests[0].labels, ["slow"])

    def test_cmake_e_env_beats_the_environment_property_on_a_conflict(self) -> None:
        payload = _wrap(
            _test_entry(
                "t-both",
                ["/usr/bin/cmake", "-E", "env", "K=wrapper", f"{BUILD.as_posix()}/exe"],
                [{"name": "ENVIRONMENT", "value": ["K=property"]}],
            )
        )
        tests, _ = bundle_tests.convert(payload, BUILD)
        self.assertEqual(tests[0].env["K"], "wrapper")

    def test_every_problem_is_reported_at_once(self) -> None:
        payload = _wrap(
            _test_entry("t-one", ["/usr/bin/cmake", "-E", "rm", "x"]),
            _test_entry("t-two", ["/usr/bin/cmake", "-E", "copy", "a", "b"]),
        )
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(payload, BUILD)
        message = str(caught.exception)
        self.assertIn("t-one", message)
        self.assertIn("t-two", message)
        self.assertIn("2 test(s)", message)


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------

PROBE = """
import os, sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
if mode == "ok":
    print("all good")
elif mode == "boom":
    print("expected_marker_here")
    sys.exit(3)
elif mode == "wrong-reason":
    print("something else entirely")
    sys.exit(3)
elif mode == "hang":
    print("hanging", flush=True)
    time.sleep(30)
elif mode == "env":
    print("K=" + os.environ.get("K", "<unset>"))
elif mode == "cwd":
    print("CWD=" + os.getcwd())
sys.exit(0)
"""


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self._tmp.name)
        (self.bundle / "probe.py").write_text(PROBE, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def write_manifest(self, tests: list[dict[str, object]]) -> None:
        (self.bundle / "tests.json").write_text(
            json.dumps({"version": 1, "tests": tests}), encoding="utf-8"
        )

    def spec(self, name: str, mode: str, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "name": name,
            "argv": [sys.executable, "${BUNDLE}/probe.py", mode],
            "env": {},
            "cwd": "${BUNDLE}",
            "timeout": 30.0,
            "expect_nonzero": False,
            "expect_text": None,
            "labels": [],
        }
        entry.update(overrides)
        return entry

    def run_bundle(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUN_BUNDLE), str(self.bundle), *extra],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_passing_and_failing_tests(self) -> None:
        self.write_manifest([self.spec("t-ok", "ok"), self.spec("t-bad", "boom")])
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("1 tests failed out of 2", proc.stdout)
        self.assertIn("exit code 3", proc.stdout)

    def test_expect_nonzero_and_expect_text(self) -> None:
        self.write_manifest(
            [self.spec("t-neg", "boom", expect_nonzero=True, expect_text="expected_marker_here")]
        )
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("100% tests passed", proc.stdout)

    def test_forbid_text_absent_passes(self) -> None:
        """issue #268's MODE=FORBID: a zero-exit test where the forbidden text does not
        appear in the output passes."""
        self.write_manifest([self.spec("t-forbid-ok", "ok", forbid_text="expected_marker_here")])
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("100% tests passed", proc.stdout)

    def test_forbid_text_present_is_red(self) -> None:
        """The mirror image: the forbidden text DOES appear, so the test must fail even
        though the process exited zero."""
        self.write_manifest([self.spec("t-forbid-bad", "ok", forbid_text="all good")])
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("forbidden text", proc.stdout)

    def test_negative_control_that_fails_for_the_wrong_reason_is_red(self) -> None:
        self.write_manifest(
            [
                self.spec(
                    "t-neg", "wrong-reason", expect_nonzero=True, expect_text="expected_marker_here"
                )
            ]
        )
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not found in output", proc.stdout)

    def test_negative_control_that_exits_zero_is_red(self) -> None:
        self.write_manifest([self.spec("t-neg", "ok", expect_nonzero=True)])
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("expected a non-zero exit", proc.stdout)

    def test_timeout_is_a_failure_even_for_a_negative_control(self) -> None:
        """run-negative.cmake: 'timed out instead of failing fast' is a FATAL_ERROR."""
        self.write_manifest(
            [self.spec("t-hang", "hang", timeout=1.0, expect_nonzero=True, expect_text="hanging")]
        )
        proc = self.run_bundle()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("timed out", proc.stdout)

    def test_timeout_scale(self) -> None:
        self.write_manifest([self.spec("t-hang", "hang", timeout=100.0)])
        proc = self.run_bundle("--timeout-scale", "0.01")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("timed out after 1s", proc.stdout)

    def test_manifest_env_reaches_the_child(self) -> None:
        self.write_manifest(
            [self.spec("t-env", "env", env={"K": "from-manifest"}, expect_text="K=from-manifest")]
        )
        self.assertEqual(self.run_bundle().returncode, 0)

    def test_cli_env_overrides_the_manifest(self) -> None:
        self.write_manifest(
            [self.spec("t-env", "env", env={"K": "from-manifest"}, expect_text="K=from-cli")]
        )
        self.assertEqual(self.run_bundle().returncode, 1, "manifest value alone must not match")
        self.assertEqual(self.run_bundle("--env", "K=from-cli").returncode, 0)

    def test_only_selects_a_subset(self) -> None:
        self.write_manifest([self.spec("t-ok", "ok"), self.spec("t-bad", "boom")])
        proc = self.run_bundle("--only", "t-ok")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("out of 1", proc.stdout)

    def test_only_with_an_unknown_name_is_an_error(self) -> None:
        self.write_manifest([self.spec("t-ok", "ok")])
        proc = self.run_bundle("--only", "t-nope")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no such test", proc.stderr)

    def test_cwd_defaults_to_the_bundle_root(self) -> None:
        self.write_manifest([self.spec("t-cwd", "cwd", expect_text=f"CWD={self.bundle.resolve()}")])
        self.assertEqual(self.run_bundle().returncode, 0)

    def test_bundle_is_prepended_to_the_loader_path(self) -> None:
        """Otherwise a bundle 'passes' on the build machine via the build-tree RPATH."""
        spec = run_test_bundle.TestSpec(
            name="t",
            argv=("x",),
            env={},
            cwd="${BUNDLE}",
            timeout=1.0,
            expect_nonzero=False,
            expect_text=None,
            forbid_text=None,
            labels=(),
        )
        env = run_test_bundle.build_environment(
            spec, self.bundle, {}, {"LD_LIBRARY_PATH": "/existing"}
        )
        self.assertEqual(env["LD_LIBRARY_PATH"], f"{self.bundle}{os.pathsep}/existing")
        self.assertEqual(env["DYLD_LIBRARY_PATH"], str(self.bundle))

    def test_junit_roundtrips_through_compare(self) -> None:
        self.write_manifest([self.spec("t-ok", "ok"), self.spec("t-bad", "boom")])
        report = self.bundle / "out.xml"
        self.run_bundle("--junit", str(report))
        outcomes = run_test_bundle.parse_junit(report)
        self.assertEqual(outcomes, {"t-ok": True, "t-bad": False})

    def test_a_missing_reference_file_explains_the_ctest_gotcha(self) -> None:
        self.write_manifest([self.spec("t-ok", "ok")])
        proc = self.run_bundle("--compare-junit", str(self.bundle / "nope.xml"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("build directory", proc.stderr + proc.stdout)

    def test_compare_junit_detects_a_missing_test(self) -> None:
        reference = self.bundle / "ctest.xml"
        reference.write_text(
            '<?xml version="1.0"?><testsuite>'
            '<testcase name="t-ok" status="run"/>'
            '<testcase name="t-gone" status="run"/>'
            "</testsuite>",
            encoding="utf-8",
        )
        self.write_manifest([self.spec("t-ok", "ok")])
        proc = self.run_bundle("--compare-junit", str(reference))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("t-gone", proc.stderr)
        self.assertIn("does not contain it", proc.stderr)

    def test_compare_junit_detects_a_result_mismatch(self) -> None:
        reference = self.bundle / "ctest.xml"
        reference.write_text(
            '<?xml version="1.0"?><testsuite><testcase name="t-bad" status="run"/></testsuite>',
            encoding="utf-8",
        )
        self.write_manifest([self.spec("t-bad", "boom")])
        proc = self.run_bundle("--compare-junit", str(reference))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ctest says pass", proc.stderr)

    def test_compare_junit_accepts_an_identical_run(self) -> None:
        reference = self.bundle / "ctest.xml"
        reference.write_text(
            '<?xml version="1.0"?><testsuite>'
            '<testcase name="t-ok" status="run"/>'
            '<testcase name="t-bad" status="fail"><failure message="x"/></testcase>'
            "</testsuite>",
            encoding="utf-8",
        )
        self.write_manifest([self.spec("t-ok", "ok"), self.spec("t-bad", "boom")])
        proc = self.run_bundle("--compare-junit", str(reference))
        self.assertIn("same 2 test names, same results", proc.stdout)
        # Still exit 1: one test genuinely failed. The comparison only asserts agreement.
        self.assertEqual(proc.returncode, 1)

    def test_ctest_skipped_cases_are_ignored_by_the_comparison(self) -> None:
        reference = self.bundle / "ctest.xml"
        reference.write_text(
            '<?xml version="1.0"?><testsuite>'
            '<testcase name="t-ok" status="run"/>'
            '<testcase name="t-skip" status="run"><skipped/></testcase>'
            "</testsuite>",
            encoding="utf-8",
        )
        self.write_manifest([self.spec("t-ok", "ok")])
        proc = self.run_bundle("--compare-junit", str(reference))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class EndToEndBundleTest(unittest.TestCase):
    """A tiny fake 'build tree' bundled by the real CLI and replayed by the real runner."""

    def test_roundtrip_without_the_build_tree(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            build = Path(root) / "build"
            build.mkdir()
            script = build / "fake-test"
            script.write_text("#!/bin/sh\necho fake ok\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            library = build / "libmimalloc-fake.so"
            library.write_bytes(b"not really a library")

            payload = _wrap(
                _test_entry(
                    "fake-test",
                    [str(script)],
                    [{"name": "WORKING_DIRECTORY", "value": str(build)}],
                )
            )
            tests, assets = bundle_tests.convert(payload, build)
            out = Path(root) / "bundle"
            bundle_tests.copy_into(
                out, list(assets.values()) + bundle_tests.library_files(build, None)
            )
            bundle_tests.write_manifest(out, tests, build, None)

            # The acceptance claim is "only uv and the bundle": prove the build tree is
            # genuinely unnecessary by deleting it before replaying.
            for path in sorted(build.iterdir()):
                path.unlink()
            build.rmdir()

            self.assertTrue((out / "libmimalloc-fake.so").is_file())
            if os.name != "nt":
                proc = subprocess.run(
                    [sys.executable, str(RUN_BUNDLE), str(out)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("100% tests passed", proc.stdout)

    def test_an_empty_suite_is_an_error_not_an_empty_green_bundle(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            bundle_tests.convert(_wrap(), BUILD)
        self.assertIn("no tests", str(caught.exception))

    def test_cli_refuses_a_build_dir_with_no_ctest(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            proc = subprocess.run(
                [sys.executable, str(BUNDLE_TESTS), root, str(Path(root) / "out")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("bundle_tests:", proc.stderr)


if __name__ == "__main__":
    unittest.main()


class RuntimeDllScanTest(unittest.TestCase):
    """`--dll-search-dir`: the Windows half of "a bundle runs where nothing is installed".

    soldr's mingw-w64 links mimalloc.dll against libgcc_s_seh-1.dll, which no
    `windows-latest` runner has. A missing DLL is a silent 0xC0000135 exit, not a test
    failure that explains itself -- so the scan is a hard gate, not a convenience.

    A fake objdump keeps these tests runnable on any host: the parser, the system-DLL
    allowlist and the transitive closure are the logic worth pinning, not binutils.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _fake_objdump(self, imports: dict[str, list[str]]) -> str:
        """An `objdump -p` stand-in that reports `imports[basename]` for its argument."""
        script = self.root / ("objdump.py" if os.name != "nt" else "objdump.bat")
        table = json.dumps(imports)
        script.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            f"table = json.loads({table!r})\n"
            "name = Path(sys.argv[-1]).name\n"
            "print('  DLL name: ' + name)\n"
            "for dll in table.get(name, []):\n"
            "    print('\\tDLL Name: ' + dll)\n",
            encoding="utf-8",
        )
        return f"{sys.executable} {script}"

    def _launcher(self, imports: dict[str, list[str]]) -> str:
        """`resolve_runtime_dlls` takes one program, so wrap interpreter + script."""
        command = self._fake_objdump(imports).split()
        launcher = self.root / "objdump.sh"
        launcher.write_text("#!/bin/sh\nexec " + " ".join(command) + ' "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        return str(launcher)

    def _run(
        self, imports: dict[str, list[str]], staged: list[str], available: list[str]
    ) -> list[Path]:
        build = self.root / "build"
        build.mkdir(exist_ok=True)
        lib = self.root / "toolchain"
        lib.mkdir(exist_ok=True)
        for name in staged:
            (build / name).write_bytes(b"MZ")
        for name in available:
            (lib / name).write_bytes(b"MZ")
        return bundle_tests.resolve_runtime_dlls(
            [build / name for name in staged], [lib], self._launcher(imports)
        )

    def test_system_dlls_are_never_carried(self) -> None:
        found = self._run(
            {"t.exe": ["KERNEL32.dll", "api-ms-win-crt-heap-l1-1-0.dll", "ntdll.dll"]},
            ["t.exe"],
            [],
        )
        self.assertEqual(found, [])

    def test_a_toolchain_dll_is_found_transitively(self) -> None:
        # t.exe -> mimalloc.dll (already in the bundle) -> libgcc_s_seh-1.dll (not).
        # Scanning only the executables would miss the one DLL the runner lacks.
        found = self._run(
            {
                "t.exe": ["mimalloc.dll", "KERNEL32.dll"],
                "mimalloc.dll": ["libgcc_s_seh-1.dll"],
                "libgcc_s_seh-1.dll": ["KERNEL32.dll"],
            },
            ["t.exe", "mimalloc.dll"],
            ["libgcc_s_seh-1.dll"],
        )
        self.assertEqual([path.name for path in found], ["libgcc_s_seh-1.dll"])

    def test_an_unresolvable_import_is_an_error_naming_the_importer(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            self._run({"t.exe": ["libwinpthread-1.dll"]}, ["t.exe"], [])
        message = str(caught.exception)
        self.assertIn("t.exe imports libwinpthread-1.dll", message)

    def test_import_names_match_case_insensitively(self) -> None:
        found = self._run({"t.exe": ["LIBGCC_S_SEH-1.DLL"]}, ["t.exe"], ["libgcc_s_seh-1.dll"])
        self.assertEqual([path.name for path in found], ["libgcc_s_seh-1.dll"])

    def test_the_export_directory_line_is_not_read_as_an_import(self) -> None:
        # objdump prints the module's own name as `DLL name:` (lower-case n) and each
        # import as `DLL Name:`. Reading the first as an import would make every DLL
        # appear to import itself, and the scan would then never terminate on a bundle
        # that carries it. The fake objdump emits both shapes for mimalloc.dll.
        build = self.root / "build"
        build.mkdir(exist_ok=True)
        (build / "mimalloc.dll").write_bytes(b"MZ")
        launcher = self._launcher({"mimalloc.dll": ["KERNEL32.dll"]})
        self.assertEqual(
            bundle_tests.read_pe_imports(build / "mimalloc.dll", launcher), ["KERNEL32.dll"]
        )

    def test_import_libraries_are_not_scanned(self) -> None:
        found = self._run({"libmimalloc.dll.a": ["nope.dll"]}, ["libmimalloc.dll.a"], [])
        self.assertEqual(found, [])

    # -- the clang-cl lane (#277 phase D) ------------------------------------------------

    def test_the_scan_runs_with_no_search_dir_and_still_rejects(self) -> None:
        # The MSVC bundles have no toolchain DLL to ship, so there is no directory to
        # point at -- but "this bundle can load" is still worth asserting, which is what
        # --check-dll-closure buys. An empty search list must not turn the scan into a
        # no-op that reports success on a bundle that cannot start.
        with self.assertRaises(bundle_tests.BundleError) as caught:
            self._run_no_dirs({"t.exe": ["something-else.dll"]}, ["t.exe"])
        self.assertIn("t.exe imports something-else.dll", str(caught.exception))

    def test_msvc_runtime_dlls_are_rejected_by_default(self) -> None:
        # VCRUNTIME140.dll is not part of Windows. Defaulting to "assume it is there"
        # would let the win-gnu lane silently acquire a dependency it cannot satisfy.
        with self.assertRaises(bundle_tests.BundleError) as caught:
            self._run_no_dirs({"t.exe": ["VCRUNTIME140.dll"]}, ["t.exe"])
        self.assertIn("t.exe imports VCRUNTIME140.dll", str(caught.exception))

    def test_msvc_runtime_dlls_are_accepted_when_the_lane_says_so(self) -> None:
        found = self._run_no_dirs(
            {"t.exe": ["VCRUNTIME140.dll", "MSVCP140.dll", "KERNEL32.dll"]},
            ["t.exe"],
            allow_msvc_runtime=True,
        )
        # Accepted means "not carried", not "copied in": there is nothing to copy.
        self.assertEqual(found, [])

    def test_allowing_the_msvc_runtime_does_not_widen_anything_else(self) -> None:
        with self.assertRaises(bundle_tests.BundleError) as caught:
            self._run_no_dirs({"t.exe": ["libgcc_s_seh-1.dll"]}, ["t.exe"], allow_msvc_runtime=True)
        self.assertIn("t.exe imports libgcc_s_seh-1.dll", str(caught.exception))

    def _run_no_dirs(
        self,
        imports: dict[str, list[str]],
        staged: list[str],
        allow_msvc_runtime: bool = False,
    ) -> list[Path]:
        build = self.root / "build"
        build.mkdir(exist_ok=True)
        for name in staged:
            (build / name).write_bytes(b"MZ")
        return bundle_tests.resolve_runtime_dlls(
            [build / name for name in staged],
            [],
            self._launcher(imports),
            allow_msvc_runtime,
        )
