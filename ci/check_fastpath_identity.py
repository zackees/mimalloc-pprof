#!/usr/bin/env python3
"""Prove the default build's allocation fast path is byte-identical to a base revision.

Issue #366 (docs/purge-all-implementation.md §10). `MI_OWNER_GATE` adds an owner acquire /
release around every allocator call; in the default build (`MI_OWNER_GATE=OFF`) those macros
expand to nothing and the plan's contract is that `malloc`/`free` are *byte-identical to
before*. That is a claim about machine code, not about source, so it is checked on machine
code: this script builds `libmimalloc.a` (Release, `MI_PPROF=ON`, `MI_OWNER_GATE=OFF`) at a
base revision and at HEAD with identical flags and the same compiler, disassembles the
fast-path entry points, normalises everything that is allowed to move (addresses, section
offsets, relocation addends into data sections), and requires an empty diff.

The positive control is a THIRD build, HEAD with `-DMI_OWNER_GATE=ON`: its fast path must
differ from HEAD's default build in at least one of the checked symbols. That is a real,
always-available control -- the gate's whole point is that it changes those bytes -- so it
stands in for the `-DMI_PURGE_ALL_FASTPATH_CANARY` source canary the plan sketched, which
would have needed a define in src/ that does nothing but perturb `mi_malloc`. A checker whose
normaliser had grown so permissive that it could not see the gate would fail here first.

Two things make the disassembly step easy to get vacuously wrong, and both are handled:

  * In this tree `mi_malloc` and `mi_free` are ALIASES of `malloc`/`free` (the override
    layer). GNU objdump's `--disassemble=SYM` prints nothing at all -- exit 0, no error --
    when SYM is not the one label it chose for that address. So symbols are resolved with
    `nm` and the function body is sliced out of the member's full disassembly by address,
    and a symbol that yields no instructions is a hard error, never an empty diff.
  * The archive's members are relocatable objects: every call target and every string
    reference is a relocation, and their addends and section offsets move whenever anything
    else in the translation unit changes size. Those are normalised away; instruction
    mnemonics and operands are not.

Usage:
    check_fastpath_identity.py [--base REV] [--out DIR] [--expect-dirty] [--skip-control]
    check_fastpath_identity.py --selftest

`--base` defaults to the merge base of HEAD and origin/main (else HEAD~1). `--expect-dirty`
inverts the main comparison for a hand check: it compares the base's default build against
HEAD's GATED build and requires a difference. Exit codes: 0 pass, 1 the check (or a control)
failed, 2 usage / tool error.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "out" / "fastpath-identity"

#: The fast-path entry points the plan names (§10). `mi_free` lives in alloc.c.o because
#: src/alloc.c includes free.c.
FASTPATH_SYMBOLS: tuple[str, ...] = (
    "mi_malloc",
    "mi_zalloc",
    "mi_free",
    "mi_heap_malloc_small",
    "mi_malloc_small",
)

COMMON_CMAKE: tuple[str, ...] = ("-DCMAKE_BUILD_TYPE=Release", "-DMI_PPROF=ON")

# `    7c2e:\tpush   %rbp` -> the instruction; `    7c2e:\t...` prefixes are addresses.
ADDR_PREFIX_RE = re.compile(r"^\s*[0-9a-f]+:\s*")
# `jmp    7c60 <mi_zalloc_small+0x32>` / `# 7c60 <sym>`: the raw target address moves with
# the function's position in the section; the symbolic form does not.
TARGET_ADDR_RE = re.compile(r"\b[0-9a-f]+ <([^>]+)>")
# A relocation line: `\t\t\tR_X86_64_PC32\t.rodata.str1.1+0x1a` -- keep the type and the
# symbol, drop the offset into a data section (string pools reorder freely).
RELOC_LINE_RE = re.compile(r"^\s*(R_[A-Z0-9_]+)\s+(.*)$")
SECTION_ADDEND_RE = re.compile(r"^(\.[A-Za-z0-9_.]+)[+-]0x[0-9a-f]+$")
FUNC_START_RE = re.compile(r"^([0-9a-f]+) <([^>]+)>:$")


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        cmd, cwd=cwd, env=env, check=False, capture_output=True, text=True, errors="replace"
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.stdout


# ------------------------------------------------------------------------------------------
# Normalisation
# ------------------------------------------------------------------------------------------


def normalise_line(line: str) -> str | None:
    """One line of `objdump -d -r --no-show-raw-insn` output -> comparable form, or None
    for a line that carries no instruction (blank, headers, the function label)."""
    stripped = line.rstrip()
    if not stripped or FUNC_START_RE.match(stripped):
        return None
    if not ADDR_PREFIX_RE.match(stripped):
        return None  # "Disassembly of section", "file format", ...
    body = ADDR_PREFIX_RE.sub("", stripped)
    reloc = RELOC_LINE_RE.match(body)  # `\t\t\t729e: R_X86_64_PC32\t.rodata.str1.1+0x1a`
    if reloc:
        target = reloc.group(2).strip()
        section = SECTION_ADDEND_RE.match(target)
        if section:
            target = f"{section.group(1)}+ADDEND"
        return f"    {reloc.group(1)} {target}"
    body = TARGET_ADDR_RE.sub(r"<\1>", body)
    return "    " + re.sub(r"\s+", " ", body)


def slice_functions(disassembly: str) -> dict[str, list[str]]:
    """Split a member's full disassembly into {start address (hex): normalised lines}."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in disassembly.splitlines():
        start = FUNC_START_RE.match(raw.rstrip())
        if start:
            current = []
            out[start.group(1).lstrip("0") or "0"] = current
            continue
        if current is None:
            continue
        norm = normalise_line(raw)
        if norm is not None:
            current.append(norm)
    return out


