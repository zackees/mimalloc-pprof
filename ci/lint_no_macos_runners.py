#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""Fail if a workflow schedules a native macOS runner outside the full PR/release lane.

Issue #277 phase B2 originally prohibited all native Mac jobs. Issue #444 narrowly
permits hosted Intel and Apple Silicon runners for full validation. Issue #463 also
selects that lane for external PR authors; internal minimal PRs and main stay Mac-free.
Both Apple architectures are still cross-built on Linux
(see cmake/toolchains/soldr-*-apple-darwin.cmake), and this lint permits only the reviewed
full-lane matrix and its exact opt-in condition. Any other Mac runner is a regression.

    uv run ci/lint_no_macos_runners.py [.github/workflows] [azure-pipelines.yml] ...

Paths may be directories (every *.yml/*.yaml inside) or individual files, so the inherited
`azure-pipelines.yml` is covered too -- it carried macOS-14/macOS-15 jobs, and a rule that
only looked at `.github/workflows` would have called that clean.

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
#: any future suffixed variant, plus Azure Pipelines' `macOS-14` vmImage spelling.
#: Anchored on the label shape, not on the word "macos", so prose like "the macOS runners"
#: does not match.
MACOS_LABEL = re.compile(r"\bmacos-(?:latest|\d+)[\w.-]*\b", re.IGNORECASE)

#: A self-hosted runner is selected by a LIST of bare labels -- `runs-on: [self-hosted,
#: macOS, X64]` -- where the macOS token carries no `-latest`/`-14` suffix at all and
#: MACOS_LABEL cannot see it. Matched only inside a `runs-on`/`vmImage`/`pool` value,
#: never in prose, so the surrounding key is what makes this safe.
MACOS_BARE = re.compile(r"^macos$", re.IGNORECASE)

#: `runs-on: ${{ vars.RUNNER }}` and `uses: other-org/repo/.github/workflows/x.yml@v1`
#: choose a runner somewhere this script cannot see. Not failures -- they may be entirely
#: legitimate -- but they are the two ways a macOS runner could come back without any
#: label appearing in this repository, so they are reported rather than ignored.
UNVERIFIABLE = re.compile(r"\$\{\{\s*(?:vars|secrets|inputs)\.", re.IGNORECASE)


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


#: Keys whose value selects a runner. `runs-on` is GitHub's; `vmImage`/`pool` are Azure
#: Pipelines'; `matrix` is where a label is *defined* before an interpolation carries it
#: into `runs-on`, which is exactly the shape cross.yml used
#: (`runner: macos-15-intel` feeding `runs-on: ${{ matrix.runner }}`).
SCHEDULING_KEYS = frozenset({"runs-on", "matrix", "vmImage", "pool"})


def _scheduling_nodes(document: object) -> Iterator[tuple[str, object]]:
    """Each scheduling key's node, once.

    `_walk` yields every descendant, so a naive `tail in SCHEDULING_KEYS` test fires twice
    for the same label: once for `runs-on` and again for `runs-on[1]`, and once for `pool`
    and again for the `vmImage` nested inside it. Since the callers recurse into the node
    themselves with `_strings`, the outermost match is the only one wanted -- so once a
    subtree has been claimed, everything under it is skipped.
    """
    claimed: list[str] = []
    for path, node in _walk(document):
        if any(path == c or path.startswith(c + ".") or path.startswith(c + "[") for c in claimed):
            continue
        if path.rsplit(".", 1)[-1].split("[", 1)[0] in SCHEDULING_KEYS:
            claimed.append(path)
            yield path, node


def offenders(document: object) -> Iterator[tuple[str, str]]:
    """(yaml path, offending label) for every runner label in a scheduling position."""
    for path, node in _scheduling_nodes(document):
        for text in _strings(node):
            for match in cast("list[str]", MACOS_LABEL.findall(text)):
                yield path, match
            # A bare `macOS` label only means "a runner" inside one of these keys, which
            # is why this check lives here and not in MACOS_LABEL: matching /^macos$/
            # anywhere would hit every job name and half the comments.
            if MACOS_BARE.match(text.strip()):
                yield path, text.strip()


def allowed_full_runner(file: Path, document: object, path: str, label: str) -> bool:
    """The only owner-approved hosted Mac exception: both arches in one full job."""
    if file.name == "auto-release.yml" and path == "jobs.test-shipped-assets.strategy.matrix":
        jobs = cast("dict[str, object]", cast("dict[str, object]", document).get("jobs", {}))
        job = cast("dict[str, object]", jobs.get("test-shipped-assets", {}))
        matrix = cast(
            "dict[str, object]",
            cast("dict[str, object]", job.get("strategy", {})).get("matrix", {}),
        )
        return (
            label in {"macos-15", "macos-15-intel"}
            and job.get("runs-on") == "${{ matrix.runner }}"
            and job.get("needs") == ["build-binaries"]
            and matrix.get("include")
            == [
                {"asset": "macos-arm64", "runner": "macos-15", "extension": "tar.gz"},
                {"asset": "macos-x86_64", "runner": "macos-15-intel", "extension": "tar.gz"},
                {"asset": "windows-x64-gnu", "runner": "windows-latest", "extension": "zip"},
                {"asset": "windows-x64-msvc", "runner": "windows-latest", "extension": "zip"},
            ]
        )
    if file.name == "auto-release.yml" and path == "jobs.smoke-shipped-assets.strategy.matrix":
        jobs = cast("dict[str, object]", cast("dict[str, object]", document).get("jobs", {}))
        job = cast("dict[str, object]", jobs.get("smoke-shipped-assets", {}))
        preflight = cast("dict[str, object]", jobs.get("preflight-assets", {}))
        release = cast("dict[str, object]", jobs.get("release", {}))
        matrix = cast(
            "dict[str, object]",
            cast("dict[str, object]", job.get("strategy", {})).get("matrix", {}),
        )
        return (
            label in {"macos-15", "macos-15-intel"}
            and job.get("runs-on") == "${{ matrix.runner }}"
            and "if" not in job
            and job.get("needs") == ["preflight-assets"]
            and preflight.get("needs")
            == ["build-and-package", "build-binaries", "test-shipped-assets"]
            and release.get("needs") == ["preflight-assets", "smoke-shipped-assets"]
            and matrix.get("include")
            == [
                {"asset": "macos-arm64", "runner": "macos-15"},
                {"asset": "macos-x86_64", "runner": "macos-15-intel"},
                {"asset": "windows-x64-gnu", "runner": "windows-latest"},
                {"asset": "windows-x64-msvc", "runner": "windows-latest"},
            ]
        )
    if file.name != "macos-bundles.yml" or label not in {"macos-15", "macos-15-intel"}:
        return False
    if path != "jobs.run-macos-native-full.strategy.matrix":
        return False
    if not isinstance(document, dict):
        return False
    jobs = cast("dict[str, object]", document).get("jobs")
    if not isinstance(jobs, dict):
        return False
    job = cast("dict[str, object]", jobs).get("run-macos-native-full")
    if not isinstance(job, dict):
        return False
    fields = cast("dict[str, object]", job)
    if fields.get("runs-on") != "${{ matrix.runner }}":
        return False
    gate = fields.get("if")
    if not isinstance(gate, str):
        return False
    expected = (
        "(github.event_name == 'pull_request' && "
        "(contains(github.event.pull_request.labels.*.name, 'ci-full') || "
        "needs.pr-ci-mode.outputs.external == 'true')) || "
        "(github.event_name == 'workflow_dispatch' && inputs.ci-mode == 'full')"
    )
    strategy = fields.get("strategy")
    if not isinstance(strategy, dict):
        return False
    matrix = cast("dict[str, object]", strategy).get("matrix")
    if not isinstance(matrix, dict):
        return False
    include = cast("dict[str, object]", matrix).get("include")
    needs = fields.get("needs")
    return (
        " ".join(gate.split()) == expected
        and isinstance(needs, list)
        and "pr-ci-mode" in cast("list[object]", needs)
        and include
        == [
            {"arch": "arm64", "runner": "macos-15", "triple": "aarch64-apple-darwin"},
            {"arch": "x64", "runner": "macos-15-intel", "triple": "x86_64-apple-darwin"},
        ]
    )


def unverifiable(document: object) -> Iterator[tuple[str, str]]:
    """(yaml path, expression) for runner choices this script cannot resolve.

    Not failures. `runs-on: ${{ vars.RUNNER }}` and a reusable workflow in another
    repository both pick a runner somewhere outside this file, so a clean scan says
    nothing about them -- and they are the two ways a macOS runner could come back with no
    label ever appearing here. Reporting them keeps the lint's claim honest: "no macOS
    label in this repository", not "no macOS runner can possibly run".
    """
    for path, node in _scheduling_nodes(document):
        for text in _strings(node):
            # `${{ matrix.* }}` resolves inside this same file and the matrix itself is a
            # scheduling key, so it is already covered and is not unverifiable.
            if UNVERIFIABLE.search(text) and "matrix." not in text:
                yield path, text.strip()
    for path, node in _walk(document):
        if path.rsplit(".", 1)[-1].split("[", 1)[0] != "uses" or not isinstance(node, str):
            continue
        # Strip the `@ref` a `uses:` always carries before looking at the extension --
        # `.../build.yml@v1` does not end in `.yml`.
        target = node.split("@", 1)[0]
        # A local reusable workflow (`./.github/workflows/x.yml`) is scanned like any
        # other file; one from another repository is not ours to see.
        if target.endswith((".yml", ".yaml")) and not target.startswith("./"):
            yield path, node


def _yaml_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(list(target.glob("*.yml")) + list(target.glob("*.yaml")))
    return [target] if target.is_file() else []


def check(*targets: Path) -> int:
    """Scan every target; 0 clean, 1 a macOS runner label, 2 nothing scanned / bad YAML."""
    files: list[Path] = []
    for target in targets:
        files.extend(_yaml_files(target))
    if not files:
        # An empty scan passes trivially, which is the same "verifies nothing" failure
        # this lint exists to prevent.
        print(
            f"lint_no_macos_runners: no YAML found under {', '.join(str(t) for t in targets)}",
            file=sys.stderr,
        )
        return 2
    failures = 0
    warnings = 0
    for file in files:
        try:
            document = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"lint_no_macos_runners: {file}: {exc}", file=sys.stderr)
            return 2
        for path, label in offenders(document):
            if allowed_full_runner(file, document, path, label):
                continue
            print(f"{file}: {path}: native macOS runner label {label!r}", file=sys.stderr)
            failures += 1
        for path, expression in unverifiable(document):
            print(f"{file}: {path}: unverifiable runner choice {expression!r}")
            warnings += 1
    if failures:
        print(
            f"\n{failures} unapproved native macOS runner label(s). Issues #444/#463 permit only\n"
            "the full matrix in macos-bundles.yml; all other labels are forbidden.\n"
            "See docs/ci-gates.md, section 'macOS'.",
            file=sys.stderr,
        )
        return 1
    summary = f"lint_no_macos_runners: {len(files)} files, no macOS runner labels"
    if warnings:
        # Printed, never fatal -- see `unverifiable`. It keeps the green result honest
        # about what it did and did not look at.
        summary += f" ({warnings} unverifiable runner choice(s) reported above)"
    print(summary)
    return 0


#: Scanned by default: GitHub Actions, plus the inherited Azure pipeline, which carried
#: macOS-14/macOS-15 jobs until #277 phase B2 and would otherwise never be looked at.
DEFAULT_TARGETS = (".github/workflows", "azure-pipelines.yml")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parents[1]
    targets = [Path(a) for a in args] if args else [root / t for t in DEFAULT_TARGETS]
    return check(*targets)


if __name__ == "__main__":
    raise SystemExit(main())
