#!/usr/bin/env python3
"""Reject unclassified or semantically changed internal-state allocation sites.

This is intentionally a small lexical checker, not a C parser. It strips comments
and literals, balances call parentheses, and compares normalized callee/argument
signatures with ci/internal-state-inventory.json. The selected APIs are the owning
allocation boundaries used for long-lived allocator state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOCATORS = (
    "_mi_arenas_alloc",
    "_mi_arenas_alloc_aligned",
    "_mi_meta_realloc",
    "_mi_meta_rezalloc",
    "_mi_meta_zalloc",
    "_mi_meta_zalloc_aligned",
    "_mi_os_alloc",
    "_mi_os_alloc_aligned",
    "_mi_os_alloc_aligned_at_offset",
    "_mi_os_alloc_huge_os_pages",
    "_mi_os_zalloc",
    "_mi_prof_arena_alloc",
    "mi_calloc",
    "mi_heap_calloc",
    "mi_heap_malloc",
    "mi_heap_realloc",
    "mi_heap_rezalloc",
    "mi_heap_zalloc",
    "mi_heap_zalloc_aligned",
    "mi_malloc",
    "mi_realloc",
    "mi_rezalloc",
    "mi_zalloc",
)

# These files implement the allocation primitives or public allocation wrappers;
# calls there are the mechanism itself, not allocator-owned state objects.
EXCLUDED = {
    "src/alloc-aligned.c",
    "src/alloc-override.c",
    "src/alloc-posix.c",
    "src/alloc.c",
    "src/os.c",
}
REQUIRED_FIELDS = {
    "id",
    "path",
    "callee",
    "args",
    "occurrence",
    "owner",
    "initial_owner",
    "owner_transitions",
    "destroyable_by",
    "lifetime",
    "logical_size",
    "usable_size",
    "cleanup",
    "publication",
    "locks",
}


@dataclass(frozen=True)
class Site:
    path: str
    callee: str
    args: str
    line: int
    occurrence: int = 1

    @property
    def signature(self) -> tuple[str, str, str, int]:
        return (self.path, self.callee, self.args, self.occurrence)


def strip_c(text: str) -> str:
    """Replace comments and literal contents with spaces while retaining lines."""
    out = list(text)
    i = 0
    state = "code"
    quote = ""
    while i < len(text):
        c = text[i]
        n = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if c == "/" and n == "*":
                out[i] = out[i + 1] = " "
                state = "block"
                i += 2
                continue
            if c == "/" and n == "/":
                out[i] = out[i + 1] = " "
                state = "line"
                i += 2
                continue
            if c in {'"', "'"}:
                quote = c
                state = "literal"
        elif state == "block":
            if c == "*" and n == "/":
                out[i] = out[i + 1] = " "
                state = "code"
                i += 2
                continue
            if c != "\n":
                out[i] = " "
        elif state == "line":
            if c == "\n":
                state = "code"
            else:
                out[i] = " "
        else:
            if c == "\\":
                out[i] = " "
                if i + 1 < len(text) and text[i + 1] != "\n":
                    out[i + 1] = " "
                i += 2
                continue
            if c == quote:
                state = "code"
            elif c != "\n":
                out[i] = " "
        i += 1
    return "".join(out)


def normalize_args(args: str) -> str:
    return re.sub(r"\s+", "", args)


def calls_in(path: str, text: str) -> list[Site]:
    clean = strip_c(text)
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, ALLOCATORS)) + r")\s*\(")
    sites: list[Site] = []
    occurrences: dict[tuple[str, str], int] = {}
    for match in pattern.finditer(clean):
        start = match.end() - 1
        depth = 0
        end = start
        while end < len(clean):
            if clean[end] == "(":
                depth += 1
            elif clean[end] == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth != 0:
            raise ValueError(f"{path}:{clean.count(chr(10), 0, start) + 1}: unbalanced call")

        # Ignore a function definition. All selected primitive definitions are also
        # in EXCLUDED, but this keeps the scanner honest if one moves.
        after = clean[end + 1 :].lstrip()
        line_start = clean.rfind("\n", 0, match.start()) + 1
        prefix = clean[line_start : match.start()].strip()
        if after.startswith("{") and "=" not in prefix and "return" not in prefix:
            continue

        call_key = (match.group(1), normalize_args(clean[start + 1 : end]))
        occurrences[call_key] = occurrences.get(call_key, 0) + 1
        sites.append(
            Site(
                path=path,
                callee=call_key[0],
                args=call_key[1],
                line=clean.count("\n", 0, match.start()) + 1,
                occurrence=occurrences[call_key],
            )
        )
    return sites


def observe(root: Path) -> list[Site]:
    sites: list[Site] = []
    for source in sorted((root / "src").rglob("*.c")):
        rel = source.relative_to(root).as_posix()
        if rel in EXCLUDED or rel.startswith("src/prim/"):
            continue
        sites.extend(calls_in(rel, source.read_text(encoding="utf-8")))
    return sites


def validate(entries: list[dict[str, object]], observed: list[Site]) -> list[str]:
    errors: list[str] = []
    expected: dict[tuple[str, str, str, int], str] = {}
    for index, entry in enumerate(entries):
        missing = REQUIRED_FIELDS - entry.keys()
        if missing:
            errors.append(f"inventory entry {index} is missing: {', '.join(sorted(missing))}")
            continue
        key = (
            str(entry["path"]),
            str(entry["callee"]),
            str(entry["args"]),
            int(str(entry["occurrence"])),
        )
        if key in expected:
            errors.append(f"duplicate inventory signature: {key} ({expected[key]}, {entry['id']})")
        expected[key] = str(entry["id"])
        if not isinstance(entry["locks"], list):
            errors.append(f"{entry['id']}: locks must be a JSON list")
        for field in REQUIRED_FIELDS - {"locks"}:
            if not str(entry[field]).strip():
                errors.append(f"{entry['id']}: {field} must not be empty")

    seen: dict[tuple[str, str, str, int], Site] = {}
    for site in observed:
        if site.signature in seen:
            first = seen[site.signature]
            errors.append(
                f"duplicate observed signature at {site.path}:{first.line} and {site.line}; "
                "make the sites semantically distinct"
            )
        seen[site.signature] = site
        if site.signature not in expected:
            errors.append(
                f"unclassified internal-state allocation at {site.path}:{site.line}: "
                f"{site.callee}({site.args}) [occurrence {site.occurrence}]"
            )
    for key, site_id in expected.items():
        if key not in seen:
            errors.append(
                f"classified site {site_id} disappeared or changed semantics: "
                f"{key[0]}: {key[1]}({key[2]}) [occurrence {key[3]}]"
            )
    return errors


def selftest(entries: list[dict[str, object]], observed: list[Site]) -> None:
    if validate(entries, observed):
        raise AssertionError("real inventory must validate before self-test")

    for allocator in ALLOCATORS:
        added = [*observed, Site("src/new-state.c", allocator, "owner,64,&memid", 7)]
        assert any("unclassified" in error for error in validate(entries, added)), allocator

    tls_index = next(
        i
        for i, site in enumerate(observed)
        if site.path == "src/threadlocal.c"
        and site.callee == "mi_heap_rezalloc"
        and site.args.startswith("mi_heap_main()")
    )
    changed = list(observed)
    old = changed[tls_index]
    changed[tls_index] = Site(
        old.path,
        old.callee,
        "_mi_theap_heap(mi_theap_get_default()),tls_old,64",
        old.line,
        old.occurrence,
    )
    changed_errors = validate(entries, changed)
    assert any("unclassified" in error for error in changed_errors)
    assert any("disappeared or changed semantics" in error for error in changed_errors)

    removed = observed[:tls_index] + observed[tls_index + 1 :]
    assert any("disappeared" in error for error in validate(entries, removed))
    print("internal-state inventory positive controls passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dump", action="store_true", help="print observed signatures")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    inventory_path = root / "ci" / "internal-state-inventory.json"
    entries = json.loads(inventory_path.read_text(encoding="utf-8"))["sites"]
    observed = observe(root)
    if args.dump:
        for site in observed:
            print(
                f"{site.path}:{site.line} {site.callee}({site.args}) occurrence={site.occurrence}"
            )
        return 0
    errors = validate(entries, observed)
    if errors:
        print("internal-state inventory check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    if args.selftest:
        selftest(entries, observed)
    else:
        print(f"internal-state inventory is complete ({len(observed)} sites)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