# ------------------------------------------------------------------------------------------
# Building and disassembling
# ------------------------------------------------------------------------------------------


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def build_static(source: Path, build: Path, gate: bool) -> Path:
    """Configure + build `mimalloc-static` and return the archive path."""
    if build.exists():
        shutil.rmtree(build)
    cmake = shutil.which("cmake") or "cmake"
    args = [cmake, "-S", str(source), "-B", str(build), *COMMON_CMAKE]
    if have("ninja"):
        args += ["-G", "Ninja"]
    args.append(f"-DMI_OWNER_GATE={'ON' if gate else 'OFF'}")
    configure = run(args, cwd=source)
    defines = next((ln for ln in configure.splitlines() if "Compiler defines" in ln), "")
    if gate and "MI_OWNER_GATE=1" not in defines:
        raise RuntimeError(f"MI_OWNER_GATE=1 did not reach the compiler defines: {defines}")
    if not gate and "MI_OWNER_GATE" in defines:
        raise RuntimeError(f"MI_OWNER_GATE leaked into the default build's defines: {defines}")
    run([cmake, "--build", str(build), "--parallel", "--target", "mimalloc-static"], cwd=source)
    libs = sorted(build.glob("libmimalloc*.a"))
    if not libs:
        raise RuntimeError(f"no libmimalloc*.a under {build}")
    return libs[0]


def disassemble_symbols(
    archive: Path, symbols: tuple[str, ...], scratch: Path
) -> dict[str, list[str]]:
    """{symbol: normalised instruction lines} for every requested symbol in `archive`."""
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    run(["ar", "x", str(archive.resolve())], cwd=scratch)
    found: dict[str, list[str]] = {}
    for member in sorted(scratch.glob("*.o")):
        nm = run(["nm", "--defined-only", str(member)], cwd=scratch)
        wanted: dict[str, str] = {}
        for line in nm.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] in ("T", "t") and parts[2] in symbols:
                wanted[parts[2]] = parts[0].lstrip("0") or "0"
        if not wanted:
            continue
        text = run(["objdump", "-d", "-r", "--no-show-raw-insn", str(member)], cwd=scratch)
        functions = slice_functions(text)
        for sym, addr in wanted.items():
            body = functions.get(addr)
            if not body:
                raise RuntimeError(
                    f"{sym} is at {addr} in {member.name} but no instructions were found there "
                    "-- the disassembly parser is broken, refusing to report a vacuous result"
                )
            if sym in found:
                raise RuntimeError(f"{sym} is defined in more than one member of {archive}")
            found[sym] = body
    missing = [s for s in symbols if s not in found]
    if missing:
        raise RuntimeError(f"symbols not found in {archive}: {', '.join(missing)}")
    return found


