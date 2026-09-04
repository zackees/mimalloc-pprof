"""Tests for ci/render_feature_table.py and for the real feature data it renders.

Two halves, deliberately:

* `RendererTests` exercise the renderer against a small inline fixture. They must not
  read `docs/allocator-features.json` -- a renderer test that depends on the real data
  starts failing every time a feature row is edited, which trains people to ignore it.
* `RealDataTests` do the opposite: they read the real file and enforce the rules the
  table's credibility rests on -- every cell cites a source, and every gap in the
  `mimalloc-pprof` column names a tracking issue. Those are the rules a reviewer
  cannot check by eye across 168 cells.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

import render_feature_table as rft

ROOT = Path(__file__).resolve().parents[2]

ISSUE_RE = re.compile(r"https://github\.com/zackees/mimalloc-pprof/issues/\d+")

FIXTURE: dict[str, Any] = {
    "caption": "a caption",
    "legend": "a legend",
    "allocators": [
        {"key": "mimalloc-pprof", "label": "mimalloc-pprof", "version": "0.9"},
        {"key": "upstream", "label": "upstream", "version": "dev3"},
    ],
    "groups": [
        {
            "name": "Memory return",
            "rows": [
                {
                    "feature": "Per-thread purge",
                    "cells": {
                        "mimalloc-pprof": {"status": "yes", "note": "", "source": "a.h:1"},
                        "upstream": {"status": "partial", "note": "collect", "source": "b.h:2"},
                    },
                },
                {
                    "feature": "Returned after idle",
                    "cells": {
                        "mimalloc-pprof": {"status": "value", "note": "74 %", "source": "c"},
                        "upstream": {"status": "value", "note": "0 %", "source": "d"},
                    },
                },
            ],
        }
    ],
}


def fixture_doc(**overrides: Any) -> rft.FeatureDoc:
    data = json.loads(json.dumps(FIXTURE))
    data.update(overrides)
    return rft.parse_doc(json.dumps(data))


class ParseTests(unittest.TestCase):
    def test_parses_the_fixture(self) -> None:
        doc = fixture_doc()
        self.assertEqual(doc.keys, ("mimalloc-pprof", "upstream"))
        self.assertEqual(len(doc.all_rows()), 2)

    def test_subject_must_be_the_first_column(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["allocators"].reverse()
        with self.assertRaises(rft.FeatureDataError):
            rft.parse_doc(json.dumps(data))

    def test_unknown_status_is_rejected_by_name(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["cells"]["upstream"]["status"] = "maybe"
        with self.assertRaises(rft.FeatureDataError) as ctx:
            rft.parse_doc(json.dumps(data))
        self.assertIn("Per-thread purge", str(ctx.exception))

    def test_a_missing_cell_is_rejected(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        del data["groups"][0]["rows"][0]["cells"]["upstream"]
        with self.assertRaises(rft.FeatureDataError):
            rft.parse_doc(json.dumps(data))

    def test_a_cell_for_an_unknown_allocator_is_rejected(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["cells"]["tcmalloc"] = {
            "status": "yes",
            "note": "",
            "source": "x",
        }
        with self.assertRaises(rft.FeatureDataError):
            rft.parse_doc(json.dumps(data))

    def test_duplicate_feature_names_are_rejected(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][1]["feature"] = "Per-thread purge"
        with self.assertRaises(rft.FeatureDataError):
            rft.parse_doc(json.dumps(data))

    def test_a_missing_source_is_rejected(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        del data["groups"][0]["rows"][0]["cells"]["upstream"]["source"]
        with self.assertRaises(rft.FeatureDataError):
            rft.parse_doc(json.dumps(data))


class OverflowGuardTests(unittest.TestCase):
    """An SVG has no layout engine: a note that is too long renders happily, straight
    over the next column, and nothing fails. So the width budget is checked at parse
    time -- these are the tests that say the guard is armed."""

    def test_an_over_wide_note_is_rejected(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["cells"]["upstream"]["note"] = "x" * 200
        with self.assertRaises(rft.FeatureDataError) as ctx:
            rft.parse_doc(json.dumps(data))
        self.assertIn("too wide", str(ctx.exception))

    def test_an_over_wide_feature_name_is_rejected(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["feature"] = "y" * 200
        with self.assertRaises(rft.FeatureDataError) as ctx:
            rft.parse_doc(json.dumps(data))
        self.assertIn("too wide", str(ctx.exception))

    def test_a_note_exactly_at_the_budget_is_accepted(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["cells"]["upstream"]["note"] = "z" * int(
            rft.MAX_NOTE_PX / rft.NOTE_CHAR_PX
        )
        rft.parse_doc(json.dumps(data))

    def test_the_caption_is_wrapped_rather_than_run_off_the_page(self) -> None:
        lines = rft.wrap_caption("word " * 200)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), rft.CAPTION_CHARS)

    def test_a_short_caption_stays_on_one_line(self) -> None:
        self.assertEqual(rft.wrap_caption("a short caption"), ["a short caption"])


class MarkdownTests(unittest.TestCase):
    def test_subject_header_is_bold_and_first(self) -> None:
        lines = rft.render_markdown(fixture_doc()).splitlines()
        self.assertEqual(lines[0], "| Feature | **mimalloc-pprof** | upstream |")

    def test_glyphs_and_group_rows(self) -> None:
        md = rft.render_markdown(fixture_doc())
        self.assertIn("| **Memory return** | | |", md)
        self.assertIn("| Per-thread purge | ✅ | ⚠️ collect |", md)

    def test_value_rows_carry_no_glyph(self) -> None:
        md = rft.render_markdown(fixture_doc())
        self.assertIn("| Returned after idle | 74 % | 0 % |", md)
        for glyph in rft.MARKDOWN_GLYPH.values():
            self.assertNotIn(f"{glyph} 74 %", md)

    def test_an_issue_reference_becomes_a_link_when_the_source_is_that_issue(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        url = "https://github.com/zackees/mimalloc-pprof/issues/335"
        data["groups"][0]["rows"][0]["cells"]["upstream"] = {
            "status": "no",
            "note": "not yet — #335",
            "source": url,
        }
        md = rft.render_markdown(rft.parse_doc(json.dumps(data)))
        self.assertIn(f"❌ not yet — [#335]({url})", md)

    def test_a_bare_hash_number_is_not_linkified_without_an_issue_source(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["cells"]["upstream"]["note"] = "#335"
        md = rft.render_markdown(rft.parse_doc(json.dumps(data)))
        self.assertIn("⚠️ #335", md)
        self.assertNotIn("](https://", md)

    def test_every_row_has_the_same_column_count(self) -> None:
        md = rft.render_markdown(fixture_doc())
        counts = {line.count("|") for line in md.splitlines()}
        self.assertEqual(counts, {4})


class SvgTests(unittest.TestCase):
    def test_both_themes_render_well_formed_svg(self) -> None:
        for theme in (rft.LIGHT, rft.DARK):
            svg = rft.render_svg(fixture_doc(), theme)
            self.assertTrue(svg.startswith("<svg "))
            self.assertTrue(svg.rstrip().endswith("</svg>"))
            self.assertEqual(svg.count("<svg "), 1)

    def test_the_subject_column_is_the_highlighted_one(self) -> None:
        svg = rft.render_svg(fixture_doc(), rft.LIGHT)
        self.assertIn(f'<rect x="{rft.COL_START}"', svg)
        self.assertIn(rft.LIGHT.highlight, svg)

    def test_themes_differ(self) -> None:
        doc = fixture_doc()
        self.assertNotEqual(rft.render_svg(doc, rft.LIGHT), rft.render_svg(doc, rft.DARK))

    def test_glyphs_are_drawn_not_typed(self) -> None:
        """GitHub serves the SVG through an <img>; an emoji in a <text> renders in
        whatever colour-emoji font the viewer has, or as tofu. Assert the renderer
        never emits one."""
        svg = rft.render_svg(fixture_doc(), rft.DARK)
        for glyph in rft.MARKDOWN_GLYPH.values():
            self.assertNotIn(glyph, svg)
        self.assertIn("<circle", svg)

    def test_markup_in_data_is_escaped(self) -> None:
        data = json.loads(json.dumps(FIXTURE))
        data["groups"][0]["rows"][0]["feature"] = 'a <b> & "c"'
        svg = rft.render_svg(rft.parse_doc(json.dumps(data)), rft.LIGHT)
        self.assertIn("a &lt;b&gt; &amp; &quot;c&quot;", svg)

    def test_rendering_is_deterministic(self) -> None:
        doc = fixture_doc()
        self.assertEqual(rft.render_svg(doc, rft.LIGHT), rft.render_svg(doc, rft.LIGHT))


class SpliceTests(unittest.TestCase):
    def test_only_the_marked_region_is_replaced(self) -> None:
        readme = f"before\n{rft.START_MARKER}\nold\n{rft.END_MARKER}\nafter\n"
        out = rft.splice_readme(readme, f"{rft.START_MARKER}\nnew\n{rft.END_MARKER}")
        self.assertEqual(out, f"before\n{rft.START_MARKER}\nnew\n{rft.END_MARKER}\nafter\n")

    def test_missing_markers_fail_loudly(self) -> None:
        with self.assertRaises(rft.FeatureDataError):
            rft.splice_readme("no markers here\n", "x")

    def test_reversed_markers_fail_loudly(self) -> None:
        readme = f"{rft.END_MARKER}\n{rft.START_MARKER}\n"
        with self.assertRaises(rft.FeatureDataError):
            rft.splice_readme(readme, "x")

    def test_splicing_is_idempotent(self) -> None:
        doc = fixture_doc()
        readme = f"a\n{rft.START_MARKER}\nold\n{rft.END_MARKER}\nb\n"
        once = rft.splice_readme(readme, rft.render_readme_region(doc))
        twice = rft.splice_readme(once, rft.render_readme_region(doc))
        self.assertEqual(once, twice)


class RealDataTests(unittest.TestCase):
    """The rules the table's credibility rests on, checked against the real file."""

    doc: rft.FeatureDoc

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = rft.load_doc()

    def test_every_cell_cites_a_source(self) -> None:
        for row in self.doc.all_rows():
            for key, cell in row.cells.items():
                self.assertTrue(
                    cell.source.strip(),
                    f"row {row.feature!r} cell {key!r} has no source; an unsourced cell is a "
                    "guess, and this table exists to not be a pile of guesses",
                )

    def test_every_mimalloc_pprof_gap_names_a_tracking_issue(self) -> None:
        """A ❌ or ⚠️ in the subject column is a TODO. Without a linked issue it is
        just an admission with nowhere to go -- which is exactly what #335 was filed
        to avoid for the process-wide purge row."""
        for row in self.doc.all_rows():
            cell = row.cells[rft.SUBJECT]
            if cell.status in ("no", "partial"):
                self.assertRegex(
                    cell.note + " " + cell.source,
                    ISSUE_RE,
                    f"row {row.feature!r}: mimalloc-pprof is {cell.status!r} but neither its "
                    "note nor its source links a mimalloc-pprof issue",
                )

    def test_the_measured_row_matches_the_readme_chart(self) -> None:
        """The one row that is a measurement rather than a capability must agree with
        the committed benchmark report the README's chart is rendered from, or the two
        halves of the README contradict each other."""
        report = json.loads(
            (ROOT / ".github" / "assets" / "allocator-idle-report.json").read_text(encoding="utf-8")
        )
        series: dict[str, Any] = report["series"]
        measured = [r for r in self.doc.all_rows() if r.cells[rft.SUBJECT].status == "value"]
        self.assertTrue(measured, "no measured row found in the feature table")
        for key, series_key in (
            ("mimalloc-pprof", "mimalloc-pprof"),
            ("bun", "bun-mimalloc"),
            ("upstream", "upstream-mimalloc"),
            ("jemalloc", "jemalloc"),
        ):
            pct = round(float(series[series_key]["percent_returned"]))
            for row in measured:
                self.assertIn(
                    f"{pct} %",
                    row.cells[key].note,
                    f"row {row.feature!r} cell {key!r} disagrees with "
                    f".github/assets/allocator-idle-report.json ({pct} %)",
                )

    def test_group_names_are_the_ones_the_readme_promises(self) -> None:
        self.assertEqual(
            [g.name for g in self.doc.groups],
            [
                "Memory return",
                "Profiling and observability",
                "Robustness",
                "Platform and integration",
                "Allocator design",
            ],
        )

    def test_the_committed_outputs_are_current(self) -> None:
        """`--check` in unit-test form: if this fails, run the renderer and commit."""
        readme = rft.README.read_text(encoding="utf-8")
        for path, body in rft.outputs(self.doc, readme):
            self.assertTrue(path.exists(), f"{path} has never been rendered")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                body,
                f"{path.name} is stale -- run `uv run ci/render_feature_table.py`",
            )


if __name__ == "__main__":
    unittest.main()
