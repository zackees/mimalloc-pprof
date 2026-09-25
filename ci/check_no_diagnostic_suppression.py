#!/usr/bin/env python3
"""Ban file-wide suppression of `-Wunused-function`; mark the function instead.

A `#pragma GCC diagnostic ignored "-Wunused-function"` silences the warning for every
function after it, so genuinely dead code hides behind the few helpers that were the
reason for the pragma. The rule is the opposite: a `static` function that a translation
unit may legitimately not call (debug-only, platform-only, feature-only, public inline
API) is marked `MI_DECL_MAYBE_UNUSED` at its definition, with a comment saying why.

Two checks:

1. No tracked C/C++ source or header under src/, include/, test/, cmake/ or rust/
   (the vendored amalgamation included, rust/target excluded) contains a GCC/clang
   pragma -- or `_Pragma` -- that ignores `-Wunused-function` (or the `-Wunused` group
   that contains it), nor MSVC's equivalent `#pragma warning(disable: 4505)`.
2. If `clang` is on PATH, the vendored amalgamation compiles with
   `-Werror=unused-function` for every feature combination in `DEFINE_SETS`. It is
   the one place the warning bites hardest: inlining every header into one file makes
   each header `static` helper a main-file definition, which clang then checks.
   Without clang this part is skipped with a note, not failed.

    python3 ci/check_no_diagnostic_suppression.py
    python3 ci/check_no_diagnostic_suppression.py --selftest
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Tracked trees whose C/C++ files are scanned for the pragma.
SCANNED_DIRS = ("src", "include", "test", "cmake", "rust")
#: Build output under a scanned tree; never tracked, excluded in case it ever is.
EXCLUDED_PREFIXES = ("rust/target/",)
SOURCE_SUFFIXES = frozenset({".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".inc"})

_WARNING = r"-Wunused(?:-function)?"
#: `#pragma GCC diagnostic ignored "-Wunused-function"` (or `clang`, or the `-Wunused` group).
PRAGMA_RE = re.compile(
    r'^\s*#\s*pragma\s+(?:GCC|clang)\s+diagnostic\s+ignored\s+"' + _WARNING + r'"'
)
#: The same pragma spelled as a `_Pragma(...)` operator, usually inside a macro.
OPERATOR_RE = re.compile(
    r'_Pragma\s*\(\s*"(?:GCC|clang)\s+diagnostic\s+ignored\s+\\"' + _WARNING + r'\\"'
)
#: MSVC's C4505 is "unreferenced function with internal linkage has been removed".
MSVC_RE = re.compile(r"^\s*#\s*pragma\s+warning\s*\(\s*disable\s*:[^)]*\b4505\b")

VENDOR_DIR = Path("rust/mimalloc-pprof/vendor")
AMALGAMATION = VENDOR_DIR / "mimalloc-pprof-amalgamated.c"
CLANG_FLAGS = (
    "-fsyntax-only",
    "-Wunused-function",
    "-Werror=unused-function",
    "-DMI_STATIC_LIB",
)
#: The five observability switches every build defines to 0 or 1 (rust/mimalloc-pprof/build.rs).
FEATURES = ("MI_PPROF", "MI_MEMEVT", "MI_DIAGNOSTICS", "MI_DHAT", "MI_OWNER_GATE")
#: (label, features set to 1, define NDEBUG?). Every feature not listed is set to 0.
DEFINE_SETS: tuple[tuple[str, frozenset[str], bool], ...] = (
    ("minimal release", frozenset(), True),
    ("full release", frozenset(FEATURES), True),
    ("full debug", frozenset(FEATURES), False),
    ("pprof only", frozenset({"MI_PPROF"}), True),
    ("memory-events + dhat", frozenset({"MI_MEMEVT", "MI_DHAT"}), True),
)


def is_suppression(line: str) -> bool:
    return bool(PRAGMA_RE.search(line) or OPERATOR_RE.search(line) or MSVC_RE.search(line))


def find_suppressions(files: Iterable[Path], root: Path) -> list[str]:
    """`path:line: text` for every suppressing line in `files` (paths relative to `root`)."""
    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if is_suppression(line):
                hits.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")
    return hits


def tracked_sources(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", *SCANNED_DIRS],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        root / rel
        for rel in out.splitlines()
        if Path(rel).suffix in SOURCE_SUFFIXES and not rel.startswith(EXCLUDED_PREFIXES)
    ]


def clang_command(clang: str, enabled: frozenset[str], ndebug: bool) -> list[str]:
    cmd = [clang, *CLANG_FLAGS, "-I", str(ROOT / VENDOR_DIR)]
    cmd += [f"-D{name}={1 if name in enabled else 0}" for name in FEATURES]
    if ndebug:
        cmd.append("-DNDEBUG")
    cmd.append(str(ROOT / AMALGAMATION))
    return cmd


def check_amalgamation_compiles() -> bool:
    clang = shutil.which("clang")
    if clang is None:
        print(
            "note: clang not on PATH; skipping the -Werror=unused-function compile of "
            f"{AMALGAMATION.as_posix()}"
        )
        return True
    ok = True
    for label, enabled, ndebug in DEFINE_SETS:
        cmd = clang_command(clang, enabled, ndebug)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            print(f"ok: {AMALGAMATION.as_posix()} [{label}]")
            continue
        ok = False
        print(f"::error::{AMALGAMATION.as_posix()} [{label}] failed: {' '.join(cmd)}")
        print(result.stdout + result.stderr)
    return ok


def selftest() -> int:
    planted_lines = (
        '#pragma GCC diagnostic ignored "-Wunused-function"',
        '  #  pragma clang diagnostic ignored "-Wunused-function"  // why',
        '#pragma GCC diagnostic ignored "-Wunused"',
        '_Pragma("GCC diagnostic ignored \\"-Wunused-function\\"")',
        "#pragma warning(disable: 4100 4505)",
    )
    clean = "\n".join(
        (
            '// #pragma GCC diagnostic ignored "-Wunused-function" is banned here',
            '#pragma GCC diagnostic ignored "-Wattributes"',
            '#pragma GCC diagnostic ignored "-Wunused-parameter"',
            "#pragma warning(disable: 4100)",
            "MI_DECL_MAYBE_UNUSED static inline int helper(void) { return 0; }",
        )
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clean_file = root / "clean.c"
        clean_file.write_text(clean + "\n", encoding="utf-8")
        assert find_suppressions([clean_file], root) == [], find_suppressions([clean_file], root)
        for index, planted in enumerate(planted_lines):
            dirty = root / f"dirty{index}.h"
            dirty.write_text(f"{clean}\n{planted}\n", encoding="utf-8")
            hits = find_suppressions([dirty], root)
            expected_line = clean.count("\n") + 2
            assert hits == [f"dirty{index}.h:{expected_line}: {planted.strip()}"], hits
    enabled_all = clang_command("clang", frozenset(FEATURES), ndebug=False)
    assert "-DMI_OWNER_GATE=1" in enabled_all and "-DNDEBUG" not in enabled_all, enabled_all
    minimal = clang_command("clang", frozenset(), ndebug=True)
    assert "-DMI_PPROF=0" in minimal and "-DNDEBUG" in minimal, minimal
    print("check_no_diagnostic_suppression selftest OK")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    hits = find_suppressions(tracked_sources(ROOT), ROOT)
    for hit in hits:
        print(
            f"::error::{hit} -- do not suppress -Wunused-function file-wide; mark the "
            "function MI_DECL_MAYBE_UNUSED with a comment saying why it may be unused"
        )
    if not hits:
        print("ok: no -Wunused-function suppression in tracked C/C++ sources")
    compiled = check_amalgamation_compiles()
    return 0 if not hits and compiled else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