def compare(
    left_name: str,
    left: dict[str, list[str]],
    right_name: str,
    right: dict[str, list[str]],
    symbols: tuple[str, ...],
) -> list[str]:
    """The symbols whose normalised disassembly differs, printing a unified diff for each."""
    differing: list[str] = []
    for sym in symbols:
        if left[sym] == right[sym]:
            print(f"  {sym:<24} identical ({len(left[sym])} instructions)")
            continue
        differing.append(sym)
        print(f"  {sym:<24} DIFFERS ({len(left[sym])} vs {len(right[sym])} instructions)")
        diff = difflib.unified_diff(
            left[sym],
            right[sym],
            fromfile=f"{left_name}:{sym}",
            tofile=f"{right_name}:{sym}",
            lineterm="",
            n=2,
        )
        for line in diff:
            print("    " + line)
    return differing


# ------------------------------------------------------------------------------------------
# Revisions
# ------------------------------------------------------------------------------------------


def git(*args: str) -> str:
    return run(["git", *args], cwd=ROOT).strip()


def default_base() -> str:
    try:
        return git("merge-base", "HEAD", "origin/main")
    except RuntimeError:
        return "HEAD~1"


def resolve_revision(rev: str) -> str:
    try:
        return git("rev-parse", "--verify", f"{rev}^{{commit}}")
    except RuntimeError:
        pass
    # a shallow CI checkout: the base sha is known but not fetched yet
    run(["git", "fetch", "--depth=1", "origin", rev], cwd=ROOT)
    return git("rev-parse", "--verify", "FETCH_HEAD^{commit}")


def add_worktree(rev: str, path: Path) -> None:
    remove_worktree(path)
    run(["git", "worktree", "add", "--detach", str(path), rev], cwd=ROOT)


def remove_worktree(path: Path) -> None:
    if path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=ROOT, check=False, capture_output=True)


# ------------------------------------------------------------------------------------------
# Self-test: the normaliser and the slicer, on fixtures
# ------------------------------------------------------------------------------------------

FIXTURE = """
alloc.c.o:     file format elf64-x86-64


Disassembly of section .text:

0000000000000000 <mi_rotr>:
       0:\tpush   %rbp
       1:\tmov    %rsp,%rbp

000000000000729a <__libc_malloc>:
    729a:\tpush   %rbp
    729b:\tlea    0x0(%rip),%rdi        # 72a2 <__libc_malloc+0x8>
\t\t\t729e: R_X86_64_PC32\t.rodata.str1.1+0x1a
    72a2:\tcall   72a7 <__libc_malloc+0xd>
\t\t\t72a3: R_X86_64_PLT32\t_mi_theap_malloc_zero-0x4
    72a7:\tjmp    72b0 <__libc_malloc+0x16>
    72b0:\tret
"""

FIXTURE_SHIFTED = (
    FIXTURE.replace("729", "8a9")
    .replace("72a", "8aa")
    .replace("72b", "8ab")
    .replace("0x1a", "0x5c")
)


def selftest() -> int:
    functions = slice_functions(FIXTURE)
    shifted = slice_functions(FIXTURE_SHIFTED)
    ok = True
    body = functions.get("729a")
    if body is None or len(body) != 7:
        print(f"FAIL: expected 7 normalised lines for the fixture function, got {body}")
        ok = False
    else:
        expected_first = "    push %rbp"
        if body[0] != expected_first:
            print(f"FAIL: first line {body[0]!r} != {expected_first!r}")
            ok = False
        if body[2] != "    R_X86_64_PC32 .rodata.str1.1+ADDEND":
            print(f"FAIL: data relocation addend not normalised: {body[2]!r}")
            ok = False
        if body[4] != "    R_X86_64_PLT32 _mi_theap_malloc_zero-0x4":
            print(f"FAIL: function relocation must keep its symbol: {body[4]!r}")
            ok = False
        if "<__libc_malloc+0x16>" not in body[5] or "72b0" in body[5]:
            print(f"FAIL: branch target not normalised: {body[5]!r}")
            ok = False
    moved = shifted.get("8a9a")
    if moved != body:
        print("FAIL: the same function at a different offset must normalise identically")
        print(f"      {body}\n      {moved}")
        ok = False
    # and a real change must still show: a different mnemonic
    changed = slice_functions(FIXTURE.replace("    72b0:\tret", "    72b0:\tnop"))
    if changed.get("729a") == body:
        print("FAIL: a changed instruction normalised away")
        ok = False
    for tool in ("objdump", "nm", "ar", "cmake"):
        if not have(tool):
            print(f"FAIL: {tool} not on PATH")
            ok = False
    print("selftest: PASS" if ok else "selftest: FAIL")
    return 0 if ok else 1


