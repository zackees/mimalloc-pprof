#!/usr/bin/env python3
"""Verify a built library contains no instructions above the architecture baseline.

Distro packagers (Debian, Fedora, Arch) build for a *generic* target: the binary must
run on the oldest CPU the distro supports.  mimalloc's `MI_OPT_ARCH` adds
`-march=haswell -mavx2` on x64 and `-march=armv8.1-a` on arm64, either of which makes
the result SIGILL on older hardware.

Two things make that easy to get wrong, and both are checked here:

  * On **arm64, `MI_OPT_ARCH` is force-enabled** -- CMakeLists.txt sets it back to ON
    *after* reading the user's value, so `-DMI_OPT_ARCH=OFF` is silently ignored.
    Only `-DMI_NO_OPT_ARCH=ON` actually disables it.
  * The failure is *latent*: the build succeeds, the tests pass on the build machine,
    and it only SIGILLs later on a user's older CPU.

So we check the shipped instruction stream directly rather than trusting the flags.

Usage:
    check_isa_baseline.py <library-or-binary> [--arch x64|arm64] [--expect-clean|--expect-dirty]

`--expect-dirty` is the positive control: it asserts the check *does* fire on a build
that was deliberately compiled with the arch optimizations on.  A check that has never
been observed to fail proves nothing.
"""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys

# Instructions above the respective baseline.  Deliberately conservative: every mnemonic
# here is one a *generic* build must not contain, and each is attributable to a specific
# ISA extension so a hit is diagnosable rather than just "something changed".
ABOVE_BASELINE = {
    # x86-64-v1 baseline (what Debian/RHEL x86_64 targets).  BMI1/BMI2/ABM/AVX2.
    "x64": {
        "rorx": "BMI2", "mulx": "BMI2", "pdep": "BMI2", "pext": "BMI2",
        "shlx": "BMI2", "shrx": "BMI2", "sarx": "BMI2", "bzhi": "BMI2",
        "andn": "BMI1", "blsi": "BMI1", "blsr": "BMI1", "blsmsk": "BMI1",
        "tzcnt": "BMI1", "lzcnt": "ABM", "popcnt": "SSE4.2",
        "vpermd": "AVX2", "vpbroadcastd": "AVX2", "vpbroadcastq": "AVX2",
    },
    # armv8.0-a baseline.  LSE atomics (8.1) are the ones mimalloc actually pulls in.
    "arm64": {
        "ldadd": "LSE", "ldadda": "LSE", "ldaddal": "LSE", "ldaddl": "LSE",
        "ldclr": "LSE", "ldclra": "LSE", "ldclral": "LSE", "ldclrl": "LSE",
        "ldeor": "LSE", "ldeora": "LSE", "ldeoral": "LSE", "ldeorl": "LSE",
        "ldset": "LSE", "ldseta": "LSE", "ldsetal": "LSE", "ldsetl": "LSE",
        "swp": "LSE", "swpa": "LSE", "swpal": "LSE", "swpl": "LSE",
        "cas": "LSE", "casa": "LSE", "casal": "LSE", "casl": "LSE",
        "casp": "LSE", "caspa": "LSE", "caspal": "LSE", "caspl": "LSE",
    },
}


def host_arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    sys.exit(f"unsupported host architecture {m!r}; pass --arch explicitly")


def find_objdump() -> list[str]:
    for cand in ("objdump", "llvm-objdump", "gobjdump"):
        path = shutil.which(cand)
        if path:
            return [path]
    # macOS ships objdump behind xcrun even when it is not on PATH.
    if shutil.which("xcrun"):
        return ["xcrun", "objdump"]
    sys.exit("no objdump/llvm-objdump found; cannot inspect the instruction stream")


def disassemble(target: str) -> str:
    tool = find_objdump()
    proc = subprocess.run(
        tool + ["-d", target], capture_output=True, text=True, errors="replace"
    )
    # objdump exits nonzero on archives whose members it cannot read, while still
    # emitting usable output for the rest.  Only bail if we got nothing at all.
    if not proc.stdout.strip():
        sys.exit(f"disassembly of {target} produced no output\n{proc.stderr.strip()}")
    return proc.stdout


def scan(disasm: str, arch: str) -> dict[str, int]:
    table = ABOVE_BASELINE[arch]
    # Match the mnemonic column only: `  4011a0: c4 e3 ... rorx $0x3f,%rax,%rax`.
    # Matching anywhere on the line would hit symbol names and operand text.
    pattern = re.compile(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2} )+\s*([a-z][a-z0-9._]*)", re.M)
    hits: dict[str, int] = {}
    for mnemonic in pattern.findall(disasm):
        base = mnemonic.split(".")[0]
        if base in table:
            hits[base] = hits.get(base, 0) + 1
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="library or executable to inspect")
    ap.add_argument("--arch", choices=sorted(ABOVE_BASELINE), default=None)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--expect-clean", action="store_true", default=True,
                      help="fail if any above-baseline instruction is present (default)")
    mode.add_argument("--expect-dirty", action="store_true",
                      help="positive control: fail if the scan finds NOTHING")
    args = ap.parse_args()

    arch = args.arch or host_arch()
    hits = scan(disassemble(args.target), arch)

    label = f"{args.target} [{arch}]"
    if args.expect_dirty:
        if not hits:
            print(f"FAIL {label}: expected above-baseline instructions, found none.")
            print("     The positive control did not fire -- the scanner is not working,")
            print("     so a clean result on the real build would be meaningless.")
            return 1
        print(f"PASS {label}: positive control fired ({sum(hits.values())} instructions)")
        for m, n in sorted(hits.items(), key=lambda kv: -kv[1]):
            print(f"       {n:>6}x {m} ({ABOVE_BASELINE[arch][m]})")
        return 0

    if hits:
        print(f"FAIL {label}: {sum(hits.values())} above-baseline instructions found.")
        for m, n in sorted(hits.items(), key=lambda kv: -kv[1]):
            print(f"       {n:>6}x {m} ({ABOVE_BASELINE[arch][m]})")
        print()
        print("     This binary will SIGILL on CPUs older than the assumed baseline.")
        print("     For a portable build, configure with -DMI_NO_OPT_ARCH=ON.")
        print("     Note -DMI_OPT_ARCH=OFF is NOT sufficient on arm64: CMakeLists.txt")
        print("     force-sets it back ON unless MI_NO_OPT_ARCH is what you passed.")
        return 1

    print(f"PASS {label}: no above-baseline instructions "
          f"({len(ABOVE_BASELINE[arch])} mnemonics checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
