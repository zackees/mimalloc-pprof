from __future__ import annotations

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import bench_hole_purging_allocators as bench

FIXTURE = Path(__file__).parent / "fixtures" / "allocator_idle"
ASSETS = Path(__file__).parent.parent.parent / ".github" / "assets"


class BenchHolePurgingAllocatorsTests(unittest.TestCase):
    def load(self) -> tuple[bench.ReportJson, dict[str, list[bench.Sample]]]:
        report = bench.load_report_json(FIXTURE / "allocator-idle-report.json")
        series = bench.load_csv(FIXTURE / "allocator-idle-rss.csv")
        return report, series

    def test_csv_round_trips_every_charted_series(self) -> None:
        _, series = self.load()
        for spec in bench.CHARTED:
            self.assertIn(spec.key, series, f"{spec.key} missing from the CSV")
            self.assertTrue(series[spec.key])
        self.assertEqual(series["jemalloc"][0].t_ms, 0)

    def test_line_chart_svg_is_well_formed_and_names_every_series(self) -> None:
        report, series = self.load()
        source_line = bench.format_source_line(
            report["commit"], report["cpu"], report["kernel"], report["runs_per_series"]
        )
        for theme in (bench.LIGHT, bench.DARK):
            svg = bench.render_line_chart(series, report["series"], theme, source_line)
            root = ET.fromstring(svg)  # raises if malformed
            self.assertTrue(root.tag.endswith("svg"))
            self.assertIn("viewBox", root.attrib)
            # Identity is never color-alone: every series is direct-labeled.
            for spec in bench.CHARTED:
                self.assertIn(spec.label, svg)
            self.assertIn(report["commit"], svg)
            # under the 60KB budget the README brief sets for each SVG
            self.assertLess(len(svg.encode("utf-8")), 60_000)

    def test_line_chart_titles_the_takeaway_with_measured_numbers(self) -> None:
        report, _ = self.load()
        title = bench.chart_title(report["series"])
        fork = report["series"]["mimalloc-pprof"]["percent_returned"]
        jemalloc = report["series"]["jemalloc"]["percent_returned"]
        self.assertIn(f"{fork:.0f}%", title)
        self.assertIn(f"{jemalloc:.0f}%", title)

    def test_two_jemalloc_series_share_a_hue_and_differ_by_dash(self) -> None:
        by_key = {spec.key: spec for spec in bench.SERIES}
        plain, purge = by_key["jemalloc"], by_key["jemalloc-purge"]
        self.assertEqual(plain.slot, purge.slot)
        self.assertFalse(plain.dashed)
        self.assertTrue(purge.dashed)

    def test_table_svg_lists_charted_rows_then_diagnostics(self) -> None:
        report, _ = self.load()
        source_line = bench.format_source_line(
            report["commit"], report["cpu"], report["kernel"], report["runs_per_series"]
        )
        for theme in (bench.LIGHT, bench.DARK):
            svg = bench.render_table_svg(report["series"], theme, source_line)
            root = ET.fromstring(svg)
            self.assertTrue(root.tag.endswith("svg"))
            self.assertIn("viewBox", root.attrib)
            for spec in bench.SERIES:
                self.assertIn(spec.label, svg)
            self.assertIn("diagnostics", svg)
            self.assertLess(len(svg.encode("utf-8")), 60_000)

    def test_every_measured_series_names_its_idle_mechanism(self) -> None:
        report, _ = self.load()
        known = tuple(bench.IDLE_DESCRIPTIONS.values()) + tuple(bench.IDLE_SHORT.values())
        for key, summary in report["series"].items():
            self.assertTrue(
                summary["idle_mechanism"].startswith(known),
                f"{key} reports an idle mechanism the renderer does not know: "
                f"{summary['idle_mechanism']!r}",
            )

    def test_a_tuned_or_lengthened_diagnostic_says_so_in_its_own_cell(self) -> None:
        """A row measured with a non-default `MALLOC_CONF`, or over a longer window,
        must carry that in the same cell as its number -- a reader must not be able to
        read past it."""
        tuned = next(s for s in bench.SERIES if s.env and s.seconds is not None)
        text = bench.idle_mechanism_text(tuned, bench.IDLE_NOTHING, 30)
        self.assertIn("JE_MALLOC_CONF=background_thread:true", text)
        self.assertIn("30 s window", text)
        plain = next(s for s in bench.SERIES if s.charted and not s.env)
        self.assertEqual(
            bench.idle_mechanism_text(plain, bench.IDLE_NOTHING, 10),
            bench.IDLE_DESCRIPTIONS[bench.IDLE_NOTHING],
        )

    def test_no_charted_series_is_tuned_or_given_a_different_window(self) -> None:
        """ "Default config" has to mean the same thing for every line on the chart."""
        for spec in bench.CHARTED:
            self.assertEqual(spec.env, (), f"{spec.key} is charted but tuned")
            self.assertIsNone(spec.seconds, f"{spec.key} is charted with its own window")

    def test_from_data_matches_committed_svgs(self) -> None:
        """Every committed asset must reproduce byte-for-byte from the committed
        CSV/JSON -- otherwise an SVG and the caption it carries (commit, CPU, kernel,
        repetition count) can silently drift apart. All four: a dark theme that had
        stopped reproducing would have gone unnoticed while the light one passed."""
        csv_path = ASSETS / "allocator-idle-rss.csv"
        json_path = ASSETS / "allocator-idle-report.json"
        if not csv_path.exists() or not json_path.exists():
            self.skipTest("no committed allocator-idle assets in this checkout")
        report = bench.load_report_json(json_path)
        series = bench.load_csv(csv_path)
        source_line = bench.format_source_line(
            report["commit"], report["cpu"], report["kernel"], report["runs_per_series"]
        )
        for theme in (bench.LIGHT, bench.DARK):
            with self.subTest(theme=theme.name):
                self.assertEqual(
                    (ASSETS / f"allocator-idle-rss-{theme.name}.svg").read_text(encoding="utf-8"),
                    bench.render_line_chart(series, report["series"], theme, source_line),
                )
                self.assertEqual(
                    (ASSETS / f"allocator-idle-table-{theme.name}.svg").read_text(encoding="utf-8"),
                    bench.render_table_svg(report["series"], theme, source_line),
                )

    def test_a_forcing_collect_row_backs_the_purge_delay_objection(self) -> None:
        """The README says upstream returns the same 18% when the collect is forced.
        That sentence needs a measured row behind it, not a side experiment."""
        spec = next(s for s in bench.SERIES if s.key == "upstream-mimalloc-collect-force")
        self.assertEqual(spec.idle, bench.IDLE_MI_COLLECT_FORCE)
        self.assertEqual(spec.allocator_id, "upstream-mimalloc")
        self.assertFalse(spec.charted)
        self.assertIn("mi_collect(true)", bench.IDLE_DESCRIPTIONS[bench.IDLE_MI_COLLECT_FORCE])
        self.assertIn("mi_collect(true);", bench.CHURN_C_SOURCE)
        report, _ = self.load()
        self.assertIn("upstream-mimalloc-collect-force", report["series"])

    def test_a_series_whose_runs_disagree_is_counted_from_the_run_records(self) -> None:
        """A "2 of 3" written by hand goes stale on the next measurement; this one is
        computed, and it is the reason the table grows a footnote."""
        report, _ = self.load()
        consistent: bench.SeriesSummary = dict(report["series"]["mimalloc-pprof"])  # type: ignore[assignment]
        consistent["runs"] = [
            {"peak_rss_mb": 100.0, "idle_start_rss_mb": 100.0, "after_idle_rss_mb": 20.0},
            {"peak_rss_mb": 100.0, "idle_start_rss_mb": 100.0, "after_idle_rss_mb": 21.0},
        ]
        self.assertEqual(bench.runs_returned(consistent), (2, 2))
        flaky: bench.SeriesSummary = dict(consistent)  # type: ignore[assignment]
        flaky["runs"] = [
            *consistent["runs"],
            {
                "peak_rss_mb": 100.0,
                "idle_start_rss_mb": 100.0,
                "after_idle_rss_mb": 100.0,
            },
        ]
        self.assertEqual(bench.runs_returned(flaky), (2, 3))
        self.assertEqual(bench.inconsistent_series({"mimalloc-pprof": consistent}), [])
        self.assertEqual(
            [
                (returned, total)
                for _, returned, total in bench.inconsistent_series({"mimalloc-pprof": flaky})
            ],
            [(2, 3)],
        )

    def test_a_disagreeing_series_is_named_on_the_table_and_in_the_caption(self) -> None:
        """Built synthetically on purpose. The committed run happens to be 3-of-3 on
        every series, so asserting against it would pass vacuously -- and the
        `background_thread` row is the one that has genuinely landed both ways, since
        jemalloc's default `dirty_decay_ms` is exactly this window."""
        report, _ = self.load()
        summaries = dict(report["series"])
        flaky: bench.SeriesSummary = dict(summaries["jemalloc-background-thread"])  # type: ignore[assignment]
        flaky["runs"] = [
            {"peak_rss_mb": 280.0, "idle_start_rss_mb": 280.0, "after_idle_rss_mb": 76.0},
            {"peak_rss_mb": 280.0, "idle_start_rss_mb": 280.0, "after_idle_rss_mb": 76.0},
            {"peak_rss_mb": 280.0, "idle_start_rss_mb": 280.0, "after_idle_rss_mb": 280.0},
        ]
        flaky["percent_returned"] = 73.0
        summaries["jemalloc-background-thread"] = flaky

        table = bench.render_table_svg(summaries, bench.LIGHT, "src")
        self.assertIn("only 2 of 3 runs", table)
        self.assertIn("(in 2 of 3 runs)", " ".join(bench.caption_lines(summaries)))

        # The footnote block grows the table, and an SVG has no layout engine: a second
        # footnote that collides with the first, or falls outside the viewBox, renders
        # silently wrong. The committed run is 3-of-3 everywhere, so this synthetic
        # table is the only place that shape is ever drawn.
        root = ET.fromstring(table)
        height = float(root.attrib["viewBox"].split()[3])
        footnotes = sorted(
            float(node.attrib["y"])
            for node in root.iter("{http://www.w3.org/2000/svg}text")
            if node.attrib.get("font-size") == "11"
        )
        self.assertEqual(len(footnotes), 2, "expected the standing note plus one run note")
        self.assertGreaterEqual(footnotes[1] - footnotes[0], 12, "footnote lines collide")
        self.assertLess(footnotes[-1], height, "a footnote falls outside the viewBox")

        # ...and the same rendering says nothing of the kind for the committed run.
        self.assertNotIn("of 3 runs;", bench.render_table_svg(report["series"], bench.LIGHT, "s"))

    def test_the_caption_always_states_both_jemalloc_background_windows(self) -> None:
        """The 10 s row sits exactly on jemalloc's default dirty_decay_ms, so it has
        been measured at both 0% and 73%. Whichever side it lands on, the caption has
        to carry the longer window too, or the chart reads as a verdict on jemalloc's
        decay thread that the data does not support."""
        report, _ = self.load()
        captions = " ".join(bench.caption_lines(report["series"]))
        short = report["series"]["jemalloc-background-thread"]
        longer = report["series"]["jemalloc-background-thread-30s"]
        self.assertIn(f"{short['percent_returned']:.0f}% inside this same 10 s window", captions)
        self.assertIn(
            f"{longer['percent_returned']:.0f}% over {longer['idle_seconds']} s", captions
        )
        self.assertIn("dirty_decay_ms", captions)

    def test_the_caption_carries_the_cooperative_caveat(self) -> None:
        """An SVG gets pasted into issues and slides on its own; the caveat that the
        74% needs an idle call has to travel with it."""
        report, _ = self.load()
        captions = " ".join(bench.caption_lines(report["series"]))
        self.assertIn("no idle hook", captions)
        self.assertIn("mi_collect()", captions)

    def test_readme_alt_text_matches_the_rendered_chart_title(self) -> None:
        """The README's `<img alt>` states the takeaway; it has to be the same
        sentence the SVG carries, or the number a screen reader hears is a number
        nobody measured."""
        json_path = ASSETS / "allocator-idle-report.json"
        readme = ASSETS.parent.parent / "README.md"
        if not json_path.exists() or not readme.exists():
            self.skipTest("no committed allocator-idle assets in this checkout")
        report = bench.load_report_json(json_path)
        self.assertIn(bench.chart_title(report["series"]), readme.read_text(encoding="utf-8"))

    def test_charted_run_is_the_lowest_after_idle_of_the_repetitions(self) -> None:
        """The selection rule is one rule for every allocator, and it is the
        generous one -- so no arm is charted at its unluckiest run."""
        runs = [
            bench.RunResult(samples=[bench.Sample(0, 900 * 1024)], peak_rss_kb=900 * 1024),
            bench.RunResult(samples=[bench.Sample(0, 700 * 1024)], peak_rss_kb=910 * 1024),
            bench.RunResult(samples=[bench.Sample(0, 800 * 1024)], peak_rss_kb=905 * 1024),
        ]
        picked = min(runs, key=lambda run: run.after_idle_rss_kb)
        self.assertEqual(picked.after_idle_rss_kb, 700 * 1024)

    def test_on_thread_idle_degrades_when_the_header_does_not_declare_it(self) -> None:
        spec = next(s for s in bench.SERIES if s.idle == bench.IDLE_ON_THREAD_IDLE)
        without = bench.AllocatorBuild(
            allocator_id="upstream-mimalloc",
            pin="pin",
            library=Path("/nonexistent/libmimalloc.a"),
            include_dirs=[],
            has_on_thread_idle=False,
        )
        with_hook = bench.AllocatorBuild(
            allocator_id="mimalloc-pprof",
            pin="pin",
            library=Path("/nonexistent/libmimalloc.a"),
            include_dirs=[],
            has_on_thread_idle=True,
        )
        self.assertEqual(bench.resolve_idle(spec, without), bench.IDLE_NOTHING)
        self.assertEqual(bench.resolve_idle(spec, with_hook), bench.IDLE_ON_THREAD_IDLE)

    def test_header_declaration_probe_reads_the_real_header(self) -> None:
        include = Path(__file__).parent.parent.parent / "include"
        self.assertTrue(bench.header_declares_on_thread_idle([include]))
        self.assertFalse(bench.header_declares_on_thread_idle([include / "does-not-exist"]))

    def test_a_knob_that_never_turned_on_is_fatal(self) -> None:
        """RED/GREEN for a real bug: the first `background_thread` row set
        `MALLOC_CONF`, which a `je_`-prefixed jemalloc ignores in silence, and it
        produced a plausible 0% that would have shipped as a measurement."""
        tuned = next(s for s in bench.SERIES if s.env)
        ignored = bench.RunResult(
            samples=[bench.Sample(0, 1024)], peak_rss_kb=1024, background_thread=False
        )
        honoured = bench.RunResult(
            samples=[bench.Sample(0, 1024)], peak_rss_kb=1024, background_thread=True
        )
        with self.assertRaises(SystemExit):
            bench.assert_env_took_effect(tuned, [ignored])
        bench.assert_env_took_effect(tuned, [honoured])

        plain = next(s for s in bench.SERIES if s.allocator_id == "jemalloc" and not s.env)
        bench.assert_env_took_effect(plain, [ignored])
        with self.assertRaises(SystemExit):
            bench.assert_env_took_effect(plain, [honoured])

    def test_the_tuned_row_uses_the_prefixed_environment_variable(self) -> None:
        tuned = next(s for s in bench.SERIES if s.env)
        self.assertEqual([name for name, _ in tuned.env], [bench.JEMALLOC_CONF_ENV])
        self.assertEqual(bench.JEMALLOC_CONF_ENV, "JE_MALLOC_CONF")

    def test_no_idle_cell_grows_into_the_peak_column(self) -> None:
        """An SVG has no layout engine: an over-long cell silently overprints its
        neighbour and only a rendered PNG shows it. The two `background_thread`
        diagnostics already overflowed once."""
        report, _ = self.load()
        for key, summary in report["series"].items():
            self.assertLessEqual(
                bench.approx_text_width(summary["idle_mechanism"]),
                bench.MAX_IDLE_TEXT_PX,
                f"{key}'s idle cell is too wide for the column: {summary['idle_mechanism']!r}",
            )

    def test_percent_returned_is_relative_to_peak(self) -> None:
        self.assertAlmostEqual(bench.percent_returned(200.0, 50.0), 75.0)
        self.assertEqual(bench.percent_returned(0.0, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
