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
                    bench.render_line_chart(
                        series["off"],
                        series["on"],
                        theme,
                        source_line,
                        bench.floor_mb(report["off"], report["on"]),
                    ),
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

    # -- #534: the "live data (theoretical minimum)" floor line -------------------

    def floor_elements(self, svg: str) -> list[ET.Element]:
        root = ET.fromstring(svg)
        return [node for node in root.iter() if node.attrib.get("data-series") == "floor"]

    def test_driver_output_parser_reads_the_floor_line(self) -> None:
        stdout = "\n".join(
            [
                "FLOOR,1536,17425408",
                "CSV,0,280000",
                "CSV,100,80000",
                'STATS_JSON:{"purged_bytes_total":5}',
                "REPORT_BEGIN",
                "hello",
                "REPORT_END",
            ]
        )
        run = bench.parse_driver_output(stdout)
        self.assertEqual(run.baseline_rss_kb, 1536)
        self.assertEqual(run.live_requested_bytes, 17425408)
        self.assertEqual(run.samples, [bench.Sample(0, 280000), bench.Sample(100, 80000)])
        self.assertEqual(run.stats, {"purged_bytes_total": 5})
        self.assertEqual(run.report_text, "hello")

    def test_driver_output_without_a_floor_line_has_no_floor(self) -> None:
        run = bench.parse_driver_output("CSV,0,1024\n")
        self.assertIsNone(run.baseline_rss_kb)
        self.assertIsNone(run.live_requested_bytes)

    def test_driver_reads_the_baseline_before_it_allocates_anything(self) -> None:
        """The baseline is the process before the workload: read at the top of main,
        before the block table exists, and reported on a machine-readable line."""
        src = bench.CHURN_C_SOURCE
        main_body = src[src.index("int main(") :]
        self.assertIn("FLOOR,", src)
        self.assertLess(main_body.index("vm_rss_kb()"), main_body.index("malloc("))
        self.assertIn("live_requested_bytes", main_body)

    def test_floor_is_the_lowest_baseline_plus_live_bytes(self) -> None:
        report, _ = self.load()
        off: bench.RunSummary = dict(report["off"])  # type: ignore[assignment]
        on: bench.RunSummary = dict(report["on"])  # type: ignore[assignment]
        off["baseline_rss_kb"] = 2048
        on["baseline_rss_kb"] = 1024
        off["live_requested_bytes"] = on["live_requested_bytes"] = 16 * 1024 * 1024
        self.assertAlmostEqual(bench.floor_mb(off, on) or 0.0, 17.0)

    def test_old_report_without_floor_fields_renders_no_floor(self) -> None:
        report, series = self.load()
        self.assertIsNone(bench.floor_mb(report["off"], report["on"]))
        svg = bench.render_line_chart(series["off"], series["on"], bench.LIGHT, "src", None)
        self.assertEqual(self.floor_elements(svg), [])
        self.assertNotIn("theoretical minimum", svg)

    def test_floor_line_is_one_dashed_line_inside_the_domain(self) -> None:
        _, series = self.load()
        floor = 17.0
        max_rss = max(s.rss_kb for run in series.values() for s in run) / 1024.0
        top = bench.nice_ticks(max(max_rss, floor))[-1]
        plot_h = bench.HEIGHT - bench.PAD_TOP - bench.PAD_BOTTOM
        expected_y = bench.PAD_TOP + plot_h - (floor / top) * plot_h
        for theme in (bench.LIGHT, bench.DARK):
            with self.subTest(theme=theme.name):
                svg = bench.render_line_chart(series["off"], series["on"], theme, "src", floor)
                floors = self.floor_elements(svg)
                self.assertEqual(len(floors), 1, "exactly one floor line")
                line = floors[0]
                self.assertTrue(line.tag.endswith("line"))
                self.assertIn("stroke-dasharray", line.attrib)
                # neutral: never one of the two series colors
                self.assertNotIn(line.attrib["stroke"], (theme.off_line, theme.on_line))
                y1, y2 = float(line.attrib["y1"]), float(line.attrib["y2"])
                self.assertEqual(y1, y2, "the floor is a horizontal line")
                self.assertAlmostEqual(y1, expected_y, places=1)
                self.assertGreaterEqual(y1, bench.PAD_TOP)
                self.assertLessEqual(y1, bench.PAD_TOP + plot_h)
                self.assertIn("live data (theoretical minimum)", svg)

    def test_a_floor_above_the_series_still_fits_the_domain(self) -> None:
        """Never clipped: the Y domain includes the floor even when it is the max."""
        _, series = self.load()
        svg = bench.render_line_chart(series["off"], series["on"], bench.LIGHT, "src", 400.0)
        (line,) = self.floor_elements(svg)
        self.assertGreaterEqual(float(line.attrib["y1"]), bench.PAD_TOP)

    def test_report_json_round_trips_the_floor_operands(self) -> None:
        import tempfile

        run = bench.RunResult(
            samples=[bench.Sample(0, 300 * 1024), bench.Sample(100, 80 * 1024)],
            stats={},
            report_text="",
            baseline_rss_kb=1500,
            live_requested_bytes=17_000_000,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            bench.write_report_json(path, run, run, "c0ffee00", "cpu", "kernel")
            report = bench.load_report_json(path)
        for key in ("off", "on"):
            self.assertEqual(report[key].get("baseline_rss_kb"), 1500)
            self.assertEqual(report[key].get("live_requested_bytes"), 17_000_000)

    def test_committed_chart_carries_a_floor_below_every_sample(self) -> None:
        """The README chart is measured with the floor: the committed report has its
        operands, and the floor sits at or below every plotted RSS sample."""
        assets = Path(__file__).parent.parent.parent / ".github" / "assets"
        json_path = assets / "hole-purging-report.json"
        if not json_path.exists():
            self.skipTest("no committed hole-purging assets in this checkout")
        report = bench.load_report_json(json_path)
        series = bench.load_csv(assets / "hole-purging-rss.csv")
        floor = bench.floor_mb(report["off"], report["on"])
        self.assertIsNotNone(floor, "committed report has no floor operands")
        assert floor is not None
        for samples in series.values():
            self.assertLessEqual(floor, min(s.rss_kb for s in samples) / 1024.0)
        svg = (assets / "hole-purging-rss-light.svg").read_text(encoding="utf-8")
        self.assertEqual(len(self.floor_elements(svg)), 1)


if __name__ == "__main__":
    unittest.main()
