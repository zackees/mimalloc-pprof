#!/usr/bin/env -S uv run --script
"""Replay one or many test bundles produced by `ci/bundle_tests.py`, with no CMake.

Issue #277 phase A. This is the half that runs on the macOS or Windows runner: given only
`uv` and the bundle directory -- no CMake, no repo checkout, no build tree -- it executes
every test in `tests.json` and reports in ctest's own shape so the output is comparable at
a glance.

Issue #307 added the multi-bundle mode `c-unit.yml`'s run stage uses: every configuration
is built once, in parallel, by the build stage, and ONE run job then executes every
bundle at once instead of each config rebuilding the library for its own slice.

    uv run ci/run_test_bundle.py <bundle> [--only NAME ...] [--env K=V ...]
                                          [--timeout-scale F] [--junit out.xml]
                                          [--compare-junit ctest.xml]
    uv run ci/run_test_bundle.py --bundles dir1 dir2 ... --jobs 4 --junit-dir results
                                 [--env-variant LABEL K=V ...]

The semantics that are not just "run it and check the exit code":

  * `expect_nonzero` -- the negative controls lowered from test/run-negative.cmake must
    fail. A zero exit is a test failure.
  * `expect_text` -- with it, the expected substring must appear in the combined
    stdout+stderr, so a control that fails for the *wrong* reason is still red.
  * `forbid_text` -- with it, the substring must NOT appear (issue #268's
    test/run-text-check.cmake MODE=FORBID), the mirror image of `expect_text`.
  * a timeout is always a failure, `expect_nonzero` included. run-negative.cmake says so
    in as many words ("timed out instead of failing fast"), and a hung negative control
    proves nothing.

`--compare-junit` takes the JUnit XML that a normal `ctest --output-junit` run wrote and
asserts the bundle ran the same set of test names with the same pass/fail per test. That
comparison is the acceptance criterion for phase A, and it is what `verify_local.py`'s
`bundle` config runs locally. In CI the run stage has no build tree to produce a
reference from, so the equivalent check there is a *name* comparison against each build
job's `ctest --show-only=json-v1` (`ci/bundle_coverage.py`); see docs/ci-gates.md.

Concurrency (#307). Work items -- one per (bundle, test, env variant) -- are run by a
pool bounded by `--jobs`, so bundles run alongside each other AND tests run alongside
each other inside a bundle. A test the manifest marks `serial` is held back and run
afterwards one at a time with nothing else running anywhere: those are the tests that
assert on process-wide RSS or on a wall-clock deadline, where another test's threads on
the same 4-vCPU runner change what is being measured rather than merely slowing it down.
The flag comes from the test's own `RUN_SERIAL` property (or a `serial` label) in
CMakeLists.txt -- ctest's own meaning of the word -- not from a name list in this script,
so a new test declares its own scheduling requirement where it is defined.

`--env-variant LABEL K=V ...` runs every selected test a second time with that extra
environment, reported as `name [LABEL]`. It is how the guarded config's
`MIMALLOC_GUARDED_SAMPLE_RATE=1` second pass joins the same wave as everything else
instead of being a serial `ctest` re-run of the whole suite. The JUnit `classname` stays
the base test name so a coverage comparison is unaffected by variants.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    forbid_text: str | None
    labels: tuple[str, ...]
    #: Run alone, after the parallel wave, with nothing else running anywhere (#307).
    #: Set from the test's `RUN_SERIAL` property or a `serial` label in CMakeLists.txt.
    serial: bool = False


@dataclass(frozen=True)
class Variant:
    """One extra-environment pass over a bundle. The base pass is `Variant("", {})`.

    `bundles` scopes the pass. It matters: the guarded config's
    `MIMALLOC_GUARDED_SAMPLE_RATE=1` second pass is meaningful only for the bundle built
    with `MI_GUARDED=ON`, and applying it to all eight of them would double the run --
    including the serial group, which is the part that does not divide by `--jobs`. An
    empty tuple means every bundle.
    """

    label: str
    env: dict[str, str] = field(default_factory=dict[str, str])
    bundles: tuple[str, ...] = ()

    def applies_to(self, bundle_name: str) -> bool:
        return not self.bundles or bundle_name in self.bundles

    def decorate(self, name: str) -> str:
        return name if not self.label else f"{name} [{self.label}]"


BASE_VARIANT = Variant("")


@dataclass(frozen=True)
class WorkItem:
    """A single (bundle, test, variant) execution -- the unit the pool schedules."""

    bundle: Path
    bundle_name: str
    spec: TestSpec
    variant: Variant

    @property
    def display(self) -> str:
        return self.variant.decorate(self.spec.name)


@dataclass
class TestResult:
    name: str
    passed: bool
    seconds: float
    reason: str
    output: str
    #: The test name with no variant decoration -- what a coverage comparison matches on.
    classname: str = ""
    #: Which bundle produced it; "" in the single-bundle mode that predates #307.
    bundle_name: str = ""

    def __post_init__(self) -> None:
        if not self.classname:
            self.classname = self.name

    @property
    def status(self) -> str:
        return "Passed" if self.passed else "Failed"

    @property
    def qualified(self) -> str:
        return self.name if not self.bundle_name else f"{self.bundle_name}::{self.name}"


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
        forbid_text = entry.get("forbid_text")
        specs.append(
            TestSpec(
                name=name,
                argv=tuple(_str_list(entry.get("argv"))),
                env=env,
                cwd=cwd if isinstance(cwd, str) else BUNDLE_PLACEHOLDER,
                timeout=timeout,
                expect_nonzero=entry.get("expect_nonzero") is True,
                expect_text=expect_text if isinstance(expect_text, str) else None,
                forbid_text=forbid_text if isinstance(forbid_text, str) else None,
                labels=tuple(_str_list(entry.get("labels"))),
                # A bundle written before #307 has no `serial` key at all; treating that
                # as False keeps every existing bundle replayable by this runner.
                serial=entry.get("serial") is True or "serial" in _str_list(entry.get("labels")),
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
    if spec.forbid_text is not None and spec.forbid_text in combined:
        return TestResult(
            spec.name,
            False,
            elapsed,
            f"forbidden text {spec.forbid_text!r} found in output",
            combined,
        )
    return TestResult(spec.name, True, elapsed, "", combined)


def run_item(item: WorkItem, extra_env: dict[str, str], timeout_scale: float) -> TestResult:
    """Run one scheduled (bundle, test, variant) item and label the result with all three."""
    env = dict(extra_env)
    env.update(item.variant.env)
    result = run_one(item.spec, item.bundle, env, timeout_scale)
    result.name = item.display
    result.classname = item.spec.name
    result.bundle_name = item.bundle_name
    return result


# --------------------------------------------------------------------------------------
# Scheduling (#307)
# --------------------------------------------------------------------------------------


def plan(
    bundles: Sequence[tuple[str, Path, Sequence[TestSpec]]], variants: Sequence[Variant]
) -> list[WorkItem]:
    """Every (bundle, test, variant) triple, in a stable order."""
    items: list[WorkItem] = []
    for bundle_name, bundle, specs in bundles:
        for variant in variants:
            if not variant.applies_to(bundle_name):
                continue
            for spec in specs:
                items.append(WorkItem(bundle, bundle_name, spec, variant))
    return items


SELECTIONS = ("all", "parallel", "serial")


def partition(items: Sequence[WorkItem], selection: str) -> list[WorkItem]:
    """The subset of `items` a `--select` value asks for.

    `parallel` and `serial` exist so the two waves can run on *different machines*
    (c-unit.yml's `run-linux` and `run-linux-serial`). A separate runner is exclusive by
    construction, which is the same guarantee running the serial group last on one
    machine gives -- without adding its duration to the critical path.
    """
    if selection == "parallel":
        return [item for item in items if not item.spec.serial]
    if selection == "serial":
        return [item for item in items if item.spec.serial]
    return list(items)


def execute(
    items: Sequence[WorkItem], *, jobs: int, extra_env: dict[str, str], timeout_scale: float
) -> list[TestResult]:
    """Run the parallel wave `jobs`-wide, then the serial group one at a time.

    The two waves are not interleaved on purpose. A `serial` test is one whose assertion
    is about the machine (process-wide RSS) or about wall-clock (a handoff deadline), so
    running it next to anything else does not merely slow it down -- it changes what is
    measured, and the failure that produces looks like a real regression. Holding them
    back and running them alone is the mechanism that makes the parallel wave safe; it is
    the same thing ctest's own RUN_SERIAL property means, and it is where a test belongs
    when it passes serially and fails under concurrency.
    """
    parallel = [item for item in items if not item.spec.serial]
    serial = [item for item in items if item.spec.serial]
    width = max((len(item.display) for item in items), default=10) + 6
    total = len(items)
    results: list[TestResult] = []
    console = threading.Lock()
    done = 0

    def report(result: TestResult) -> None:
        nonlocal done
        done += 1
        print(format_progress(done, total, result, width), flush=True)
        if not result.passed:
            print(f"          reason: {result.reason}")
            for line in result.output.strip().splitlines()[-25:]:
                print(f"          | {line}")

    print(
        f"Scheduling {total} test run(s): {len(parallel)} in a {jobs}-wide wave, "
        f"{len(serial)} serialized afterwards."
    )
    if parallel:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = [pool.submit(run_item, item, extra_env, timeout_scale) for item in parallel]
            # as_completed, not submission order: a 20-second stress test scheduled first
            # would otherwise leave the CI log silent until it finished, which is exactly
            # when a reader needs to see what else is progressing.
            for future in as_completed(futures):
                result = future.result()
                with console:
                    results.append(result)
                    report(result)
    for item in serial:
        result = run_item(item, extra_env, timeout_scale)
        results.append(result)
        report(result)
    return results


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def format_progress(index: int, total: int, result: TestResult, width: int) -> str:
    """A line shaped like ctest's, so the two outputs can be read side by side."""
    counter = f"{index}/{total}"
    label = f"{result.qualified} "
    dots = "." * max(3, width - len(result.qualified) + 3)
    return (
        f"{counter:>7} Test #{index}: {label}{dots} {result.status:>6}  {result.seconds:6.2f} sec"
    )


#: Characters XML 1.0 does not allow at all -- not even escaped. mimalloc's own verbose
#: output carries ANSI colour codes (ESC, 0x1b), so a captured log routinely contains
#: them; before #307 nothing parsed these files back and the invalid bytes went unnoticed,
#: and `guarded.xml` then failed the coverage step with "not well-formed (invalid token)"
#: rather than reporting on coverage. Dropped rather than escaped, because there is no
#: legal escape for them.
_XML_FORBIDDEN = re.compile("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def xml_safe(text: str) -> str:
    """`text` with every character XML 1.0 forbids removed."""
    return _XML_FORBIDDEN.sub("", text)


def write_junit(path: Path, results: Sequence[TestResult], suite: str = "test-bundle") -> None:
    failures = sum(1 for result in results if not result.passed)
    total_time = sum(result.seconds for result in results)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr(suite)} tests="{len(results)}" '
        f'failures="{failures}" disabled="0" skipped="0" time="{total_time:.6f}">',
    ]
    for result in results:
        status = "run" if result.passed else "fail"
        lines.append(
            f"  <testcase name={quoteattr(xml_safe(result.name))} "
            f"classname={quoteattr(xml_safe(result.classname))} "
            f'time="{result.seconds:.6f}" status="{status}">'
        )
        if not result.passed:
            lines.append(f"    <failure message={quoteattr(xml_safe(result.reason))}/>")
        lines.append(f"    <system-out>{escape(xml_safe(result.output))}</system-out>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_junit_per_bundle(directory: Path, results: Sequence[TestResult]) -> list[Path]:
    """One JUnit file per bundle, named after it -- what `ci/bundle_coverage.py` consumes."""
    directory.mkdir(parents=True, exist_ok=True)
    by_bundle: dict[str, list[TestResult]] = {}
    for result in results:
        by_bundle.setdefault(result.bundle_name or "bundle", []).append(result)
    written: list[Path] = []
    for name, group in sorted(by_bundle.items()):
        path = directory / f"{name}.xml"
        write_junit(path, group, suite=name)
        written.append(path)
    return written


def parse_junit(path: Path) -> dict[str, bool]:
    """Read a ctest (or our own) JUnit file into {test name: passed}.

    ctest marks a passing case `status="run"` with no `<failure>` child and a failing one
    `status="fail"` with one; a `<skipped>` child means it never ran. Presence of a
    `<failure>`/`<error>` child is the authoritative signal, with `status` as the fallback.
    """
    if not path.is_file():
        # ctest resolves --output-junit against the build directory, not the working
        # directory, so a relative path silently lands somewhere else. Say that, rather
        # than raising a bare FileNotFoundError from deep inside ElementTree.
        raise FileNotFoundError(
            f"{path} does not exist. `ctest --output-junit` writes relative paths into the "
            f"build directory -- pass it an absolute path."
        )
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


def parse_assignments(items: Iterable[str], flag: str) -> dict[str, str]:
    """`K=V` tokens into a dict. Raises ValueError naming the offending token."""
    env: dict[str, str] = {}
    for assignment in items:
        key, separator, value = assignment.partition("=")
        if not separator or not key:
            raise ValueError(f"{flag} expects K=V, got {assignment!r}")
        env[key] = value
    return env


def parse_variants(raw: Sequence[Sequence[str]]) -> list[Variant]:
    """`--env-variant [BUNDLE:]LABEL K=V ...` groups, plus the always-present base pass.

    `BUNDLE:` scopes the extra pass to one bundle (repeat the flag for several). Without
    it the pass applies to every bundle, which is what a single-bundle invocation wants
    and almost never what a multi-bundle one does.
    """
    variants = [BASE_VARIANT]
    for group in raw:
        if len(group) < 2:
            raise ValueError(
                f"--env-variant needs a label and at least one K=V, got {list(group)!r}"
            )
        scope, separator, label = group[0].rpartition(":")
        if separator and not scope:
            raise ValueError(f"--env-variant {group[0]!r} has an empty bundle name")
        if not label:
            raise ValueError(f"--env-variant {group[0]!r} has an empty label")
        variants.append(
            Variant(
                label,
                parse_assignments(group[1:], "--env-variant"),
                (scope,) if separator else (),
            )
        )
    return variants


def select(specs: Sequence[TestSpec], only: Sequence[str] | None) -> list[TestSpec]:
    if only is None:
        return list(specs)
    wanted = set(only)
    return [spec for spec in specs if spec.name in wanted]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("bundle", type=Path, nargs="?", default=None)
    parser.add_argument(
        "--bundles",
        nargs="+",
        default=None,
        metavar="DIR",
        type=Path,
        help="run several bundles in one wave (#307). Mutually exclusive with the "
        "positional argument.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="how many tests may run at once across all bundles (default 1). Tests the "
        "manifest marks `serial` always run alone, after the parallel wave.",
    )
    parser.add_argument("--only", nargs="+", default=None, metavar="NAME", help="run these tests")
    parser.add_argument(
        "--env", nargs="+", default=None, metavar="K=V", help="extra environment for every test"
    )
    parser.add_argument(
        "--env-variant",
        nargs="+",
        action="append",
        default=None,
        metavar=("BUNDLE:LABEL", "K=V"),
        help="run every selected test a second time with this extra environment, "
        "reported as `name [LABEL]`. Prefix the label with `<bundle>:` to scope the "
        "extra pass to one bundle (repeatable).",
    )
    parser.add_argument(
        "--select",
        choices=SELECTIONS,
        default="all",
        help="run only the parallel-safe tests, only the `serial` ones, or both "
        "(default). The two halves are split across runners in c-unit.yml so the serial "
        "group still has a machine to itself without lengthening the critical path.",
    )
    parser.add_argument("--timeout-scale", type=float, default=1.0)
    parser.add_argument("--junit", type=Path, default=None, metavar="FILE")
    parser.add_argument(
        "--junit-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="write one JUnit file per bundle here, named <bundle>.xml",
    )
    parser.add_argument(
        "--compare-junit",
        type=Path,
        default=None,
        metavar="FILE",
        help="assert the same test names and pass/fail as this ctest --output-junit file",
    )
    args = parser.parse_args(argv)

    positional = cast("Path | None", args.bundle)
    listed = cast("list[Path] | None", args.bundles)
    if (positional is None) == (listed is None):
        print("pass exactly one of <bundle> or --bundles DIR ...", file=sys.stderr)
        return 1
    paths = [positional] if positional is not None else list(listed or [])
    multi = listed is not None

    try:
        variants = parse_variants(cast("list[list[str]]", args.env_variant) or [])
        extra_env = parse_assignments(cast("list[str] | None", args.env) or [], "--env")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    reference = cast("Path | None", args.compare_junit)
    if reference is not None and (multi or len(variants) > 1):
        # The comparison is name-and-outcome against one ctest run; with several bundles
        # or an env variant there is no single reference run it could mean. CI compares
        # names per bundle with ci/bundle_coverage.py instead.
        print(
            "--compare-junit applies to a single bundle with no --env-variant "
            "(see ci/bundle_coverage.py for the multi-bundle equivalent)",
            file=sys.stderr,
        )
        return 1

    only = cast("list[str] | None", args.only)
    loaded: list[tuple[str, Path, Sequence[TestSpec]]] = []
    for raw_path in paths:
        bundle = Path(raw_path).resolve()
        try:
            specs = load_manifest(bundle)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"cannot read {bundle}: {exc}", file=sys.stderr)
            return 1
        if only is not None:
            missing = sorted(set(only) - {spec.name for spec in specs})
            if missing and not multi:
                print(f"no such test(s) in the bundle: {missing}", file=sys.stderr)
                return 1
        loaded.append((bundle.name, bundle, select(specs, only)))

    # A `--env-variant guarded:...` whose bundle name is a typo would silently drop the
    # second pass and still report green -- the "gate that verifies nothing" shape. Refuse.
    known = {name for name, _, _ in loaded}
    unknown = sorted({b for variant in variants for b in variant.bundles} - known)
    if unknown:
        print(
            f"--env-variant is scoped to bundle(s) {unknown} that were not given; "
            f"have {sorted(known)}",
            file=sys.stderr,
        )
        return 1

    selection = cast("str", args.select)
    items = partition(plan(loaded, variants), selection)
    if not items:
        # An empty run reports 100% passed and proves nothing -- the exact failure mode
        # docs/ci-gates.md exists to prevent.
        print(
            f"no tests selected (--select {selection}); refusing to report a green run on nothing",
            file=sys.stderr,
        )
        return 1

    print(f"Test bundles: {', '.join(name for name, _, _ in loaded)}  (--select {selection})")
    print(f"Host: {platform.system()} {platform.machine()}")
    started = time.monotonic()
    results = execute(
        items,
        jobs=int(cast("int", args.jobs)),
        extra_env=extra_env,
        timeout_scale=float(cast("float", args.timeout_scale)),
    )
    total = time.monotonic() - started

    failed = [result for result in results if not result.passed]
    percent = 100 if not results else round(100 * (len(results) - len(failed)) / len(results))
    print()
    print(f"{percent}% tests passed, {len(failed)} tests failed out of {len(results)}")
    print(f"Total Test time (real) = {total:8.2f} sec")
    if multi:
        print()
        print("Per bundle:")
        for name, _, _ in loaded:
            group = [result for result in results if result.bundle_name == name]
            bad = sum(1 for result in group if not result.passed)
            print(f"  {name:<28} {len(group) - bad}/{len(group)} passed")
    if failed:
        print()
        print("The following tests FAILED:")
        for result in failed:
            print(f"\t{result.qualified} ({result.reason})")

    junit = cast("Path | None", args.junit)
    if junit is not None:
        write_junit(junit, results)
        print(f"JUnit written to {junit}")
    junit_dir = cast("Path | None", args.junit_dir)
    if junit_dir is not None:
        for path in write_junit_per_bundle(junit_dir, results):
            print(f"JUnit written to {path}")

    if reference is not None:
        try:
            problems = compare_with_junit(reference, results)
        except (FileNotFoundError, ElementTree.ParseError) as exc:
            print(f"cannot read the ctest reference: {exc}", file=sys.stderr)
            return 1
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
