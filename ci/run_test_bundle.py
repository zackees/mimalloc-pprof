#!/usr/bin/env -S uv run --script
"""Replay a test bundle produced by `ci/bundle_tests.py`, serially, with no CMake.

Issue #277 phase A. This is the half that runs on the macOS or Windows runner: given only
`uv` and the bundle directory -- no CMake, no repo checkout, no build tree -- it executes
every test in `tests.json` and reports in ctest's own shape so the output is comparable at
a glance.

    uv run ci/run_test_bundle.py <bundle> [--only NAME ...] [--env K=V ...]
                                          [--timeout-scale F] [--junit out.xml]
                                          [--compare-junit ctest.xml]

The three semantics that are not just "run it and check the exit code":

  * `expect_nonzero` -- the negative controls lowered from test/run-negative.cmake must
    fail. A zero exit is a test failure.
  * `expect_text` -- with it, the expected substring must appear in the combined
    stdout+stderr, so a control that fails for the *wrong* reason is still red.
  * a timeout is always a failure, `expect_nonzero` included. run-negative.cmake says so
    in as many words ("timed out instead of failing fast"), and a hung negative control
    proves nothing.

`--compare-junit` takes the JUnit XML that a normal `ctest --output-junit` run wrote and
asserts the bundle ran the same set of test names with the same pass/fail per test. That
comparison is the acceptance criterion for phase A, and it is what the `bundle-roundtrip`
job in c-unit.yml runs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from xml.sax.saxutils import escape, quoteattr

MANIFEST_NAME = "tests.json"
BUNDLE_PLACEHOLDER = "${BUNDLE}"

#: Loader search paths, per platform. The bundle is prepended to all of them because the
#: executables were linked in a build tree and carry an RPATH pointing at it; without this
#: a bundle "passes" on the build machine by loading the original library and fails
#: everywhere else, which would make the roundtrip test a lie.
_LOADER_PATH_VARS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "PATH")


@dataclass(frozen=True)
class TestSpec:
    name: str
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    timeout: float
    expect_nonzero: bool
    expect_text: str | None
    labels: tuple[str, ...]


@dataclass
class TestResult:
    name: str
    passed: bool
    seconds: float
    reason: str
    output: str

    @property
    def status(self) -> str:
        return "Passed" if self.passed else "Failed"


def _as_object(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _as_array(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _str_list(value: object) -> list[str]:
    return [item for item in _as_array(value) if isinstance(item, str)]


def load_manifest(bundle: Path) -> list[TestSpec]:
    payload = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    root = _as_object(payload)
    if root is None:
        raise ValueError(f"{bundle / MANIFEST_NAME} is not a JSON object")
    specs: list[TestSpec] = []
    for raw in _as_array(root.get("tests")):
        entry = _as_object(raw)
        if entry is None:
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        raw_env = _as_object(entry.get("env")) or {}
        env = {key: value for key, value in raw_env.items() if isinstance(value, str)}
        raw_timeout = entry.get("timeout")
        timeout = (
            float(raw_timeout)
            if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool)
            else 1500.0
        )
        cwd = entry.get("cwd")
        expect_text = entry.get("expect_text")
        specs.append(
            TestSpec(
                name=name,
                argv=tuple(_str_list(entry.get("argv"))),
                env=env,
                cwd=cwd if isinstance(cwd, str) else BUNDLE_PLACEHOLDER,
                timeout=timeout,
                expect_nonzero=entry.get("expect_nonzero") is True,
                expect_text=expect_text if isinstance(expect_text, str) else None,
                labels=tuple(_str_list(entry.get("labels"))),
            )
        )
    return specs


def _decode(stream: object) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream if isinstance(stream, str) else ""


def expand(value: str, bundle: Path) -> str:
    return value.replace(BUNDLE_PLACEHOLDER, str(bundle))


def build_environment(
    spec: TestSpec, bundle: Path, extra: dict[str, str], base: dict[str, str]
) -> dict[str, str]:
    env = dict(base)
    for key, value in spec.env.items():
        env[key] = expand(value, bundle)
    env.update(extra)
    for variable in _LOADER_PATH_VARS:
        current = env.get(variable)
        env[variable] = str(bundle) if not current else f"{bundle}{os.pathsep}{current}"
    return env


def run_one(
    spec: TestSpec, bundle: Path, extra_env: dict[str, str], timeout_scale: float
) -> TestResult:
    argv = [expand(item, bundle) for item in spec.argv]
    cwd = Path(expand(spec.cwd, bundle))
    env = build_environment(spec, bundle, extra_env, dict(os.environ))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=spec.timeout * timeout_scale,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        # Keep whatever the hung process managed to say -- for a negative control that is
        # usually the only clue to why it hung instead of aborting.
        text = _decode(exc.stdout) + _decode(exc.stderr)
        # Deliberately a failure even for a negative control: run-negative.cmake calls this
        # out explicitly as "timed out instead of failing fast".
        return TestResult(
            spec.name, False, elapsed, f"timed out after {spec.timeout * timeout_scale:g}s", text
        )
    except OSError as exc:
        return TestResult(
            spec.name, False, time.monotonic() - started, f"cannot execute: {exc}", ""
        )

    elapsed = time.monotonic() - started
    combined = proc.stdout + proc.stderr
    if spec.expect_nonzero:
        if proc.returncode == 0:
            return TestResult(
                spec.name, False, elapsed, "expected a non-zero exit, got 0", combined
            )
    elif proc.returncode != 0:
        return TestResult(spec.name, False, elapsed, f"exit code {proc.returncode}", combined)
    if spec.expect_text is not None and spec.expect_text not in combined:
        return TestResult(
            spec.name,
            False,
            elapsed,
            f"expected text {spec.expect_text!r} not found in output",
            combined,
        )
    return TestResult(spec.name, True, elapsed, "", combined)


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def format_progress(index: int, total: int, result: TestResult, width: int) -> str:
    """A line shaped like ctest's, so the two outputs can be read side by side."""
    counter = f"{index}/{total}"
    label = f"{result.name} "
    dots = "." * max(3, width - len(result.name) + 3)
    return (
        f"{counter:>7} Test #{index}: {label}{dots} {result.status:>6}  {result.seconds:6.2f} sec"
    )


