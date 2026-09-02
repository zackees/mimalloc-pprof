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

A Windows bundle also has to carry the runtime DLLs its executables import but the build
tree does not contain: soldr's mingw-w64 links `mimalloc.dll` against `libgcc_s_seh-1.dll`,
which does not exist on a plain `windows-latest` runner. `--dll-search-dir` turns on an
import scan (`--objdump`, transitive) that copies exactly those and refuses to write a
bundle whose executables import something neither carried nor supplied by Windows itself.

Usage:

    uv run ci/bundle_tests.py <build-dir> <out-dir> [--config Debug]
                              [--objdump PROG] [--dll-search-dir DIR ...]
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
    forbid_text: str | None = None
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
            "forbid_text": self.forbid_text,
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
    forbid_text: str | None = None


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


def _parse_dash_d_p(tokens: Sequence[str], test_name: str) -> tuple[dict[str, str], str | None]:
    """Parse a `-Dkey=value ... -P script` token stream shared by every `cmake -P` harness
    this bundler understands. Any other token shape is refused outright."""
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
    return defines, script


def _lower_run_negative(defines: dict[str, str], test_name: str) -> _Lowered:
    """Lower `cmake -D... -P .../run-negative.cmake` into manifest fields.

    Mirrors test/run-negative.cmake exactly: run TEST_EXE (with optional TEST_ARG) under a
    10 s timeout, require a non-zero exit, and require EXPECTED_TEXT somewhere in the
    combined stdout+stderr. A timeout is a *failure*, not a pass -- the script's own words
    are "negative control timed out instead of failing fast".
    """
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


def _lower_run_text_check(defines: dict[str, str], test_name: str) -> _Lowered:
    """Lower `cmake -D... -P .../run-text-check.cmake` into manifest fields (issue #268).

    Mirrors test/run-text-check.cmake: run TEST_EXE under a 10 s timeout, require a *zero*
    exit (the opposite contract from run-negative.cmake), and either require or forbid
    EXPECTED_TEXT in the combined stdout+stderr depending on MODE. ENVIRONMENT is not a
    script variable here -- it is lowered separately from the ctest ENVIRONMENT property,
    which the `cmake -P` process inherits down to execute_process() same as any other
    command shape.
    """
    exe = defines.get("TEST_EXE")
    expected = defines.get("EXPECTED_TEXT")
    mode = defines.get("MODE")
    if not exe or not expected or mode not in ("REQUIRE", "FORBID"):
        raise BundleError(
            f"{test_name}: run-text-check.cmake needs TEST_EXE, EXPECTED_TEXT and "
            f"MODE=REQUIRE|FORBID, got {sorted(defines)}"
        )
    unknown = sorted(set(defines) - {"TEST_EXE", "EXPECTED_TEXT", "MODE"})
    if unknown:
        raise BundleError(f"{test_name}: unrecognised run-text-check.cmake variables {unknown}")
    return _Lowered(
        argv=[exe],
        timeout=NEGATIVE_TIMEOUT_SECONDS,
        expect_nonzero=False,
        expect_text=expected if mode == "REQUIRE" else None,
        forbid_text=expected if mode == "FORBID" else None,
    )


