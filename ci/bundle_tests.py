#!/usr/bin/env -S uv run --script
"""Turn a CMake build tree's ctest suite into a portable *test bundle*.

Issue #277 phase A. The goal of #277 is to build once on Linux and execute once per OS,
which means a macOS or Windows runner has to be able to run our whole ctest suite with
neither CMake nor a repo checkout present. This script produces that: a directory holding
every test executable, the mimalloc shared libraries they need, and a `tests.json`
manifest that `ci/run_test_bundle.py` replays.

The interesting part is that `ctest --show-only=json-v1` does *not* emit a list of plain
executables. On the `MI_DEBUG_FULL` tree, 7 of 31 tests invoke `cmake` itself:

  * `cmake -E env K=V ... <exe> [args]` -- a plain environment wrapper (test-stress-dynamic
    uses it to set MIMALLOC_VERBOSE and LD_PRELOAD/DYLD_INSERT_LIBRARIES)
  * `cmake -DTEST_EXE=... -DTEST_ARG=... -DEXPECTED_TEXT=... -P test/run-negative.cmake`
    -- the negative-control harness: run the exe, require a *non-zero* exit, require an
    expected substring in the combined stdout+stderr, and treat a 10 s timeout as a
    failure ("timed out instead of failing fast", not "failed as expected")

Both are *lowered* into manifest fields (`env`, `expect_nonzero`, `expect_text`,
`timeout`), so the runner needs no CMake. Any other `cmake` argv shape is a hard error
naming the test -- a bundle that silently drops a test is worse than no bundle, and
issue #277's whole coverage argument depends on the count not going down.

Absolute build-tree paths appear in argv *and* in env values, so both are rewritten to a
`${BUNDLE}` placeholder that the runner expands. Files are flattened into the bundle root
(so a Windows DLL sits next to its exe and a Linux .so is found via LD_LIBRARY_PATH rather
than a build-tree RPATH); a basename collision is a hard error rather than a silent
overwrite.

Usage:

    uv run ci/bundle_tests.py <build-dir> <out-dir> [--config Debug]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

MANIFEST_NAME = "tests.json"
MANIFEST_VERSION = 1

#: The runner substitutes this for the bundle's own absolute path. It is what "argv
#: relative to the bundle root" means concretely: paths stay explicit and machine-readable
#: instead of depending on the process's cwd, which `WORKING_DIRECTORY` is free to change.
BUNDLE_PLACEHOLDER = "${BUNDLE}"

#: ctest's own default when a test sets no TIMEOUT property.
DEFAULT_TIMEOUT_SECONDS = 1500.0

#: run-negative.cmake wraps its `execute_process` in `TIMEOUT 10`, and treats hitting it as
#: a failure of the negative control rather than as the expected non-zero exit.
NEGATIVE_TIMEOUT_SECONDS = 10.0

#: Test properties this format understands. Anything else is a hard error: ctest semantics
#: we do not implement (PASS_REGULAR_EXPRESSION, SKIP_RETURN_CODE, RESOURCE_LOCK, ...) must
#: not be silently discarded into a bundle that then reports green.
SUPPORTED_PROPERTIES = frozenset(
    {"ENVIRONMENT", "TIMEOUT", "WORKING_DIRECTORY", "LABELS", "WILL_FAIL", "DISABLED"}
)

#: Shared/import libraries every bundle carries regardless of whether a test names them on
#: its command line -- a dynamically linked test finds them through the loader, not argv.
LIBRARY_GLOBS = (
    "libmimalloc*",
    "mimalloc*.dll",
    "mimalloc*.so*",
    "mimalloc*.dylib",
    "mimalloc-redirect*.dll",
)


class BundleError(Exception):
    """A shape this bundler will not guess at. Always names the test."""


# --------------------------------------------------------------------------------------
# JSON narrowing (see ci/ci_queue_wait.py for the same rationale: prove, don't index)
# --------------------------------------------------------------------------------------


def _as_object(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _as_array(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _str_list(value: object) -> list[str]:
    return [item for item in _as_array(value) if isinstance(item, str)]


# --------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------


@dataclass
class BundledTest:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict[str, str])
    cwd: str = BUNDLE_PLACEHOLDER
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    expect_nonzero: bool = False
    expect_text: str | None = None
    labels: list[str] = field(default_factory=list[str])

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "env": dict(self.env),
            "cwd": self.cwd,
            "timeout": self.timeout,
            "expect_nonzero": self.expect_nonzero,
            "expect_text": self.expect_text,
            "labels": list(self.labels),
        }


@dataclass
class _Lowered:
    """Result of stripping any `cmake` wrapper off a ctest command."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict[str, str])
    timeout: float | None = None
    expect_nonzero: bool = False
    expect_text: str | None = None


