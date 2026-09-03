from __future__ import annotations

import unittest
from pathlib import Path

import check_rust_surface as surface

ROOT = Path(__file__).resolve().parents[2]


class CheckRustSurfaceTests(unittest.TestCase):
    def test_the_tree_is_clean(self) -> None:
        """The gate itself: every fork C API is reachable from the Rust crate."""
        self.assertEqual(surface.check(verbose=False), 0)

    def test_selftest_passes(self) -> None:
        self.assertEqual(surface.selftest(), 0)

    def test_comments_do_not_look_like_exports(self) -> None:
        """include/mimalloc.h discusses prototypes in prose; a raw regex would find them."""
        source = "/* mi_decl_export void mi_ghost(void); */\nmi_decl_export void mi_real(void);"
        self.assertEqual(surface.exports_in_source(source), {"mi_real"})

    def test_fork_headers_parse_to_something(self) -> None:
        """A regex that matches nothing reports 'clean' exactly as loudly as one that works."""
        for relative in surface.FORK_ONLY_HEADERS:
            with self.subTest(header=str(relative)):
                self.assertTrue(surface.header_exports(surface.INCLUDE_DIR / relative))

    def test_the_forks_own_options_are_mirrored_in_order(self) -> None:
        header = (surface.INCLUDE_DIR / "mimalloc.h").read_text(encoding="utf-8")
        options = surface.option_enumerators(header)
        self.assertEqual(options[-1], "_mi_option_last")
        # The thirteen this fork inserts, contiguously, ahead of the sentinel.
        fork_block = [
            "mi_option_prof",
            "mi_option_prof_sample_rate",
            "mi_option_prof_bt_max",
            "mi_option_prof_accum",
            "mi_option_prof_seed",
            "mi_option_prof_max_bytes",
            "mi_option_memory_events",
            "mi_option_purge_zeroes",
            "mi_option_scavenger",
            "mi_option_purge_holes",
            "mi_option_purge_holes_eager_zero",
            "mi_option_purge_holes_min_interval",
            "mi_option_purge_holes_full_every",
        ]
        self.assertEqual(options[-len(fork_block) - 1 : -1], fork_block)

    def test_a_nested_name_does_not_count_as_a_call_site(self) -> None:
        """C names nest, and a substring test would call the shorter one wrapped.

        `sys::mi_prof_start_seeded` contains `sys::mi_prof_start`, `sys::mi_option_get_clamp`
        contains `sys::mi_option_get`, and so on -- so a plain `in` test would report an
        UNwrapped function as wrapped on the strength of its longer sibling's call site.
        """
        source = "let x = unsafe { sys::mi_prof_start_seeded(0, 1) };"
        self.assertFalse(surface.is_called_from_lib("mi_prof_start", source))
        self.assertTrue(surface.is_called_from_lib("mi_prof_start_seeded", source))
        clamp = "options::get_clamp(unsafe { sys::mi_option_get_clamp(o, a, b) })"
        self.assertFalse(surface.is_called_from_lib("mi_option_get", clamp))
        self.assertTrue(surface.is_called_from_lib("mi_option_get_clamp", clamp))

    def test_a_doc_comment_mention_is_not_a_call_site(self) -> None:
        """lib.rs names the C functions it wraps in prose; prose must not count.

        If a wrapper were deleted but its doc comment left behind, a raw text search
        would still report the function as wrapped -- the gate would pass on a sentence.
        """
        described_only = (
            "/// Stops the profiler. Thin wrapper around `sys::mi_prof_stop`.\n"
            "//! See sys::mi_prof_stop for the raw declaration.\n"
            "/* was: unsafe { sys::mi_prof_stop() } */\n"
        )
        self.assertFalse(surface.is_called_from_lib("mi_prof_stop", described_only))
        self.assertTrue(
            surface.is_called_from_lib("mi_prof_stop", "unsafe { sys::mi_prof_stop() }")
        )

    def test_a_url_in_source_does_not_swallow_the_rest_of_its_line(self) -> None:
        """The trailing-comment rule must not treat the `//` of `https://` as a comment."""
        line = 'let _ = "https://example.invalid/x"; unsafe { sys::mi_prof_reset() }'
        self.assertTrue(surface.is_called_from_lib("mi_prof_reset", line))

    def test_a_shifted_mirror_is_caught(self) -> None:
        """The negative control that matters.

        A mirror that dropped one enumerator still contains every *name* the header does,
        so a set comparison would call it clean -- while every option past the drop point
        resolves to a different option than the caller named. This must fail.
        """
        header_options = ["mi_option_a", "mi_option_b", "mi_option_c", "_mi_option_last"]
        shifted = [("mi_option_a", 0), ("mi_option_c", 1), ("_mi_option_last", 2)]
        problems = surface.option_mirror_problems(header_options, shifted)
        self.assertTrue(problems)
        self.assertIn("diverges at index 1", problems[0])

    def test_a_correct_mirror_is_accepted(self) -> None:
        header_options = ["mi_option_a", "mi_option_b", "_mi_option_last"]
        good = [("mi_option_a", 0), ("mi_option_b", 1), ("_mi_option_last", 2)]
        self.assertEqual(surface.option_mirror_problems(header_options, good), [])

    def test_a_renumbered_mirror_is_caught(self) -> None:
        """Right names, wrong values: still the wrong option at runtime."""
        header_options = ["mi_option_a", "mi_option_b", "_mi_option_last"]
        renumbered = [("mi_option_a", 0), ("mi_option_b", 7), ("_mi_option_last", 2)]
        problems = surface.option_mirror_problems(header_options, renumbered)
        self.assertTrue(any("mirrors `mi_option_b` as 7" in p for p in problems))

    def test_every_allowlist_entry_carries_a_reason(self) -> None:
        for name, reason in {
            **surface.UNBOUND_WITH_REASON,
            **surface.SYS_ONLY_WITH_REASON,
        }.items():
            with self.subTest(name=name):
                self.assertGreater(len(reason), 40, f"{name}: the reason is not a reason")

    def test_the_named_fork_additions_are_real_exports(self) -> None:
        """The literal list stands in for a diff against upstream; keep it honest."""
        exported: set[str] = set()
        for relative in surface.SHARED_HEADERS:
            exported |= surface.header_exports(surface.INCLUDE_DIR / relative)
        missing = surface.FORK_ADDITIONS_IN_UPSTREAM_HEADERS - exported
        self.assertEqual(missing, set(), f"no longer exported by C: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