# ------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base", help="base revision (default: merge-base of HEAD and origin/main, else HEAD~1)"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"scratch directory (default: {DEFAULT_OUT})"
    )
    parser.add_argument(
        "--expect-dirty",
        action="store_true",
        help="hand check: compare the base's default build against HEAD's GATED build and require a difference",
    )
    parser.add_argument(
        "--skip-control", action="store_true", help="do not build the gated positive control"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="check the normaliser on fixtures and exit"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    for tool in ("objdump", "nm", "ar", "cmake"):
        if not have(tool):
            print(f"error: {tool} not on PATH", file=sys.stderr)
            return 2

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    base_rev = resolve_revision(args.base or default_base())
    head_rev = git("rev-parse", "HEAD")
    print(
        f"fastpath-identity: base {base_rev[:12]} vs HEAD {head_rev[:12]} ({'dirty tree' if git('status', '--porcelain', '--', 'src', 'include') else 'clean src/include'})"
    )
    print(f"  symbols: {', '.join(FASTPATH_SYMBOLS)}")
    print(
        f"  flags:   {' '.join(COMMON_CMAKE)} (+ MI_OWNER_GATE)  CC={os.environ.get('CC', '(cmake default)')}"
    )

    base_src = out / "base-src"
    rc = 0
    try:
        add_worktree(base_rev, base_src)
        print("building base (MI_OWNER_GATE=OFF) ...")
        base_off = disassemble_symbols(
            build_static(base_src, out / "base-off", gate=False),
            FASTPATH_SYMBOLS,
            out / "x-base-off",
        )
        if args.expect_dirty:
            print("building HEAD (MI_OWNER_GATE=ON) ...")
            head_on = disassemble_symbols(
                build_static(ROOT, out / "head-on", gate=True), FASTPATH_SYMBOLS, out / "x-head-on"
            )
            print("base default build vs HEAD gated build (must differ):")
            if not compare("base-off", base_off, "head-on", head_on, FASTPATH_SYMBOLS):
                print(
                    "FAIL: --expect-dirty, but the gated build's fast path is identical to the base's default build"
                )
                return 1
            print("PASS: the gated build's fast path differs from the base's default build")
            return 0

        print("building HEAD (MI_OWNER_GATE=OFF) ...")
        head_off = disassemble_symbols(
            build_static(ROOT, out / "head-off", gate=False), FASTPATH_SYMBOLS, out / "x-head-off"
        )
        print("base vs HEAD, default build (must be identical):")
        differing = compare("base-off", base_off, "head-off", head_off, FASTPATH_SYMBOLS)
        if differing:
            print(
                f"FAIL: the default build's fast path changed vs {base_rev[:12]}: {', '.join(differing)}"
            )
            rc = 1
        else:
            print("PASS: the default build's fast path is byte-identical to the base revision")

        if not args.skip_control:
            print("building HEAD (MI_OWNER_GATE=ON) -- the positive control ...")
            head_on = disassemble_symbols(
                build_static(ROOT, out / "head-on", gate=True), FASTPATH_SYMBOLS, out / "x-head-on"
            )
            print("HEAD default vs HEAD gated (must differ):")
            control = compare("head-off", head_off, "head-on", head_on, FASTPATH_SYMBOLS)
            if not control:
                print(
                    "FAIL: positive control -- MI_OWNER_GATE=ON did not change any checked symbol, so this check could not see a real change either"
                )
                rc = 1
            else:
                print(f"PASS: positive control -- the gate changes {', '.join(control)}")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        remove_worktree(base_src)
    return rc


if __name__ == "__main__":
    sys.exit(main())