def write_junit(path: Path, results: Sequence[TestResult]) -> None:
    failures = sum(1 for result in results if not result.passed)
    total_time = sum(result.seconds for result in results)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr("test-bundle")} tests="{len(results)}" '
        f'failures="{failures}" disabled="0" skipped="0" time="{total_time:.6f}">',
    ]
    for result in results:
        status = "run" if result.passed else "fail"
        lines.append(
            f"  <testcase name={quoteattr(result.name)} classname={quoteattr(result.name)} "
            f'time="{result.seconds:.6f}" status="{status}">'
        )
        if not result.passed:
            lines.append(f"    <failure message={quoteattr(result.reason)}/>")
        lines.append(f"    <system-out>{escape(result.output)}</system-out>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_junit(path: Path) -> dict[str, bool]:
    """Read a ctest (or our own) JUnit file into {test name: passed}.

    ctest marks a passing case `status="run"` with no `<failure>` child and a failing one
    `status="fail"` with one; a `<skipped>` child means it never ran. Presence of a
    `<failure>`/`<error>` child is the authoritative signal, with `status` as the fallback.
    """
    tree = ElementTree.parse(path)
    outcomes: dict[str, bool] = {}
    for case in tree.getroot().iter("testcase"):
        name = case.get("name")
        if name is None:
            continue
        status = case.get("status")
        if case.find("skipped") is not None or status in ("disabled", "notrun"):
            # ctest did not execute it, so there is nothing for the bundle to agree with.
            continue
        failed = case.find("failure") is not None or case.find("error") is not None
        if not failed and status not in (None, "run"):
            failed = True
        outcomes[name] = not failed
    return outcomes


def compare_with_junit(reference: Path, results: Sequence[TestResult]) -> list[str]:
    """Return the differences between a ctest run and this bundle run. Empty means equal."""
    expected = parse_junit(reference)
    actual = {result.name: result.passed for result in results}
    problems: list[str] = []
    for name in sorted(set(expected) - set(actual)):
        problems.append(f"{name}: ctest ran it, the bundle does not contain it")
    for name in sorted(set(actual) - set(expected)):
        problems.append(f"{name}: the bundle ran it, ctest did not")
    for name in sorted(set(expected) & set(actual)):
        if expected[name] != actual[name]:
            problems.append(
                f"{name}: ctest says {'pass' if expected[name] else 'fail'}, "
                f"the bundle says {'pass' if actual[name] else 'fail'}"
            )
    return problems


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--only", nargs="+", default=None, metavar="NAME", help="run these tests")
    parser.add_argument(
        "--env", nargs="+", default=None, metavar="K=V", help="extra environment for every test"
    )
    parser.add_argument("--timeout-scale", type=float, default=1.0)
    parser.add_argument("--junit", type=Path, default=None, metavar="FILE")
    parser.add_argument(
        "--compare-junit",
        type=Path,
        default=None,
        metavar="FILE",
        help="assert the same test names and pass/fail as this ctest --output-junit file",
    )
    args = parser.parse_args(argv)

    bundle = Path(cast("Path", args.bundle)).resolve()
    specs = load_manifest(bundle)
    only = cast("list[str] | None", args.only)
    if only is not None:
        wanted = set(only)
        missing = sorted(wanted - {spec.name for spec in specs})
        if missing:
            print(f"no such test(s) in the bundle: {missing}", file=sys.stderr)
            return 1
        specs = [spec for spec in specs if spec.name in wanted]

    extra_env: dict[str, str] = {}
    for assignment in cast("list[str] | None", args.env) or []:
        key, separator, value = assignment.partition("=")
        if not separator or not key:
            print(f"--env expects K=V, got {assignment!r}", file=sys.stderr)
            return 1
        extra_env[key] = value

    scale = float(cast("float", args.timeout_scale))
    width = max((len(spec.name) for spec in specs), default=10) + 6
    print(f"Test bundle: {bundle}  ({platform.system()} {platform.machine()})")
    results: list[TestResult] = []
    started = time.monotonic()
    for index, spec in enumerate(specs, start=1):
        print(f"{'':>7} Start {index:>2}: {spec.name}", flush=True)
        result = run_one(spec, bundle, extra_env, scale)
        results.append(result)
        print(format_progress(index, len(specs), result, width), flush=True)
        if not result.passed:
            print(f"          reason: {result.reason}")
            tail = result.output.strip().splitlines()[-25:]
            for line in tail:
                print(f"          | {line}")
    total = time.monotonic() - started

    failed = [result for result in results if not result.passed]
    percent = 100 if not results else round(100 * (len(results) - len(failed)) / len(results))
    print()
    print(f"{percent}% tests passed, {len(failed)} tests failed out of {len(results)}")
    print(f"Total Test time (real) = {total:8.2f} sec")
    if failed:
        print()
        print("The following tests FAILED:")
        for result in failed:
            print(f"\t{result.name} ({result.reason})")

    junit = cast("Path | None", args.junit)
    if junit is not None:
        write_junit(junit, results)
        print(f"JUnit written to {junit}")

    reference = cast("Path | None", args.compare_junit)
    if reference is not None:
        problems = compare_with_junit(reference, results)
        if problems:
            print()
            print(f"bundle/ctest mismatch ({len(problems)}):", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(f"bundle matches {reference}: same {len(results)} test names, same results")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
