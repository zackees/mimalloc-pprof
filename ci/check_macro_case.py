#!/usr/bin/env python3
"""Macros must be UPPER_CASE.

Fails on any `#define` in a tracked C/C++ file under src/, include/ or test/ whose name has a
lower-case letter, unless the name is in ci/macro_case_baseline.txt. The baseline grandfathers the
lower-case macros inherited from upstream mimalloc (`mi_decl_export`, `mi_assert`, ...); it may only
shrink. `--update-baseline` rewrites it from the tree (a deliberate, reviewed act).

    uv run ci/check_macro_case.py [--selftest | --update-baseline]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "ci" / "macro_case_baseline.txt"
SCOPES = ("src/", "include/", "test/")
SUFFIXES = (".c", ".h", ".cpp", ".hpp", ".cc")
DEFINE = re.compile(r"^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)")


def tracked_sources() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", *SCOPES], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [ROOT / line for line in out.splitlines() if line.endswith(SUFFIXES)]


def lowercase_macros(files: Iterable[Path]) -> dict[str, str]:
    """Every macro name with a lower-case letter, mapped to its first file:line."""
    found: dict[str, str] = {}
    for path in files:
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            match = DEFINE.match(line)
            if match and any(c.islower() for c in match.group(1)):
                found.setdefault(
                    match.group(1),
                    f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{number}",
                )
    return found


def read_baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def check(files: Iterable[Path], baseline: set[str]) -> list[str]:
    return [
        f"{where}: macro `{name}` must be UPPER_CASE"
        for name, where in sorted(lowercase_macros(files).items())
        if name not in baseline
    ]


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        good, bad = Path(tmp) / "good.h", Path(tmp) / "bad.h"
        good.write_text("#define MI_GOOD 1\n#define mi_old(x) (x)\n", encoding="utf-8")
        bad.write_text("  #  define mi_new_thing 1\n", encoding="utf-8")
        assert check([good], {"mi_old"}) == [], "a grandfathered or upper-case macro was flagged"
        assert len(check([bad], {"mi_old"})) == 1, "a new lower-case macro was not flagged"
    print("check_macro_case selftest OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.update_baseline:
        names = sorted(lowercase_macros(tracked_sources()))
        BASELINE.write_text(
            "# Lower-case macros inherited from upstream; see ci/check_macro_case.py. May only shrink.\n"
            + "\n".join(names)
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(names)} names to {BASELINE.relative_to(ROOT)}")
        return 0
    errors = check(tracked_sources(), read_baseline())
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    if errors:
        return 1
    print("PASS every new macro is UPPER_CASE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
