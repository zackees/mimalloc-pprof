#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# ///
"""Fail if a C API this fork adds is not reachable from the Rust crate.

The Rust crate is a hand-written binding: `rust/mimalloc-pprof/src/sys.rs` declares each
C function by hand and mirrors each C enumerator's value by hand. Nothing in the Rust
build notices when the C side grows a function or an option -- the crate keeps compiling,
and the new API is simply invisible to every Rust consumer. That is how
`include/mimalloc/memory-events.h` ended up with no binding at all while the profiler
next to it was bound completely.

Worse than absent is *wrong*. `mi_option_t` is positional, and this fork inserts thirteen
enumerators (`prof*`, `memory_events`, `purge_zeroes`, `scavenger`, `purge_holes*`) at
indices 47..=59, ahead of `_mi_option_last`. A mirror that drifted by one would compile,
link, run, and set a different option than the caller named, silently and forever. So
this script does not merely check that each option NAME is present: it checks the whole
sequence, in order, against the header.

    uv run ci/check_rust_surface.py            # check the tree
    uv run ci/check_rust_surface.py --selftest # check this script's own parsing

What it checks:

  1. Every `mi_decl_export` prototype in the fork's own headers (`mimalloc/profile.h`,
     `mimalloc/memory-events.h`, `mimalloc/dhat.h` -- 100% fork) plus the fork's named
     additions to the two upstream headers is declared in `sys.rs`, or is on
     `UNBOUND_WITH_REASON` with a reason.
  2. `MI_OPTIONS_IN_ORDER` in `sys.rs` is exactly the `mi_option_t` enumerator sequence
     from `include/mimalloc.h`, up to and including `_mi_option_last`.
  3. Every C function on `WRAPPED_IN_LIB` really does have its safe wrapper named in
     `src/lib.rs`, and every fork export is either wrapped or on `SYS_ONLY_WITH_REASON`.

What it deliberately does NOT do: diff against the pinned upstream base commit
(`6def7be9`). `actions/checkout@v4` fetches depth 1, and this fork's history is unrelated
to upstream's, so that object does not exist in CI. FORK_ADDITIONS_IN_UPSTREAM_HEADERS
below is the explicit list instead -- and every name on it is verified to still be an
export in the header it claims, so a rename cannot quietly empty the list.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INCLUDE_DIR = REPO_ROOT / "include"
CRATE_DIR = REPO_ROOT / "rust" / "mimalloc-pprof"
SYS_RS = CRATE_DIR / "src" / "sys.rs"
LIB_RS = CRATE_DIR / "src" / "lib.rs"

#: Headers whose every `mi_decl_export` is a fork addition: these three files do not
#: exist upstream at all.
FORK_ONLY_HEADERS = (
    Path("mimalloc/profile.h"),
    Path("mimalloc/memory-events.h"),
    Path("mimalloc/dhat.h"),
)

#: Headers the fork shares with upstream. Only the names listed in
#: FORK_ADDITIONS_IN_UPSTREAM_HEADERS are this fork's; the rest are upstream's API and are
#: bound opportunistically, not by requirement.
SHARED_HEADERS = (Path("mimalloc.h"), Path("mimalloc-stats.h"))

#: The fork's own exports inside the two shared headers. Kept as a literal list rather
#: than derived from git: see the module docstring.
FORK_ADDITIONS_IN_UPSTREAM_HEADERS = {
    # issue #272 / Bun parity P7a -- the idle/scavenger surface
    "mi_on_thread_idle",
    "mi_on_thread_idle_start",
    "mi_on_thread_idle_end",
    "mi_scavenger_stop",
    # issue #272 / Bun parity P7b -- hole purging
    "mi_purge_holes_stats_get",
    "mi_purge_holes_report",
    # issue #269 / Bun parity P4 -- the heap dump
    "mi_heap_dump_json",
    "mi_heap_get_seq",
}

#: C functions with no `sys.rs` declaration, and why that is deliberate. Empty today:
#: every fork export is bound. Kept as the pressure valve so a future omission has to be
#: written down rather than merely not noticed.
UNBOUND_WITH_REASON: dict[str, str] = {}

#: Fork exports that are declared in `sys.rs` but deliberately have no safe wrapper in
#: `lib.rs`, and why.
SYS_ONLY_WITH_REASON = {
    "mi_prof_debug_stats": (
        "deprecated in include/mimalloc/profile.h in favour of mi_prof_stats_get, which "
        "is wrapped as prof::stats(). Binding it in sys.rs keeps the C surface complete "
        "without giving the deprecated shape a Rust name."
    ),
    "mi_heap_get_seq": (
        "needs a `mi_heap_t*`, and mimalloc v3 removed `mi_heap_get_default`, so Rust has "
        "no safe way to name a heap to ask about. The sequence numbers it returns are "
        "already visible in the JSON from `heap_dump_json`, which is the wrapped route."
    ),
    "mi_prof_visit": (
        "prof::samples() deliberately wraps mi_prof_snapshot_new/_visit/_free instead. "
        "mi_prof_visit runs its visitor while the profiler lock is held, so a Rust "
        "closure that allocates -- which collecting into a Vec does -- can deadlock "
        "against an ordinary mi_malloc on another thread (see profile.h's #270 note)."
    ),
}

_EXPORT_RE = re.compile(
    r"mi_decl_export[^;{]*?\b(mi_[A-Za-z0-9_]+)\s*\(",
)
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"//[^\n]*")
_OPTION_ENUM_RE = re.compile(r"typedef\s+enum\s+mi_option_e\s*\{(.*?)\}\s*mi_option_t", re.DOTALL)
_SYS_EXTERN_FN_RE = re.compile(r"^\s*pub fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*[(<]", re.MULTILINE)
_SYS_OPTIONS_BLOCK_RE = re.compile(r"mi_options!\s*\{(.*?)\n\}", re.DOTALL)
_SYS_OPTION_NAME_RE = re.compile(r"^\s*(_?mi_option_[A-Za-z0-9_]*)\s*=\s*(\d+);", re.MULTILINE)


def strip_comments(source: str) -> str:
    """C source with block and line comments blanked out.

    Load bearing: several prototypes are *described* in neighbouring comments (and
    `include/mimalloc.h` discusses `mi_heap_visit_blocks` at length), so a regex run over
    raw text would find exports that are not declared and enumerators that are not there.
    """
    return _COMMENT_LINE_RE.sub(" ", _COMMENT_BLOCK_RE.sub(" ", source))


def exports_in_source(source: str) -> set[str]:
    """Every `mi_decl_export`ed function name declared in a chunk of C header text."""
    return set(_EXPORT_RE.findall(strip_comments(source)))


def header_exports(path: Path) -> set[str]:
    """Every `mi_decl_export`ed function name declared in one header."""
    return exports_in_source(path.read_text(encoding="utf-8"))


def option_enumerators(header: str) -> list[str]:
    """`mi_option_t`'s enumerators in declaration order, up to `_mi_option_last`.

    The deprecated aliases after the sentinel are excluded: they carry explicit `= other`
    values rather than positions, so they are not part of the sequence that shifts.
    """
    match = _OPTION_ENUM_RE.search(strip_comments(header))
    if match is None:
        raise ValueError("could not find `typedef enum mi_option_e { ... } mi_option_t`")
    names: list[str] = []
    for token in match.group(1).split(","):
        name = token.split("=")[0].strip()
        if not name.startswith(("mi_option_", "_mi_option")):
            continue
        names.append(name)
        if name == "_mi_option_last":
            break
    return names


def sys_declared_functions(source: str) -> set[str]:
    """Every function `sys.rs` declares in its `unsafe extern "C"` block."""
    return set(_SYS_EXTERN_FN_RE.findall(source))


def sys_option_mirror(source: str) -> list[tuple[str, int]]:
    """The `mi_options! { ... }` mirror in `sys.rs`, as `(name, value)` in source order."""
    match = _SYS_OPTIONS_BLOCK_RE.search(source)
    if match is None:
        raise ValueError("could not find the `mi_options! { ... }` block in sys.rs")
    return [(name, int(value)) for name, value in _SYS_OPTION_NAME_RE.findall(match.group(1))]


def is_called_from_lib(name: str, lib_source: str) -> bool:
    """Whether `src/lib.rs` actually calls `sys::<name>`.

    Word-anchored rather than a plain substring test, because C's names nest:
    `sys::mi_prof_start_seeded` contains `sys::mi_prof_start`, `sys::mi_option_get_clamp`
    contains `sys::mi_option_get`, `sys::mi_option_set_default` contains
    `sys::mi_option_set`. A substring test would report the shorter one as wrapped on the
    strength of the longer one's call site -- which is precisely the regression this
    check exists to catch. `\\b` does not match between `t` and `_`, so the longer name
    no longer covers the shorter.
    """
    return re.search(rf"\bsys::{re.escape(name)}\b", lib_source) is not None


def option_mirror_problems(header_options: list[str], mirror: list[tuple[str, int]]) -> list[str]:
    """Everything wrong with `sys.rs`'s `mi_option_t` mirror, as human-readable strings.

    Split out from `check` so the negative control is testable: a mirror that is merely
    *shifted* still contains every correct name, so a set comparison would call it clean
    while every option past the shift silently resolves to the wrong one.
    """
    problems: list[str] = []
    mirror_names = [name for name, _ in mirror]
    if mirror_names != header_options:
        # Report the first divergence rather than two long lists: that is the enumerator
        # whose wrong value every later one inherits.
        for index, expected in enumerate(header_options):
            actual = mirror_names[index] if index < len(mirror_names) else "<missing>"
            if actual != expected:
                problems.append(
                    f"mi_option_t mirror diverges at index {index}: include/mimalloc.h "
                    f"has `{expected}`, sys.rs's mi_options! has `{actual}`. Every later "
                    "option is now off by at least one, which silently sets the WRONG "
                    "option at runtime."
                )
                break
        else:
            problems.append(
                f"mi_option_t mirror has {len(mirror_names)} enumerators, "
                f"include/mimalloc.h has {len(header_options)}."
            )
    for index, (name, value) in enumerate(mirror):
        if value != index:
            problems.append(
                f"sys.rs mirrors `{name}` as {value} but it is at index {index}; "
                "mi_option_t is a plain sequential C enum."
            )
    return problems


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def check(*, verbose: bool = True) -> int:
    """Return 0 when the Rust binding surface covers the C surface, 1 otherwise."""
    problems: list[str] = []

    fork_exports: dict[str, str] = {}
    for relative in FORK_ONLY_HEADERS:
        for name in sorted(header_exports(INCLUDE_DIR / relative)):
            fork_exports[name] = str(relative)

    shared_exports: dict[str, str] = {}
    for relative in SHARED_HEADERS:
        for name in header_exports(INCLUDE_DIR / relative):
            shared_exports[name] = str(relative)

    # A rename upstream (or in the fork) must not quietly shrink the list of things this
    # script requires, so every literal name is checked against the header it claims.
    for name in sorted(FORK_ADDITIONS_IN_UPSTREAM_HEADERS):
        if name not in shared_exports:
            _fail(
                problems,
                f"{name}: listed in FORK_ADDITIONS_IN_UPSTREAM_HEADERS but no longer "
                f"exported by any of {', '.join(str(h) for h in SHARED_HEADERS)}. If it "
                "was renamed, update the list; if it was removed, drop it.",
            )
        else:
            fork_exports[name] = shared_exports[name]

    sys_source = SYS_RS.read_text(encoding="utf-8")
    lib_source = LIB_RS.read_text(encoding="utf-8")
    declared = sys_declared_functions(sys_source)

    for name, origin in sorted(fork_exports.items()):
        if name in declared:
            continue
        reason = UNBOUND_WITH_REASON.get(name)
        if reason:
            if verbose:
                print(f"  allowed unbound: {name} ({origin}) -- {reason}")
            continue
        _fail(
            problems,
            f"{name} ({origin}) is exported by C but not declared in "
            f"rust/mimalloc-pprof/src/sys.rs. Add the `pub fn` declaration, or add it to "
            "UNBOUND_WITH_REASON in this script with a reason.",
        )

    # Stale allowlist entries are their own failure: an entry that no longer describes
    # anything real is a claim nobody checked.
    for name in sorted(UNBOUND_WITH_REASON):
        if name in declared:
            _fail(
                problems,
                f"{name} is on UNBOUND_WITH_REASON but IS declared in sys.rs. Remove the "
                "allowlist entry.",
            )
        elif name not in fork_exports:
            _fail(
                problems,
                f"{name} is on UNBOUND_WITH_REASON but is not a fork export at all. "
                "Remove the allowlist entry.",
            )

    # --- the positional hazard -------------------------------------------------------
    header_options = option_enumerators((INCLUDE_DIR / "mimalloc.h").read_text(encoding="utf-8"))
    problems.extend(option_mirror_problems(header_options, sys_option_mirror(sys_source)))

    # --- safe wrappers ---------------------------------------------------------------
    for name in sorted(fork_exports):
        if name not in declared:
            continue  # already reported (or allowlisted) above
        if is_called_from_lib(name, lib_source):
            continue
        reason = SYS_ONLY_WITH_REASON.get(name)
        if reason:
            if verbose:
                print(f"  sys-only: {name} -- {reason}")
            continue
        _fail(
            problems,
            f"{name} is declared in sys.rs but never called from src/lib.rs, so there is "
            "no safe wrapper. Add one, or add it to SYS_ONLY_WITH_REASON with a reason.",
        )

    for name in sorted(SYS_ONLY_WITH_REASON):
        if name not in fork_exports:
            _fail(
                problems,
                f"{name} is on SYS_ONLY_WITH_REASON but is not a fork export. Remove the "
                "allowlist entry.",
            )
        elif is_called_from_lib(name, lib_source):
            _fail(
                problems,
                f"{name} is on SYS_ONLY_WITH_REASON but IS called from lib.rs. Remove the "
                "allowlist entry.",
            )

    if problems:
        print("\ncheck_rust_surface: the Rust binding surface does not cover C.\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            f"\n{len(problems)} problem(s). See rust/mimalloc-pprof/src/sys.rs and the "
            "'API surface' table in README.md.",
            file=sys.stderr,
        )
        return 1

    print(
        f"check_rust_surface: {len(fork_exports)} fork exports bound, "
        f"{len(header_options)} mi_option_t enumerators mirrored in order."
    )
    return 0


# --------------------------------------------------------------------------------------
# Self-test: the parsing, on fixtures, so a regex that silently matches nothing (and
# therefore reports "clean") fails loudly instead. This has already happened twice in
# this repo's gate scripts -- see .github/workflows/python-lint.yml's header.
# --------------------------------------------------------------------------------------

_SELFTEST_HEADER = """
/* mi_decl_export void mi_in_a_comment(void); */
// mi_decl_export void mi_in_a_line_comment(void);
mi_decl_export void mi_real_one(void) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_real_two(size_t n) mi_attr_noexcept;

