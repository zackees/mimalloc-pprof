#!/usr/bin/env python3
"""Compile every C code block embedded in the project's Markdown documentation.

Docs and code drift apart silently. PR #259 fixed a `docs/profiler.md` snippet that
used `mi_prof_config_t_decl` and `mi_prof_start_ex` without naming a single `#include`
-- it read fine, nobody caught it in review, and it would not have compiled if a reader
pasted it verbatim. Nothing checked that class of bug before this gate: doc snippets are
prose to every other tool in this repo, so a header rename, a removed macro, or a typo'd
identifier in an example ships silently and only a reader trying the example finds out.

This script extracts every fenced ```c block from `README.md` and `docs/*.md` and
compiles each one with `-fsyntax-only -Wall -Wextra -Werror -I include`, using both gcc
and clang when available. `-fsyntax-only` is deliberate: these are documentation
fragments, not a build target, and we only need to know whether they parse and
type-check against the real headers -- not whether they link.

**Fragments.** Most snippets are not complete programs: they have no `int main`, just a
sequence of statements meant to be read in context (a `#include` near the top, then a
few calls). For those, every `#include` line is hoisted to the top of the file and the
remaining body is wrapped in `int main(void) { ...; return 0; }`. A block that already
contains `int main` is compiled as written.

**Opt-out.** `docs/upstreaming.md` contains a ```c block that is a *patch excerpt*: a
fragment of an upstream `#if defined(__GNUC__) ...` guard, deliberately left without its
`#endif` because the surrounding prose is quoting upstream source, not presenting a
program. That block is exempted with an HTML comment on the line immediately before the
fence:

    <!-- doc-snippet: skip (patch excerpt, not a program) -->
    ```c
    ...

HTML comments are invisible in rendered Markdown (GitHub, docs sites, everywhere) so the
skip is silent to a reader, but the marker syntax *requires* a parenthesized reason --
an empty `skip ()` does not match and the block still gets compiled -- so an opt-out
always leaves behind a stated reason for the next person to judge, rather than becoming
a way to silently make an inconvenient failure disappear.

**Positive control.** `--self-test` feeds the checker a snippet that calls an undeclared
mimalloc function and asserts the checker's own compile step FAILS on it. If the checker
wrongly reports success, `--self-test` itself exits non-zero: a doc gate that has never
been observed to catch a broken snippet proves nothing about the real docs, the same
argument `docs/ci-gates.md` makes for every other gate here. CI runs both the real check
and `--self-test` on every PR that can affect a doc snippet (README.md, docs/*.md,
include/**, or this script itself).

Usage:
    check_doc_snippets.py                 # compile every doc snippet, exit 1 on failure
    check_doc_snippets.py --self-test      # positive control: exit 1 if it FAILS to catch
                                            # a deliberately broken snippet
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# NOTE: `from __future__ import annotations` (above) makes every annotation in this
# file a lazily-evaluated string, so `list[...]` / `X | None` are safe even though
# pyproject.toml targets py39 (which predates PEP 604/585 at runtime) -- ruff's
# pyupgrade ruleset (UP006/UP045) enforces exactly this, so use the builtin spellings
# rather than typing.List/typing.Optional.

REPO_ROOT = Path(__file__).resolve().parent.parent
INCLUDE_DIR = REPO_ROOT / "include"

# Compilers to try, in a fixed order so output is stable across runs. Each has its own
# extra flags: clang here is (in this environment, and plausibly others) a wrapper that
# injects linker flags such as `-Wl,-dynamic-linker=...`. Those are unused under
# `-fsyntax-only`, and `-Werror` turns clang's "unused command-line argument" warning
# about them into a hard failure that has nothing to do with the snippet -- so clang
# needs `-Wno-unused-command-line-argument` or every single check fails spuriously,
# gcc does not emit that warning and does not need the flag, but tolerates it fine.
COMPILERS = {
    "gcc": ["-fsyntax-only", "-Wall", "-Wextra", "-Werror"],
    "clang": [
        "-fsyntax-only",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-unused-command-line-argument",
    ],
}

FENCE_OPEN = "```c"
FENCE_CLOSE = "```"

# `<!-- doc-snippet: skip (reason) -->` on the line immediately before a ```c fence.
# The reason is mandatory -- `skip ()` does not match -- so an opt-out cannot be added
# without leaving behind a stated justification.
SKIP_MARKER_RE = re.compile(r"^<!--\s*doc-snippet:\s*skip\s*\((?P<reason>.+?)\)\s*-->\s*$")

MAIN_RE = re.compile(r"\bint\s+main\s*\(")
INCLUDE_RE = re.compile(r"^\s*#\s*include\b")


@dataclass
class Block:
    """One fenced ```c block found in a Markdown file."""

    path: Path
    line: int  # 1-indexed line number of the fence itself, for error messages
    code: str
    skip_reason: str | None


@dataclass
class CompileResult:
    compiler: str
    ok: bool
    # First error line on failure; "not installed" style note when a compiler is
    # missing (that case never counts as a failure, see compile_source below).
    detail: str
    installed: bool


def find_doc_files(root: Path) -> list[Path]:
    files: list[Path] = []
    readme = root / "README.md"
    if readme.is_file():
        files.append(readme)
    files.extend(sorted((root / "docs").glob("*.md")))
    return files


def extract_blocks(path: Path) -> list[Block]:
    """Pull every fenced ```c block out of a Markdown file, honoring the skip marker."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() != FENCE_OPEN:
            i += 1
            continue

        skip_reason: str | None = None
        if i > 0:
            m = SKIP_MARKER_RE.match(lines[i - 1].strip())
            if m:
                skip_reason = m.group("reason")

        fence_line = i + 1  # 1-indexed
        j = i + 1
        body: list[str] = []
        while j < n and lines[j].strip() != FENCE_CLOSE:
            body.append(lines[j])
            j += 1
        if j >= n:
            raise ValueError(f"{path}:{fence_line}: unterminated ```c code fence")

        blocks.append(
            Block(path=path, line=fence_line, code="\n".join(body), skip_reason=skip_reason)
        )
        i = j + 1
    return blocks