# --------------------------------------------------------------------------------------
# Lowering
# --------------------------------------------------------------------------------------


def is_cmake(argv0: str) -> bool:
    stem = PurePosixPath(argv0.replace("\\", "/")).name.lower()
    return stem in {"cmake", "cmake.exe"}


def _split_env_assignments(tokens: Sequence[str], test_name: str) -> tuple[dict[str, str], int]:
    """Consume leading `K=V` tokens of `cmake -E env`.

    Returns the assignments and the index of the first token that is not one. `--unset=`
    and `--modify` are `cmake -E env` options we deliberately refuse rather than silently
    ignore.
    """
    env: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            raise BundleError(
                f"{test_name}: `cmake -E env` option {token!r} is not supported by the "
                f"bundle format (only plain K=V assignments are lowered)"
            )
        if "=" not in token:
            return env, index
        key, _, value = token.partition("=")
        if not key:
            return env, index
        env[key] = value
    raise BundleError(f"{test_name}: `cmake -E env` has no command after its assignments")


def _lower_run_negative(tokens: Sequence[str], test_name: str) -> _Lowered:
    """Lower `cmake -D... -P .../run-negative.cmake` into manifest fields.

    Mirrors test/run-negative.cmake exactly: run TEST_EXE (with optional TEST_ARG) under a
    10 s timeout, require a non-zero exit, and require EXPECTED_TEXT somewhere in the
    combined stdout+stderr. A timeout is a *failure*, not a pass -- the script's own words
    are "negative control timed out instead of failing fast".
    """
    defines: dict[str, str] = {}
    script: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-D"):
            key, _, value = token[2:].partition("=")
            defines[key] = value
            index += 1
        elif token == "-P":
            if index + 1 >= len(tokens):
                raise BundleError(f"{test_name}: `cmake -P` with no script path")
            script = tokens[index + 1]
            index += 2
        else:
            raise BundleError(
                f"{test_name}: unsupported token {token!r} in a `cmake -P` command line"
            )
    if script is None or PurePosixPath(script.replace("\\", "/")).name != "run-negative.cmake":
        raise BundleError(
            f"{test_name}: only test/run-negative.cmake is understood as a `cmake -P` "
            f"harness, got {script!r}"
        )
    exe = defines.get("TEST_EXE")
    expected = defines.get("EXPECTED_TEXT")
    if not exe or not expected:
        raise BundleError(
            f"{test_name}: run-negative.cmake needs TEST_EXE and EXPECTED_TEXT, got "
            f"{sorted(defines)}"
        )
    unknown = sorted(set(defines) - {"TEST_EXE", "TEST_ARG", "EXPECTED_TEXT"})
    if unknown:
        raise BundleError(f"{test_name}: unrecognised run-negative.cmake variables {unknown}")
    argv = [exe]
    arg = defines.get("TEST_ARG")
    if arg:
        argv.append(arg)
    return _Lowered(
        argv=argv,
        timeout=NEGATIVE_TIMEOUT_SECONDS,
        expect_nonzero=True,
        expect_text=expected,
    )


