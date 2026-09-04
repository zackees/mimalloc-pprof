from __future__ import annotations

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import unittest
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import bench_hole_purging as bench

FIXTURE = Path(__file__).parent / "fixtures" / "hole_purging"


class BenchHolePurgingTests(unittest.TestCase):
    def load(self) -> tuple[bench.ReportJson, dict[str, list[bench.Sample]]]:
        report = bench.load_report_json(FIXTURE / "hole-purging-report.json")
        series = bench.load_csv(FIXTURE / "hole-purging-rss.csv")
        return report, series

    def test_csv_round_trips_samples(self) -> None:
        _, series = self.load()
        self.assertEqual(len(series["off"]), 4)
        self.assertEqual(len(series["on"]), 4)
        self.assertEqual(series["off"][0].t_ms, 0)
        self.assertAlmostEqual(series["off"][0].rss_kb / 1024.0, 280.3, places=2)

    def test_line_chart_svg_is_well_formed_and_labeled(self) -> None:
        report, series = self.load()
        source_line = bench.format_source_line(report["commit"], report["cpu"], report["kernel"])
        for theme in (bench.LIGHT, bench.DARK):
            svg = bench.render_line_chart(series["off"], series["on"], theme, source_line)
            root = ET.fromstring(svg)  # raises if malformed
            self.assertTrue(root.tag.endswith("svg"))
            self.assertIn("viewBox", root.attrib)
            self.assertIn("hole purging off", svg)
            self.assertIn("hole purging on", svg)
            self.assertIn("fixture01", svg)
            # under the 60KB budget the README brief sets for each SVG
            self.assertLess(len(svg.encode("utf-8")), 60_000)

    def test_table_svg_is_well_formed_with_expected_rows(self) -> None:
        report, _ = self.load()
        source_line = bench.format_source_line(report["commit"], report["cpu"], report["kernel"])
        off_stats, on_stats = report["off"]["stats"], report["on"]["stats"]
        off_summary, on_summary = report["off"], report["on"]
        stats_off = cast(Mapping[str, int], off_stats)
        stats_on = cast(Mapping[str, int], on_stats)
        for theme in (bench.LIGHT, bench.DARK):
            svg = bench.render_table_svg(
                stats_off, stats_on, off_summary, on_summary, theme, source_line
            )
            root = ET.fromstring(svg)
            self.assertTrue(root.tag.endswith("svg"))
            self.assertIn("viewBox", root.attrib)
            for _, label in bench.TABLE_ROWS:
                self.assertIn(label, svg)
            for label, _, _ in bench.memory_summary_rows(off_summary, on_summary):
                self.assertIn(label, svg)
            # a real measured delta must show up, not just zeros
            self.assertIn("165,052,416", svg)
            self.assertLess(len(svg.encode("utf-8")), 60_000)

    def test_table_omits_rows_missing_from_stats(self) -> None:
        report, _ = self.load()
        off_stats = cast("dict[str, int]", dict(report["off"]["stats"]))
        on_stats = cast("dict[str, int]", dict(report["on"]["stats"]))
        del off_stats["full_sweeps"]
        del on_stats["full_sweeps"]
        svg = bench.render_table_svg(
            off_stats, on_stats, report["off"], report["on"], bench.LIGHT, "src"
        )
        self.assertNotIn("sweeps that walked every page", svg)

    def test_from_data_matches_committed_svgs(self) -> None:
        """The real assets under .github/assets/ must reproduce byte-for-byte from
        the committed CSV/JSON via --from-data -- otherwise the SVG and the caption
        it carries (commit/cpu/kernel) can silently drift apart."""
        assets = Path(__file__).parent.parent.parent / ".github" / "assets"
        csv_path = assets / "hole-purging-rss.csv"
        json_path = assets / "hole-purging-report.json"
        if not csv_path.exists() or not json_path.exists():
            self.skipTest("no committed hole-purging assets in this checkout")
        report = bench.load_report_json(json_path)
        series = bench.load_csv(csv_path)
        source_line = bench.format_source_line(report["commit"], report["cpu"], report["kernel"])
        off_stats = cast(Mapping[str, int], report["off"]["stats"])
        on_stats = cast(Mapping[str, int], report["on"]["stats"])
        for theme in (bench.LIGHT, bench.DARK):
            with self.subTest(theme=theme.name):
                self.assertEqual(
                    (assets / f"hole-purging-rss-{theme.name}.svg").read_text(encoding="utf-8"),
                    bench.render_line_chart(series["off"], series["on"], theme, source_line),
                )
                self.assertEqual(
                    (assets / f"hole-purging-table-{theme.name}.svg").read_text(encoding="utf-8"),
                    bench.render_table_svg(
                        off_stats, on_stats, report["off"], report["on"], theme, source_line
                    ),
                )

    def test_median_run_picks_middle_by_tail_rss(self) -> None:
        low = bench.RunResult(
            samples=[bench.Sample(9000, 700 * 1024), bench.Sample(10000, 700 * 1024)],
            stats={},
            report_text="",
        )
        mid = bench.RunResult(
            samples=[bench.Sample(9000, 800 * 1024), bench.Sample(10000, 800 * 1024)],
            stats={},
            report_text="",
        )
        high = bench.RunResult(
            samples=[bench.Sample(9000, 900 * 1024), bench.Sample(10000, 900 * 1024)],
            stats={},
            report_text="",
        )
        picked = bench.median_run([high, low, mid])
        self.assertIs(picked, mid)


if __name__ == "__main__":
    unittest.main()