typedef enum mi_option_e {
  mi_option_alpha,
  mi_option_beta,   /* a comment, with a comma, that must not become an enumerator */
  mi_option_gamma,
  _mi_option_last,
  mi_option_alias = mi_option_alpha
} mi_option_t;
"""

_SELFTEST_SYS = """
mi_options! {
    /// doc
    mi_option_alpha = 0;
    mi_option_beta = 1;
    mi_option_gamma = 2;
    _mi_option_last = 3;
}

unsafe extern "C" {
    pub fn mi_real_one();
    pub fn mi_real_two(n: usize) -> bool;
}
"""


def selftest() -> int:
    """Check the parsers against fixtures; return 0 on success."""
    failures: list[str] = []

    exports = exports_in_source(_SELFTEST_HEADER)
    if exports != {"mi_real_one", "mi_real_two"}:
        failures.append(f"export parsing: expected the two real exports, got {sorted(exports)}")

    options = option_enumerators(_SELFTEST_HEADER)
    if options != ["mi_option_alpha", "mi_option_beta", "mi_option_gamma", "_mi_option_last"]:
        failures.append(f"option parsing: got {options}")

    mirror = sys_option_mirror(_SELFTEST_SYS)
    if mirror != [
        ("mi_option_alpha", 0),
        ("mi_option_beta", 1),
        ("mi_option_gamma", 2),
        ("_mi_option_last", 3),
    ]:
        failures.append(f"sys.rs option mirror parsing: got {mirror}")

    declared = sys_declared_functions(_SELFTEST_SYS)
    if declared != {"mi_real_one", "mi_real_two"}:
        failures.append(f"sys.rs extern parsing: got {sorted(declared)}")

    # C names nest, and a substring test would call the shorter one wrapped.
    nested = "let x = unsafe { sys::mi_prof_start_seeded(0, 1) };"
    if is_called_from_lib("mi_prof_start", nested):
        failures.append("call-site matching: `mi_prof_start_seeded` satisfied `mi_prof_start`")
    if not is_called_from_lib("mi_prof_start_seeded", nested):
        failures.append("call-site matching: missed a real `sys::mi_prof_start_seeded` call")

    # The parsers must find something real in the actual tree, too: a regex that matches
    # nothing reports "clean" just as loudly as one that works.
    for relative in FORK_ONLY_HEADERS:
        if not header_exports(INCLUDE_DIR / relative):
            failures.append(f"{relative}: parsed zero exports from a real fork header")
    if len(sys_declared_functions(SYS_RS.read_text(encoding="utf-8"))) < 20:
        failures.append("sys.rs: parsed suspiciously few extern declarations")

    if failures:
        for failure in failures:
            print(f"check_rust_surface --selftest: {failure}", file=sys.stderr)
        return 1
    print("check_rust_surface --selftest: parsers OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check this script's own parsing against fixtures and exit",
    )
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    return check(verbose=not args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
