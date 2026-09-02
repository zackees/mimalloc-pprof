#!/usr/bin/env -S uv run --script
"""Fail if any workflow would schedule a job onto a native macOS runner.

Issue #277 phase B2. The owner's requirement is absolute -- "not one mac build may run on
a native mac device" -- and a requirement that nothing checks is a requirement that comes
back. Both Apple architectures are cross-built on Linux (see
cmake/toolchains/soldr-*-apple-darwin.cmake) and the x86_64 bundle is executed inside a
dockurr/macos guest on a Linux runner, so a macOS runner label reappearing in a workflow
is always a regression, never a deliberate exception.

    uv run ci/lint_no_macos_runners.py [.github/workflows]

Why this is not a one-line `grep`: the workflows discuss macOS constantly -- job names,
`runner.os != 'macOS'` conditions, and long comments explaining why the macOS runners were
removed. A grep for "macos" matches its own explanation and would have to be muzzled until
it matched nothing at all. So this parses the YAML and looks at the two places a runner
label can actually appear -- `runs-on`, and the matrix values it interpolates -- and
ignores everything else. Comments and `if:` conditions are invisible to it by
construction.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import yaml

#: `macos-latest`, `macos-14`, `macos-15-intel`, `macOS-latest`, `macos-13-xlarge`, and
#: any future suffixed variant. Anchored on the label shape, not on the word "macos", so
#: prose like "the macOS runners" does not match.
MACOS_LABEL = re.compile(r"\bmacos-(?:latest|\d+)[\w.-]*\b", re.IGNORECASE)


def _walk(node: object, path: str = "") -> Iterator[tuple[str, object]]:
    yield path, node
    if isinstance(node, dict):
        for key, value in cast("dict[str, object]", node).items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(cast("list[object]", node)):
            yield from _walk(value, f"{path}[{index}]")


def _strings(node: object) -> Iterator[str]:
    """Every scalar under `node`, so a label buried in a matrix list is still seen."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in cast("dict[str, object]", node).values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in cast("list[object]", node):
            yield from _strings(value)


def offenders(document: object) -> Iterator[tuple[str, str]]:
    """(yaml path, offending label) for every runner label in a scheduling position."""
    for path, node in _walk(document):
        tail = path.rsplit(".", 1)[-1].split("[", 1)[0]
        # `runs-on:` is where a label is used. `strategy.matrix` is where it is *defined*
        # -- including under `include:` -- and a matrix value only ever reaches a job
        # through an interpolation, so both have to be checked. Checking `runs-on` alone
        # would miss `runner: macos-15-intel` feeding `runs-on: ${{ matrix.runner }}`,
        # which is exactly the shape cross.yml used.
        if tail not in {"runs-on", "matrix"}:
            continue
        for text in _strings(node):
            for match in cast("list[str]", MACOS_LABEL.findall(text)):
                yield path, match


def check(root: Path) -> int:
    files = sorted(list(root.glob("*.yml")) + list(root.glob("*.yaml")))
    if not files:
        # An empty scan passes trivially, which is the same "verifies nothing" failure
        # this lint exists to prevent.
        print(f"lint_no_macos_runners: no workflows found under {root}", file=sys.stderr)
        return 2
    failures = 0
    for file in files:
        try:
            document = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"lint_no_macos_runners: {file}: {exc}", file=sys.stderr)
            return 2
        for path, label in offenders(document):
            print(f"{file}: {path}: native macOS runner label {label!r}", file=sys.stderr)
            failures += 1
    if failures:
        print(
            f"\n{failures} native macOS runner label(s). Issue #277 phase B2 removed every\n"
            "one of them: both Apple architectures are cross-built on Linux through soldr,\n"
            "and the x86_64 bundle is executed under dockurr/macos on a Linux runner.\n"
            "See docs/ci-gates.md, section 'macOS'.",
            file=sys.stderr,
        )
        return 1
    print(f"lint_no_macos_runners: {len(files)} workflows, no macOS runner labels")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1] / ".github/workflows"
    return check(root)


if __name__ == "__main__":
    raise SystemExit(main())