def lower_command(command: Sequence[str], test_name: str) -> _Lowered:
    """Strip any `cmake` wrapper off a ctest command, or refuse loudly."""
    if not command:
        raise BundleError(f"{test_name}: empty command")
    if not is_cmake(command[0]):
        return _Lowered(argv=list(command))

    rest = list(command[1:])
    if rest[:2] == ["-E", "env"]:
        env, offset = _split_env_assignments(rest[2:], test_name)
        argv = rest[2 + offset :]
        if not argv:
            raise BundleError(f"{test_name}: `cmake -E env` has no command after its assignments")
        if is_cmake(argv[0]):
            raise BundleError(f"{test_name}: nested `cmake` invocations are not supported")
        return _Lowered(argv=argv, env=env)
    if rest[:1] == ["-E"]:
        mode = rest[1] if len(rest) > 1 else "<none>"
        raise BundleError(
            f"{test_name}: `cmake -E {mode}` is not a shape this bundler lowers (only "
            f"`cmake -E env`)"
        )
    return _lower_run_negative(rest, test_name)


# --------------------------------------------------------------------------------------
# Path rewriting
# --------------------------------------------------------------------------------------


def _normalise(path: str) -> str:
    return os.path.normpath(path).replace("\\", "/")


#: A Windows drive-qualified path (`C:\\build\\x`) or a UNC share. `os.path.isabs` does not
#: recognise either when this script runs on Linux, which is exactly where every cross
#: bundle is produced -- so the leak scan below cannot rely on `isabs` alone.
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/])")


def looks_absolute(value: str) -> bool:
    return Path(value).is_absolute() or bool(_WINDOWS_ABSOLUTE.match(value))


def leaked_paths(test: BundledTest) -> list[str]:
    """Every absolute path in a lowered test that the bundle does not carry.

    `PathRewriter` only rewrites paths under the *build* directory. An absolute path from
    anywhere else -- the source tree, the toolchain, the runner's home -- is copied into
    the manifest verbatim and then silently refers to a directory that does not exist on
    the machine that replays the bundle. Phase A only checked `argv[0]`, which was enough
    while the bundle never left the build host; phase B ships bundles to another machine,
    so argv[1:], every env value and the working directory are checked too (review
    follow-up on PR #279).

    `os.pathsep`-joined values are split first, so a `PATH`-shaped variable is reported by
    the element that leaked rather than as one unreadable blob.
    """
    leaked: list[str] = []

    def scan(where: str, value: str) -> None:
        for piece in value.split(os.pathsep) if os.pathsep in value else [value]:
            if not piece or piece.startswith(BUNDLE_PLACEHOLDER):
                continue
            if looks_absolute(piece):
                leaked.append(f"{where}: {piece}")

    for index, argument in enumerate(test.argv):
        scan(f"argv[{index}]", argument)
    for key in sorted(test.env):
        scan(f"env[{key}]", test.env[key])
    scan("cwd", test.cwd)
    return leaked


class PathRewriter:
    """Rewrites absolute build-tree paths to `${BUNDLE}/<basename>` and records the files.

    Flattening (rather than mirroring the build tree) is deliberate: on Windows a DLL has
    to sit beside the exe that loads it, and on Linux/macOS a flat directory is what makes
    `LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH` enough to override the build-tree RPATH baked
    into the executable. Two different files with one basename would make that ambiguous,
    so it is an error.
    """

    def __init__(self, build_dir: Path) -> None:
        self._build_prefix = _normalise(str(build_dir.resolve())) + "/"
        self.assets: dict[str, Path] = {}

    def _register(self, source: Path) -> str:
        name = source.name
        existing = self.assets.get(name)
        if existing is not None and _normalise(str(existing)) != _normalise(str(source)):
            raise BundleError(
                f"two different files would both land at {name!r} in the bundle: "
                f"{existing} and {source}"
            )
        self.assets[name] = source
        return f"{BUNDLE_PLACEHOLDER}/{name}"

    def rewrite(self, value: str) -> str:
        """Rewrite one argv element or env value. Non-build-tree text passes through."""
        candidate = _normalise(value)
        if candidate == self._build_prefix.rstrip("/"):
            return BUNDLE_PLACEHOLDER
        if not candidate.startswith(self._build_prefix):
            return value
        return self._register(Path(candidate))

    def rewrite_all(self, values: Iterable[str]) -> list[str]:
        return [self.rewrite(value) for value in values]


