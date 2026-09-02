"""Unit tests for ci/check_bun_surface.py (issue #274, Bun parity P9a).

`parse_missing_symbols` is the one piece of this gate's logic that is worth testing in
isolation: it is the difference between "link failed, good luck" and a CI log that names
the exact `mi_*` symbol Bun would fail to link against. The rest of the script is
subprocess orchestration (compile, link, run, nm -u) that is exercised for real by
`ci/check_bun_surface.py` itself in CI (see `.github/workflows/c-unit.yml`, job
`bun-surface`) -- there is little point re-mocking a compiler invocation here when the
real one runs on every PR.
"""

from __future__ import annotations

import unittest

from check_bun_surface import parse_missing_symbols

GNU_LD_SINGLE = (
    "/usr/bin/ld: test-bun-surface.o:(.data.rel.ro+0xd8): "
    "undefined reference to `mi_on_thread_idle'\n"
    "collect2: error: ld returned 1 exit status\n"
)

GNU_LD_MULTIPLE = (
    "/usr/bin/ld: a.o: in function `main':\n"
    "a.o:(.text+0x10): undefined reference to `mi_on_thread_idle'\n"
    "a.o:(.text+0x20): undefined reference to `mi_heap_dump_json'\n"
    "collect2: error: ld returned 1 exit status\n"
)

GNU_LD_DUPLICATE_REFS = (
    # The real linker repeats a symbol once per call site; the parser must dedupe.
    "a.o:(.data.rel.ro+0x8): undefined reference to `mi_on_thread_idle'\n"
    "b.o:(.data.rel.ro+0x18): undefined reference to `mi_on_thread_idle'\n"
)

LLD_MACHO_STYLE = (
    "Undefined symbols for architecture x86_64:\n"
    '  "_mi_on_thread_idle", referenced from:\n'
    "      _main in test-bun-surface.o\n"
)

MIXED_NON_MI_SYMBOL = (
    "/usr/bin/ld: a.o: undefined reference to `pthread_create'\n"
    "/usr/bin/ld: a.o: undefined reference to `mi_on_thread_idle'\n"
)

CLEAN_LINK = "collect2: error: ld returned 1 exit status\n"  # no undefined-reference lines at all


class ParseMissingSymbolsTest(unittest.TestCase):
    def test_single_gnu_ld_symbol(self):
        self.assertEqual(parse_missing_symbols(GNU_LD_SINGLE), ["mi_on_thread_idle"])

    def test_multiple_gnu_ld_symbols_sorted(self):
        self.assertEqual(
            parse_missing_symbols(GNU_LD_MULTIPLE),
            ["mi_heap_dump_json", "mi_on_thread_idle"],
        )

    def test_duplicate_call_sites_deduped(self):
        self.assertEqual(parse_missing_symbols(GNU_LD_DUPLICATE_REFS), ["mi_on_thread_idle"])

    def test_macho_style_leading_underscore_stripped(self):
        self.assertEqual(parse_missing_symbols(LLD_MACHO_STYLE), ["mi_on_thread_idle"])

    def test_non_mi_symbols_filtered_out(self):
        # pthread_create is a real problem too, but this gate exists to report the
        # mi_* surface specifically -- see the module docstring's rationale.
        self.assertEqual(parse_missing_symbols(MIXED_NON_MI_SYMBOL), ["mi_on_thread_idle"])

    def test_no_undefined_reference_lines_returns_empty(self):
        self.assertEqual(parse_missing_symbols(CLEAN_LINK), [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(parse_missing_symbols(""), [])


if __name__ == "__main__":
    unittest.main()
