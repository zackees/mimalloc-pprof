#!/usr/bin/env -S uv run --script
"""Prove a test bundle runs every test the native runner's own ctest would have run.

Issue #277 phase B. The whole argument for building the macOS test binaries on Linux and
executing them from a bundle is that coverage does not go down. That claim is only worth
anything if something checks it on every run, against the machine the jobs are being taken
away from -- so `run-macos` configures the same CMake trees natively (configure only; a
tree needs no build for `ctest --show-only=json-v1` to list its suite), and this script
compares those name lists against the bundles it just executed.

Since #277 phase B2 there is no macOS runner left to enumerate a native ctest suite on, so
the macOS caller compares the *executed* x86_64 bundle against the *compile-only* arm64
bundle instead: the reference side is whichever manifest is authoritative for the suite,
and `read_test_names` never cared which shape it was handed. `--heading` and
`--names` exist so the report says what was actually compared.

A name the native tree has and the bundle does not is a hard failure: it is precisely the
"gate that reports green on less" failure mode docs/ci-gates.md exists to prevent, and it
can arise silently -- a test that CMake registers only for AppleClang, or one whose
`add_test` sits behind a check that a cross configure resolves differently.

A name the bundle has and the native tree does not is reported but not fatal: the bundle
covering more than the runner used to is the direction this issue wants.

    uv run ci/bundle_coverage.py \
        --compare "ctest (Release)" native-release.json bundle-release/tests.json \
        --compare "ctest-debug-full" native-debug.json bundle-debug/tests.json \
        --summary "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class CoverageError(Exception):
    """An input this script cannot read. Always names the file."""


def _as_object(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _as_array(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def read_junit_names(path: Path) -> set[str]:
    """Test names actually EXECUTED, from a JUnit file written by `run_test_bundle.py`.

    Issue #307 made this the candidate side in `c-unit.yml`: the run stage has no build
    tree, so what it can prove is that every name the build job's `ctest --show-only`
    listed was really executed -- a manifest only proves the bundle *contains* it.

    `classname` is read in preference to `name` because an env-variant pass is reported
    as `name [LABEL]` (the guarded config's `MIMALLOC_GUARDED_SAMPLE_RATE=1` second
    pass); the coverage question is about the underlying test, not about how many
    environments it ran under. A `skipped` case does not count as executed.
    """
    try:
        tree = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError) as exc:
        raise CoverageError(f"{path}: {exc}") from exc
    names: set[str] = set()
    for case in tree.getroot().iter("testcase"):
        if case.find("skipped") is not None:
            continue
        name = case.get("classname") or case.get("name")
        if name:
            names.add(name)
    if not names:
        raise CoverageError(f"{path}: contains no executed test names")
    return names


def read_test_names(path: Path) -> set[str]:
    """Test names from any of the three shapes this repository produces.

    `ctest --show-only=json-v1` and a bundle `tests.json` both put a `tests` array of
    objects with a `name` at the top level, which is not a coincidence -- `bundle_tests.py`
    derives one from the other -- so one reader covers both and the comparison cannot
    drift by reading them differently. A `.xml` input is read as JUnit instead (#307), so
    the candidate side can be "what actually ran" rather than "what was packaged".
    """
    if path.is_dir():
        # A directory means "the union of these" -- what c-unit.yml needs since #307's
        # follow-up split the run stage across two runners: the parallel wave writes one
        # JUnit per bundle and the serial group writes another, and coverage is only
        # meaningful over both. An empty directory is refused for the same reason an
        # empty file is: it would make every comparison trivially pass.
        names: set[str] = set()
        for child in sorted(path.iterdir()):
            if child.suffix.lower() in (".xml", ".json"):
                names |= read_test_names(child)
        if not names:
            raise CoverageError(f"{path}: no .xml/.json inputs with any test names")
        return names
    if path.suffix.lower() == ".xml":
        return read_junit_names(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageError(f"{path}: {exc}") from exc
    root = _as_object(payload)
    if root is None:
        raise CoverageError(f"{path}: not a JSON object")
    names: set[str] = set()
    for raw in _as_array(root.get("tests")):
        entry = _as_object(raw)
        name = entry.get("name") if entry else None
        if isinstance(name, str):
            names.add(name)
    if not names:
        # An empty side would make every comparison trivially pass, which is the same
        # "verifies nothing" shape this script exists to catch.
        raise CoverageError(f"{path}: contains no test names")
    return names


@dataclass(frozen=True)
class Comparison:
    label: str
    native: set[str]
    bundle: set[str]

    @property
    def missing(self) -> list[str]:
        return sorted(self.native - self.bundle)

    @property
    def extra(self) -> list[str]:
        return sorted(self.bundle - self.native)


def render(
    comparisons: Sequence[Comparison],
    heading: str = "Coverage: native ctest vs cross-built bundle",
    reference: str = "native",
    candidate: str = "bundle",
) -> str:
    lines = [
        f"### {heading}",
        "",
        f"| config | {reference} | {candidate} | missing from {candidate} | extra in {candidate} |",
        "|---|---:|---:|---|---|",
    ]
    for comparison in comparisons:
        missing = ", ".join(f"`{name}`" for name in comparison.missing) or "—"
        extra = ", ".join(f"`{name}`" for name in comparison.extra) or "—"
        lines.append(
            f"| {comparison.label} | {len(comparison.native)} | {len(comparison.bundle)} "
            f"| {missing} | {extra} |"
        )
    lines.append("")
    total_missing = sum(len(comparison.missing) for comparison in comparisons)
    if total_missing:
        lines.append(
            f"**{total_missing} test(s) present in the {reference} side are absent from "
            f"the {candidate} side.** Coverage would go down; see issue #277 §4."
        )
    else:
        lines.append(f"Every test in the {reference} side is present in the {candidate} side.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--compare",
        nargs=3,
        action="append",
        metavar=("LABEL", "REFERENCE", "CANDIDATE"),
        required=True,
        help="a reference manifest (ctest --show-only=json-v1 or a bundle tests.json) "
        "and what replaces it: another manifest, or a JUnit .xml of what actually ran",
    )
    parser.add_argument(
        "--heading",
        default="Coverage: native ctest vs cross-built bundle",
        help="markdown heading for the report",
    )
    parser.add_argument(
        "--names",
        nargs=2,
        default=["native", "bundle"],
        metavar=("REFERENCE", "CANDIDATE"),
        help="what to call the two sides in the report",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        metavar="FILE",
        help="append the markdown table here (point at $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args(argv)

    comparisons: list[Comparison] = []
    try:
        for label, native, bundle in cast("list[list[str]]", args.compare):
            comparisons.append(
                Comparison(
                    label=label,
                    native=read_test_names(Path(native)),
                    bundle=read_test_names(Path(bundle)),
                )
            )
    except CoverageError as exc:
        print(f"bundle_coverage: {exc}", file=sys.stderr)
        return 2

    reference, candidate = cast("list[str]", args.names)
    table = render(comparisons, cast("str", args.heading), reference, candidate)
    print(table, end="")
    summary = cast("Path | None", args.summary)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write(table)
    return 1 if any(comparison.missing for comparison in comparisons) else 0


if __name__ == "__main__":
    raise SystemExit(main())
