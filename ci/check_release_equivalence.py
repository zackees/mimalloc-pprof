#!/usr/bin/env python3
"""Prove MI_DEBUG=0 removes issue #167 diagnostics before compilation.

For MI_PPROF=ON and OFF, preprocess the complete src/static.c amalgamation twice
from the same revision: normally, and with diagnostic.c forcibly included even
though MI_DEBUG=0. Identical output plus a forbidden-token scan proves the
diagnostic source, hooks, fields, declarations, code, and data disappear before
compilation. Comparing one revision with itself means this permanent gate does
not reject unrelated intentional release work in future PRs.

The optional --base comparison is the one-time integration proof requested by
issue #167: it compares the entire release amalgamation against a parent revision.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path) -> bytes:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True).stdout


def export_revision(root: Path, revision: str, destination: Path) -> None:
    archive = run(["git", "archive", "--format=tar", revision], cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(destination, filter="data")


FORBIDDEN_RELEASE_TOKENS = (
    b"debug_owner",
    b"_mi_diagnostic_",
    b"_mi_lock_debug",
    b"mi_test_tls_control",
)


def copy_sources(source: Path, destination: Path) -> None:
    shutil.copytree(source / "include", destination / "include")
    shutil.copytree(source / "src", destination / "src")


def force_diagnostic_include(root: Path) -> None:
    static = root / "src" / "static.c"
    text = static.read_text(encoding="utf-8")
    guarded = '#if MI_DEBUG > 2\n#include "diagnostic.c"\n#endif'
    if text.count(guarded) != 1:
        raise RuntimeError("src/static.c diagnostic include guard changed; update the release gate")
    static.write_text(text.replace(guarded, '#include "diagnostic.c"'), encoding="utf-8")


def preprocess(root: Path, compiler: str, pprof: int) -> bytes:
    command = [
        compiler,
        "-E",
        "-P",
        "-x",
        "c",
        f"-ffile-prefix-map={root}=<repo>",
        f"-I{root / 'include'}",
        "-DMI_DEBUG=0",
        f"-DMI_PPROF={pprof}",
        "-DMI_STATIC_LIB=1",
        "-Wno-builtin-macro-redefined",
        '-D__DATE__="Jan 01 1970"',
        '-D__TIME__="00:00:00"',
        str(root / "src" / "static.c"),
    ]
    raw = run(command, cwd=root).replace(b"\r\n", b"\n")
    # Empty lines and trailing horizontal whitespace carry no C tokens. Macro
    # placeholders such as an empty debug-only struct field may leave either.
    lines = [line.rstrip() for line in raw.splitlines() if line.rstrip()]
    return b"\n".join(lines) + b"\n"


def assert_equivalent(base: Path, current: Path, compiler: str) -> None:
    for pprof in (0, 1):
        before = preprocess(base, compiler, pprof)
        after = preprocess(current, compiler, pprof)
        if before != after:
            with tempfile.TemporaryDirectory(prefix="mi-release-diff-") as raw:
                output = Path(raw)
                (output / "base.i").write_bytes(before)
                (output / "current.i").write_bytes(after)
                diff = subprocess.run(
                    ["git", "diff", "--no-index", "--", "base.i", "current.i"],
                    cwd=output,
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout
            raise RuntimeError(
                f"MI_DEBUG=0 MI_PPROF={pprof} changed compiler input:\n{diff[:12000]}"
            )
        print(f"MI_DEBUG=0 MI_PPROF={pprof}: amalgamation compiler input is identical")


def assert_release_firewall(current: Path, compiler: str) -> None:
    with tempfile.TemporaryDirectory(prefix="mi-release-forced-") as raw_forced:
        forced = Path(raw_forced)
        copy_sources(current, forced)
        force_diagnostic_include(forced)
        for pprof in (0, 1):
            normal = preprocess(current, compiler, pprof)
            diagnostic_source_present = preprocess(forced, compiler, pprof)
            if any(token in normal for token in FORBIDDEN_RELEASE_TOKENS):
                leaked = [token.decode() for token in FORBIDDEN_RELEASE_TOKENS if token in normal]
                raise RuntimeError(
                    f"MI_DEBUG=0 MI_PPROF={pprof} contains diagnostic tokens: {leaked}"
                )
            if normal != diagnostic_source_present:
                raise RuntimeError(
                    f"MI_DEBUG=0 MI_PPROF={pprof} changes when diagnostic.c is forcibly included"
                )
            print(f"MI_DEBUG=0 MI_PPROF={pprof}: diagnostic source/hooks/layout/data are absent")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    if args.base:
        with tempfile.TemporaryDirectory(prefix="mi-release-base-") as raw_base:
            base = Path(raw_base)
            export_revision(root, args.base, base)
            assert_equivalent(base, root, args.cc)

    assert_release_firewall(root, args.cc)
    if args.selftest:
        with tempfile.TemporaryDirectory(prefix="mi-release-control-") as raw_control:
            control = Path(raw_control)
            copy_sources(root, control)
            diagnostic = control / "src" / "diagnostic.c"
            with diagnostic.open("a", encoding="utf-8") as output:
                output.write("\nconst int _mi_release_equivalence_positive_control = 1;\n")
            try:
                assert_release_firewall(control, args.cc)
            except RuntimeError:
                print("release-equivalence positive control passed")
            else:
                raise AssertionError("release-equivalence gate missed injected release data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