# --------------------------------------------------------------------------------------
# ctest
# --------------------------------------------------------------------------------------


def run_ctest_show_only(build_dir: Path, config: str | None) -> object:
    argv = ["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"]
    if config:
        argv += ["-C", config]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BundleError(f"`{' '.join(argv)}` failed ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def convert(payload: object, build_dir: Path) -> tuple[list[BundledTest], dict[str, Path]]:
    """Convert a `ctest --show-only=json-v1` payload into manifest entries + asset list."""
    root = _as_object(payload)
    if root is None:
        raise BundleError("ctest --show-only=json-v1 did not return a JSON object")
    rewriter = PathRewriter(build_dir)
    tests: list[BundledTest] = []
    problems: list[str] = []

    for raw in _as_array(root.get("tests")):
        entry = _as_object(raw)
        if entry is None:
            problems.append("a `tests` element was not an object")
            continue
        name = _as_str(entry.get("name"))
        if name is None:
            problems.append("a test has no name")
            continue
        command = _str_list(entry.get("command"))

        properties: dict[str, object] = {}
        for raw_property in _as_array(entry.get("properties")):
            prop = _as_object(raw_property)
            if prop is None:
                continue
            key = _as_str(prop.get("name"))
            if key is not None:
                properties[key] = prop.get("value")
        unsupported = sorted(set(properties) - SUPPORTED_PROPERTIES)
        if unsupported:
            problems.append(
                f"{name}: test properties {unsupported} have no bundle equivalent; "
                f"implement them or the bundle would silently change what is asserted"
            )
            continue
        if properties.get("DISABLED") in (True, "TRUE", "ON", "1"):
            continue

        try:
            lowered = lower_command(command, name)
        except BundleError as exc:
            problems.append(str(exc))
            continue

        env: dict[str, str] = {}
        for assignment in _str_list(properties.get("ENVIRONMENT")):
            key, _, value = assignment.partition("=")
            if key:
                env[key] = value
        # `cmake -E env` assignments are applied by the wrapper, i.e. after (and therefore
        # over) the ENVIRONMENT property, so they win on a conflict.
        env.update(lowered.env)

        timeout = lowered.timeout
        if timeout is None:
            raw_timeout = properties.get("TIMEOUT")
            timeout = (
                float(raw_timeout)
                if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool)
                else DEFAULT_TIMEOUT_SECONDS
            )

        working_directory = _as_str(properties.get("WORKING_DIRECTORY"))
        expect_nonzero = lowered.expect_nonzero or properties.get("WILL_FAIL") in (
            True,
            "TRUE",
            "ON",
            "1",
        )

        try:
            test = BundledTest(
                name=name,
                argv=rewriter.rewrite_all(lowered.argv),
                env={key: rewriter.rewrite(value) for key, value in env.items()},
                cwd=rewriter.rewrite(working_directory)
                if working_directory
                else BUNDLE_PLACEHOLDER,
                timeout=timeout,
                expect_nonzero=expect_nonzero,
                expect_text=lowered.expect_text,
                labels=_str_list(properties.get("LABELS")),
            )
        except BundleError as exc:
            problems.append(f"{name}: {exc}")
            continue

        if not test.argv[0].startswith(BUNDLE_PLACEHOLDER):
            problems.append(
                f"{name}: argv[0] {test.argv[0]!r} is not inside the build tree, so the "
                f"bundle cannot carry it"
            )
            continue
        leaked = leaked_paths(test)
        if leaked:
            problems.append(
                f"{name}: absolute path(s) that the bundle does not carry would be "
                f"replayed verbatim on another machine: " + ", ".join(leaked)
            )
            continue
        tests.append(test)

    if problems:
        raise BundleError(
            "cannot lower "
            + str(len(problems))
            + " test(s) into a bundle:\n  - "
            + "\n  - ".join(problems)
        )
    if not tests:
        # An empty bundle would run cleanly and prove nothing -- exactly the "gate that
        # verifies nothing" failure docs/ci-gates.md exists to prevent.
        raise BundleError("ctest reported no tests; refusing to write an empty bundle")
    tests.sort(key=lambda t: t.name)
    return tests, dict(rewriter.assets)