def prepare_source(code: str) -> str:
    """Turn a snippet into a compilable translation unit.

    A block that already declares `int main` is used as-is. Otherwise it is a
    fragment: every `#include` line is hoisted (in order of appearance) above a
    synthesized `int main(void) { ... return 0; }` wrapping the rest of the body.
    """
    if MAIN_RE.search(code):
        return code

    includes: list[str] = []
    body: list[str] = []
    for line in code.splitlines():
        if INCLUDE_RE.match(line):
            includes.append(line)
        else:
            body.append(line)

    parts = [*includes, "", "int main(void) {", *body, "  return 0;", "}"]
    return "\n".join(parts) + "\n"


def first_error_line(compiler_output: str) -> str:
    """Pull the first `error:` line out of compiler stderr, for a one-line summary."""
    for line in compiler_output.splitlines():
        if "error:" in line:
            return line.strip()
    # Fall back to the first non-empty line -- covers driver-level failures that
    # never mention "error:" (e.g. clang rejecting an unknown flag).
    for line in compiler_output.splitlines():
        if line.strip():
            return line.strip()
    return "(compiler reported failure with no output)"


def compile_source(source: str, compiler: str) -> CompileResult:
    exe = shutil.which(compiler)
    if exe is None:
        return CompileResult(
            compiler=compiler, ok=True, detail="not installed, skipped", installed=False
        )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False, encoding="utf-8") as f:
        f.write(source)
        tmp_path = Path(f.name)

    try:
        proc = subprocess.run(
            [exe, *COMPILERS[compiler], "-I", str(INCLUDE_DIR), str(tmp_path)],
            capture_output=True,
            text=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    if proc.returncode == 0:
        return CompileResult(compiler=compiler, ok=True, detail="ok", installed=True)
    return CompileResult(
        compiler=compiler,
        ok=False,
        detail=first_error_line(proc.stdout + proc.stderr),
        installed=True,
    )


def check_block(block: Block) -> bool:
    """Compile one block with every available compiler; print a line per compiler.

    Returns True iff the block passed (or was skipped) on every compiler that is
    actually installed.
    """
    label = f"{block.path}:{block.line}"
    if block.skip_reason is not None:
        print(f"skip {label}  ({block.skip_reason})")
        return True

    source = prepare_source(block.code)
    block_ok = True
    for compiler in COMPILERS:
        result = compile_source(source, compiler)
        if not result.installed:
            print(f"skip {label}  [{compiler}] {result.detail}")
            continue
        if result.ok:
            print(f"ok   {label}  [{compiler}]")
        else:
            print(f"FAIL {label}  [{compiler}] {result.detail}")
            block_ok = False
    return block_ok


def run_check(root: Path) -> int:
    doc_files = find_doc_files(root)
    if not doc_files:
        print(f"no documentation files found under {root}")
        return 1

    blocks: list[Block] = []
    for path in doc_files:
        blocks.extend(extract_blocks(path))

    if not blocks:
        print("no ```c blocks found -- nothing to check")
        return 0

    all_ok = True
    checked = 0
    skipped = 0
    for block in blocks:
        if block.skip_reason is not None:
            skipped += 1
        else:
            checked += 1
        if not check_block(block):
            all_ok = False

    print()
    print(f"{'PASS' if all_ok else 'FAIL'}: {checked} block(s) checked, {skipped} skipped")
    return 0 if all_ok else 1


# A deliberately broken fragment: `mi_totally_bogus_function_xyz` is not declared
# anywhere, so any conforming compiler must reject it as an implicit/undeclared
# function reference under -Wall -Werror. This is the positive control -- if
# compile_source() ever reports this as `ok`, the checker itself is broken.
SELF_TEST_BROKEN_SNIPPET = """\
#include <mimalloc.h>

void* p = mi_totally_bogus_function_xyz(1024);
mi_free(p);
"""


def run_self_test() -> int:
    source = prepare_source(SELF_TEST_BROKEN_SNIPPET)
    any_installed = False
    caught = True
    for compiler in COMPILERS:
        result = compile_source(source, compiler)
        if not result.installed:
            print(f"skip [{compiler}] not installed")
            continue
        any_installed = True
        if result.ok:
            print(
                f"FAIL [{compiler}] positive control did NOT fire -- checker let a broken snippet pass"
            )
            caught = False
        else:
            print(f"PASS [{compiler}] positive control fired: {result.detail}")

    if not any_installed:
        print(
            "FAIL --self-test: no compiler (gcc or clang) is installed; cannot verify the gate works"
        )
        return 1
    if not caught:
        print("FAIL --self-test: the checker did not catch a deliberately broken snippet")
        return 1
    print("PASS --self-test: checker correctly rejected the planted broken snippet")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="positive control: verify the checker rejects a deliberately broken snippet",
    )
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()
    return run_check(REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())