def _lower_cmake_script(tokens: Sequence[str], test_name: str) -> _Lowered:
    """Lower a `cmake -D... -P <script>` command, dispatching on the script's basename."""
    defines, script = _parse_dash_d_p(tokens, test_name)
    script_name = PurePosixPath(script.replace("\\", "/")).name if script else None
    if script_name == "run-negative.cmake":
        return _lower_run_negative(defines, test_name)
    if script_name == "run-text-check.cmake":
        return _lower_run_text_check(defines, test_name)
    raise BundleError(
        f"{test_name}: only test/run-negative.cmake and test/run-text-check.cmake are "
        f"understood as a `cmake -P` harness, got {script!r}"
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
    return _lower_cmake_script(rest, test_name)


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


#: A leading option prefix that an absolute path can hide behind: `-I/abs`, `-L/abs`,
#: `--out=/abs`, `--sysroot=/abs`. Stripping it is what makes those reachable to
#: `looks_absolute`, which only ever looks at the start of the string.
_FLAG_PREFIX = re.compile(r"^--?[A-Za-z][A-Za-z0-9_-]*=?")


def split_path_list(value: str) -> list[str]:
    """Split a list-shaped value on `;`, `,` and `:` -- but never inside `C:\\...`.

    Both separators are always honoured, regardless of the host: a Windows bundle is
    produced on Linux (`os.pathsep == ":"`) and replayed on Windows (`";"`), so splitting
    on `os.pathsep` alone leaves one of the two shapes unscanned.

    A `:` is a separator *except* when it is the second character of the current element
    and the first is a letter -- i.e. a drive qualifier. Splitting `C:\\build\\x` on `:`
    the way the phase B code did produced `C` and `\\build\\x`, neither of which any
    absolute-path test recognises, so a Windows build-tree leak inside a `PATH`-shaped
    value scanned clean (deferred review follow-up on PR #282).
    """
    pieces: list[str] = []
    start = 0
    for index, char in enumerate(value):
        # A `:` is a separator except when it is the drive qualifier of the element that
        # started one character ago.
        is_drive_letter = char == ":" and index - start == 1 and value[start].isalpha()
        if char in ";,:" and not is_drive_letter:
            pieces.append(value[start:index])
            start = index + 1
    pieces.append(value[start:])
    return pieces


def leaked_paths(test: BundledTest) -> list[str]:
    """Every absolute path in a lowered test that the bundle does not carry.

    `PathRewriter` only rewrites paths under the *build* directory, and only when they are
    the whole value. An absolute path from anywhere else -- the source tree, the
    toolchain, the runner's home -- or one hiding behind a flag prefix is copied into the
    manifest verbatim and then silently refers to a directory that does not exist on the
    machine that replays the bundle. Phase A only checked `argv[0]`, which was enough
    while the bundle never left the build host; phase B ships bundles to another machine,
    so argv[1:], every env value and the working directory are checked too, and phase C
    closes the two shapes phase B's review found still slipping through: a flag-prefixed
    absolute (`--out=/abs`, `-I/abs`) and a `C:\\...` element inside a separated list.
    """
    leaked: list[str] = []

    def scan(where: str, value: str) -> None:
        for piece in split_path_list(value):
            if not piece or piece.startswith(BUNDLE_PLACEHOLDER):
                continue
            candidates = [piece]
            flag = _FLAG_PREFIX.match(piece)
            if flag is not None:
                # Report the whole element, not the tail: `--out=/abs` is what a reader
                # has to go and find in the CMakeLists.
                candidates.append(piece[flag.end() :])
            for candidate in candidates:
                if not candidate or candidate.startswith(BUNDLE_PLACEHOLDER):
                    continue
                if looks_absolute(candidate):
                    leaked.append(f"{where}: {piece}")
                    break

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
                forbid_text=lowered.forbid_text,
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
# Windows runtime DLLs
# --------------------------------------------------------------------------------------

#: `objdump -p`/`llvm-objdump -p` print one of these per PE import descriptor. Both tools
#: spell the *export* directory's own name `DLL name:` (lower-case `n`), so matching
#: case-sensitively is what keeps `mimalloc.dll` from being reported as importing itself.
_PE_IMPORT = re.compile(r"^\s*DLL Name:\s*(\S+)\s*$")

#: DLLs Windows itself supplies. Anything outside this set has to be carried by the bundle
#: or the executable will not start on a machine that never had a toolchain installed --
#: and a missing DLL surfaces as a dialog-free 0xC0000135 exit, not as a test failure that
#: says what happened.
#:
#: The prefixes cover the API sets (`api-ms-win-crt-heap-l1-1-0.dll` and friends): UCRT is
#: reached exclusively through those, which is why an mingw-w64 UCRT build imports no
#: `ucrtbase.dll` by name.
SYSTEM_DLL_PREFIXES = ("api-ms-win-", "ext-ms-win-")
SYSTEM_DLLS = frozenset(
    name.lower()
    for name in (
        "advapi32.dll",
        "bcrypt.dll",
        "bcryptprimitives.dll",
        "comctl32.dll",
        "comdlg32.dll",
        "crypt32.dll",
        "d3d11.dll",
        "dbghelp.dll",
        "dxgi.dll",
        "gdi32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "kernelbase.dll",
        "mscoree.dll",
        "msvcrt.dll",
        "ntdll.dll",
        "ole32.dll",
        "oleaut32.dll",
        "powrprof.dll",
        "psapi.dll",
        "rpcrt4.dll",
        "secur32.dll",
        "shell32.dll",
        "shlwapi.dll",
        "synchronization.dll",
        "ucrtbase.dll",
        "user32.dll",
        "userenv.dll",
        "version.dll",
        "winmm.dll",
        "ws2_32.dll",
    )
)

#: Files whose imports are worth reading. `.a`/`.lib` import libraries are not loaded at
#: run time, so they are not scanned even when they sit in the bundle.
PE_SUFFIXES = (".exe", ".dll")


def is_system_dll(name: str) -> bool:
    lowered = name.lower()
    return lowered in SYSTEM_DLLS or lowered.startswith(SYSTEM_DLL_PREFIXES)


def read_pe_imports(path: Path, objdump: str) -> list[str]:
    """The DLL names `path` imports, in import-table order. Non-PE input yields nothing."""
    proc = subprocess.run(
        [objdump, "-p", str(path)], capture_output=True, text=True, errors="replace", check=False
    )
    if proc.returncode != 0:
        raise BundleError(
            f"`{objdump} -p {path}` failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    names: list[str] = []
    for line in proc.stdout.splitlines():
        match = _PE_IMPORT.match(line)
        if match is not None and match.group(1) not in names:
            names.append(match.group(1))
    return names


def resolve_runtime_dlls(
    staged: Sequence[Path], search_dirs: Sequence[Path], objdump: str
) -> list[Path]:
    """Every non-system DLL the staged files import, transitively, found in `search_dirs`.

    The closure matters: `mimalloc-test-stress-dynamic.exe` imports `mimalloc.dll`, which
    imports `libgcc_s_seh-1.dll`, which is the one the runner does not have. Scanning only
    the executables would miss it.

    An import that is neither a system DLL nor findable is a hard error naming both the
    importer and the directories that were searched -- shipping the bundle anyway would
    trade a legible failure here for an unexplained exit code on the Windows runner.
    """
    # Case-insensitive, because PE import names are (`KERNEL32.dll` vs `kernel32.dll`) and
    # the search directories live on a case-sensitive filesystem during a cross build.
    available: dict[str, Path] = {}
    for directory in search_dirs:
        if not directory.is_dir():
            raise BundleError(f"--dll-search-dir {directory} is not a directory")
        for candidate in sorted(directory.glob("*.dll")):
            available.setdefault(candidate.name.lower(), candidate)

    carried = {path.name.lower(): path for path in staged}
    pending = [path for path in staged if path.suffix.lower() in PE_SUFFIXES]
    found: list[Path] = []
    missing: list[str] = []
    while pending:
        current = pending.pop(0)
        for imported in read_pe_imports(current, objdump):
            key = imported.lower()
            if key in carried or is_system_dll(imported):
                continue
            resolved = available.get(key)
            if resolved is None:
                missing.append(f"{current.name} imports {imported}")
                carried[key] = current  # report each missing DLL once, not once per importer
                continue
            carried[key] = resolved
            found.append(resolved)
            pending.append(resolved)
    if missing:
        searched = ", ".join(str(directory) for directory in search_dirs) or "(none given)"
        raise BundleError(
            "the bundle would ship executables that cannot load:\n  - "
            + "\n  - ".join(sorted(missing))
            + f"\nnot a known Windows system DLL and not found in: {searched}"
        )
    return found


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
    parser.add_argument(
        "--dll-search-dir",
        action="append",
        default=None,
        metavar="DIR",
        type=Path,
        help=(
            "directory holding toolchain runtime DLLs (repeatable). Passing it turns on the "
            "PE import scan: every non-system DLL the bundle's executables import, "
            "transitively, is copied in, and an unresolvable import is an error."
        ),
    )
    parser.add_argument(
        "--objdump",
        default="llvm-objdump",
        metavar="PROG",
        help="objdump used by --dll-search-dir (llvm-objdump or x86_64-w64-mingw32-objdump)",
    )
    args = parser.parse_args(argv)

    build_dir = Path(cast("Path", args.build_dir)).resolve()
    out_dir = Path(cast("Path", args.out_dir))
    config = cast("str | None", args.config)
    search_dirs = cast("list[Path] | None", args.dll_search_dir) or []
    objdump = cast("str", args.objdump)

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
        runtime_dlls: list[Path] = []
        if search_dirs:
            runtime_dlls = resolve_runtime_dlls(unique, search_dirs, objdump)
            for dll in runtime_dlls:
                if dll.name not in seen:
                    seen[dll.name] = dll
                    unique.append(dll)
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
    if runtime_dlls:
        print(
            f"  {len(runtime_dlls)} toolchain runtime DLL(s): "
            + ", ".join(sorted(dll.name for dll in runtime_dlls))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