# --------------------------------------------------------------------------------------
# Copying
# --------------------------------------------------------------------------------------


def library_files(build_dir: Path, config: str | None) -> list[Path]:
    """Every mimalloc library in the build tree, whether or not a test names it."""
    roots = [build_dir]
    if config:
        candidate = build_dir / config
        if candidate.is_dir():
            roots.append(candidate)
    found: dict[str, Path] = {}
    for root in roots:
        for pattern in LIBRARY_GLOBS:
            for path in sorted(root.glob(pattern)):
                if path.is_file() or path.is_symlink():
                    found.setdefault(path.name, path)
    return list(found.values())


def copy_into(out_dir: Path, sources: Iterable[Path]) -> list[str]:
    """Copy files (and symlinks, as symlinks) flat into `out_dir`. Returns the names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source in sources:
        destination = out_dir / source.name
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        if source.is_symlink():
            # `libmimalloc-debug.so -> libmimalloc-debug.so.3` and friends: keep the SONAME
            # chain intact, otherwise the loader looks for a name the bundle does not have.
            os.symlink(source.readlink(), destination)
        else:
            shutil.copy2(source, destination)
        copied.append(source.name)
    return copied


def write_manifest(
    out_dir: Path, tests: Sequence[BundledTest], build_dir: Path, config: str | None
) -> Path:
    manifest = {
        "version": MANIFEST_VERSION,
        "generated_from": {
            # The build directory's *name* only. Its absolute path was the one remaining
            # host path in the manifest, which made "grep the manifest for absolute paths"
            # -- the check phase B's bundles are shipped under -- impossible to state
            # simply (review follow-up on PR #279).
            "build_dir": build_dir.name,
            "config": config,
            "platform": sys.platform,
        },
        "tests": [test.to_json() for test in tests],
    }
    path = out_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("build_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--config",
        default=None,
        help="build configuration for multi-config generators (passed to ctest as -C)",
    )
    args = parser.parse_args(argv)

    build_dir = Path(cast("Path", args.build_dir)).resolve()
    out_dir = Path(cast("Path", args.out_dir))
    config = cast("str | None", args.config)

    try:
        payload = run_ctest_show_only(build_dir, config)
        tests, assets = convert(payload, build_dir)
        sources = list(assets.values()) + library_files(build_dir, config)
        seen: dict[str, Path] = {}
        unique: list[Path] = []
        for source in sources:
            if source.name not in seen:
                seen[source.name] = source
                unique.append(source)
        copied = copy_into(out_dir, unique)
        manifest_path = write_manifest(out_dir, tests, build_dir, config)
    except BundleError as exc:
        print(f"bundle_tests: {exc}", file=sys.stderr)
        return 1

    print(f"bundled {len(tests)} tests and {len(copied)} files into {out_dir}")
    print(f"manifest: {manifest_path}")
    negatives = sum(1 for test in tests if test.expect_nonzero)
    if negatives:
        print(f"  {negatives} negative control(s) lowered from run-negative.cmake")
    wrapped = sum(1 for test in tests if test.env)
    if wrapped:
        print(f"  {wrapped} test(s) carry environment overrides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
