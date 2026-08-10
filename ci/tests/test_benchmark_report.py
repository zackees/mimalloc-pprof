from __future__ import annotations

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark_report as report

FIXTURE = Path(__file__).parent / "fixtures" / "benchmark"


class BenchmarkReportTests(unittest.TestCase):
    def load_latest(self) -> dict[str, object]:
        return json.loads((FIXTURE / "latest.json").read_text(encoding="utf-8"))

    def load_history_row(self) -> dict[str, object]:
        return json.loads((FIXTURE / "history.jsonl").read_text(encoding="utf-8"))

    def write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")

    def render_fixture(self, root: Path) -> tuple[Path, Path]:
        site = root / "site"
        digest = root / "manifest.sha256"
        report.render(
            FIXTURE / "latest.json",
            FIXTURE / "history.jsonl",
            site,
            digest,
            False,
        )
        return site, digest

    def test_fixture_renders_exact_allowlist_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site, digest = self.render_fixture(Path(temporary))
            self.assertEqual(report.SITE_FILES, {path.name for path in site.iterdir()})
            self.assertNotIn(
                "manifest.json",
                {
                    entry["path"]
                    for entry in json.loads((site / "manifest.json").read_text())["files"]
                },
            )
            report.validate_site(site, digest)

    def test_two_fixture_renders_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, first_digest = self.render_fixture(root / "first")
            second, second_digest = self.render_fixture(root / "second")
            for name in report.SITE_FILES:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
            self.assertEqual(first_digest.read_bytes(), second_digest.read_bytes())

    def test_refuses_aggregate_or_unvalidated_input(self) -> None:
        cases: list[dict[str, object]] = []
        aggregate = self.load_latest()
        aggregate["raw_samples"] = []
        cases.append(aggregate)
        unvalidated = self.load_latest()
        validation = unvalidated["validation_report"]
        assert isinstance(validation, dict)
        validation["status"] = "invalid"
        cases.append(unvalidated)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, value in enumerate(cases):
                source = root / f"latest-{index}.json"
                self.write_json(source, value)
                with self.assertRaises(report.ReportError):
                    report.render(
                        source,
                        FIXTURE / "history.jsonl",
                        root / f"site-{index}",
                        root / f"digest-{index}",
                        False,
                    )

    def test_html_escapes_validator_approved_strings(self) -> None:
        latest = self.load_latest()
        run = latest["run"]
        assert isinstance(run, dict)
        run["run_id"] = "</code><script>alert(1)</script>"
        latest["reproduction_command"] = "echo '<img src=x onerror=alert(1)>'"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, root / "missing-history.jsonl", site, root / "digest", True)
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertNotIn("<img src=x onerror=alert(1)>", page)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)

    def test_site_rejects_corruption_unexpected_files_and_broken_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site, digest = self.render_fixture(root)
            chart = site / "benchmark-throughput.png"
            chart.write_bytes(chart.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(report.ReportError, "manifest metadata/digest mismatch"):
                report.validate_site(site, digest)

        with tempfile.TemporaryDirectory() as temporary:
            site, digest = self.render_fixture(Path(temporary))
            (site / "raw-run.json").write_text("{}")
            with self.assertRaisesRegex(report.ReportError, "allowlist mismatch"):
                report.validate_site(site, digest)

        with tempfile.TemporaryDirectory() as temporary:
            site, _digest = self.render_fixture(Path(temporary))
            (site / "index.html").write_text(
                '<html><body><img src="missing.png" alt="missing"></body></html>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(report.ReportError, "broken or unsafe local link"):
                report.validate_html_links(site)

        with tempfile.TemporaryDirectory() as temporary:
            site, digest = self.render_fixture(Path(temporary))
            (site / ".nojekyll").write_bytes(b"unexpected")
            with self.assertRaisesRegex(report.ReportError, "exceeds cap"):
                report.validate_site(site, digest)

        with tempfile.TemporaryDirectory() as temporary:
            site, _digest = self.render_fixture(Path(temporary))
            (site / "index.html").write_text(
                "<html><style>@import url(https://evil.invalid/site.css)</style></html>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(report.ReportError, "remote assets"):
                report.validate_html_links(site)

    def test_history_absence_requires_explicit_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.jsonl"
            with self.assertRaisesRegex(report.ReportError, "initialize-history"):
                report.read_history(path, False)
            self.assertEqual([], report.read_history(path, True))

    def test_history_rejects_malformed_incompatible_and_duplicate_rows(self) -> None:
        row = self.load_history_row()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "malformed.jsonl"
            malformed.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaises(report.ReportError):
                report.read_history(malformed, False)

            incompatible = copy.deepcopy(row)
            incompatible["history_schema_version"] = "benchmark-history-v0"
            incompatible_path = root / "incompatible.jsonl"
            self.write_json(incompatible_path, incompatible)
            with self.assertRaisesRegex(report.ReportError, "incompatible schema"):
                report.read_history(incompatible_path, False)

            duplicate = root / "duplicate.jsonl"
            line = json.dumps(row, separators=(",", ":"))
            duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(report.ReportError, "duplicate run ID/attempt"):
                report.read_history(duplicate, False)

    def test_history_boundaries_sort_cap_and_final_newline(self) -> None:
        template = self.load_history_row()
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)

        def rows(count: int) -> list[dict[str, object]]:
            result: list[dict[str, object]] = []
            for index in range(count):
                row = copy.deepcopy(template)
                run = row["run"]
                assert isinstance(run, dict)
                run["run_id"] = f"history-{index:04d}"
                run["generated_at_utc"] = (start + timedelta(seconds=index)).isoformat()
                result.append(row)
            return result

        current = report.history_row(self.load_latest())
        for count, expected in ((998, 999), (999, 1000), (1000, 1000)):
            merged = report.merge_history(rows(count), copy.deepcopy(current))
            self.assertEqual(expected, len(merged))
            last_run = merged[-1]["run"]
            assert isinstance(last_run, dict)
            self.assertEqual("fixture-current", last_run["run_id"])
        capped = report.merge_history(rows(1000), copy.deepcopy(current))
        first_run = capped[0]["run"]
        assert isinstance(first_run, dict)
        self.assertEqual("history-0001", first_run["run_id"])

        with tempfile.TemporaryDirectory() as temporary:
            site, _digest = self.render_fixture(Path(temporary))
            history = (site / "history.jsonl").read_bytes()
            self.assertTrue(history.endswith(b"\n"))
            self.assertFalse(history.endswith(b"\n\n"))

    def test_history_preserves_comparison_key_lineages(self) -> None:
        latest = self.load_latest()
        latest["comparison_key"] = "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(
                source,
                FIXTURE / "history.jsonl",
                site,
                root / "manifest.sha256",
                False,
            )
            keys = {
                json.loads(line)["comparison_key"]
                for line in (site / "history.jsonl").read_text(encoding="utf-8").splitlines()
            }
            self.assertEqual({"a" * 64, "b" * 64}, keys)


if __name__ == "__main__":
    unittest.main()
