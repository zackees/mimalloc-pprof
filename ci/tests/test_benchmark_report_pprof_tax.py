from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportIndexIssue=false

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

import benchmark_report as report

FIXTURE = Path(__file__).parent / "fixtures" / "benchmark"

#: One synthetic workload cell is enough to exercise every per-cell shape
#: (`cells`, `cell_comparisons`, `active_telemetry`, `rss_summaries`) without
#: duplicating the whole matrix the production Rust validator emits.
CELL_SCENARIO_ID = "small-log-mixed"
CELL_THREAD_POINT = "1"

#: (comparison_id, numerator, denominator, badge, ratio, ratio_lower, ratio_upper,
#: overhead, overhead_lower, overhead_upper). Mirrors `PPROF_TAX_COMPARISONS`'
#: canonical order/table exactly; sparse-sampling-tax (index 3) is the headline at
#: 2% overhead [1%, 3%], rate-1-stress (index 5) is a deliberately large 80%
#: overhead [75%, 85%] so tests can confirm it never dominates the panel scale or
#: the headline.
PLANTED_COMPARISON_VALUES = (
    ("fork-overlay-tax", "fork-pprof-off", "upstream-baseline", "context", 0.990, 0.985, 0.995),
    (
        "instrumentation-tax",
        "fork-pprof-on-stopped",
        "fork-pprof-off",
        "control",
        0.995,
        0.990,
        1.000,
    ),
    (
        "frame-pointer-tax",
        "fork-pprof-off-frame-pointers",
        "fork-pprof-off",
        "control",
        0.995,
        0.990,
        1.000,
    ),
    (
        "sparse-sampling-tax",
        "fork-pprof-sparse",
        "fork-pprof-on-stopped",
        "production-oriented",
        0.980,
        0.970,
        0.990,
    ),
    (
        "aggressive-sampling-tax",
        "fork-pprof-aggressive",
        "fork-pprof-on-stopped",
        "aggressive",
        0.900,
        0.880,
        0.920,
    ),
    (
        "rate-1-stress",
        "fork-pprof-rate-1-stress",
        "fork-pprof-on-stopped",
        "stress-only",
        0.200,
        0.150,
        0.250,
    ),
)
#: Index of sparse-sampling-tax within PLANTED_COMPARISON_VALUES / PPROF_TAX_COMPARISONS.
HEADLINE_INDEX = 3

CONFIGURATION_ATTRS: dict[str, tuple[str, str, bool, bool, int | None, str]] = {
    # configuration_id: (compiled_configuration_id, role, pprof_compiled, pprof_active,
    #                     sampling_interval_bytes, frame_pointer_policy)
    "upstream-baseline": ("upstream-baseline", "context", False, False, None, "omitted"),
    "fork-pprof-off": ("fork-pprof-off", "context", False, False, None, "omitted"),
    "fork-pprof-on-stopped": (
        "fork-pprof-on",
        "control",
        True,
        False,
        None,
        "cmake-mi-pprof-implicit",
    ),
    "fork-pprof-off-frame-pointers": (
        "fork-pprof-off-frame-pointers",
        "control",
        False,
        False,
        None,
        "forced-flag",
    ),
    "fork-pprof-sparse": (
        "fork-pprof-on",
        "production-oriented",
        True,
        True,
        524288,
        "cmake-mi-pprof-implicit",
    ),
    "fork-pprof-aggressive": (
        "fork-pprof-on",
        "aggressive",
        True,
        True,
        4096,
        "cmake-mi-pprof-implicit",
    ),
    "fork-pprof-rate-1-stress": (
        "fork-pprof-on",
        "stress-only",
        True,
        True,
        1,
        "cmake-mi-pprof-implicit",
    ),
}
ACTIVE_CONFIGURATION_IDS = (
    "fork-pprof-sparse",
    "fork-pprof-aggressive",
    "fork-pprof-rate-1-stress",
)


class BenchmarkReportPprofTaxTests(unittest.TestCase):
    def load_latest(self) -> dict[str, object]:
        return json.loads((FIXTURE / "latest.json").read_text(encoding="utf-8"))

    def write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")

    def upstream_source_sha(self, latest: dict[str, object]) -> str:
        allocators = latest["allocators"]
        assert isinstance(allocators, list)
        for value in allocators:
            assert isinstance(value, dict)
            if value.get("allocator_id") == "upstream-mimalloc":
                return cast(str, value["source_sha"])
        raise AssertionError("fixture has no upstream-mimalloc allocator entry")

    def build_comparison(
        self,
        values: tuple[str, str, str, str, float, float, float],
        *,
        headline_eligible: bool = False,
        cell: tuple[str, str] | None = None,
        valid_block_count: int = 15,
    ) -> dict[str, object]:
        comparison_id, numerator, denominator, badge, ratio, ratio_lower, ratio_upper = values
        overhead = round(1.0 - ratio, 6)
        overhead_lower = round(1.0 - ratio_upper, 6)
        overhead_upper = round(1.0 - ratio_lower, 6)
        return {
            "comparison_id": comparison_id,
            "label": comparison_id.replace("-", " "),
            "numerator_configuration_id": numerator,
            "denominator_configuration_id": denominator,
            "scenario_id": cell[0] if cell else None,
            "thread_point": cell[1] if cell else None,
            "metric_id": report.PPROF_TAX_COMPARISON_METRIC,
            "badge": badge,
            "support_status": "supported",
            "valid_block_count": valid_block_count,
            "ratio": ratio,
            "ratio_lower": ratio_lower,
            "ratio_upper": ratio_upper,
            "overhead": overhead,
            "overhead_lower": overhead_lower,
            "overhead_upper": overhead_upper,
            "headline_eligible": headline_eligible,
            "reason": None,
        }

    def build_pprof_tax_section(self, latest: dict[str, object]) -> dict[str, object]:
        run = latest["run"]
        runner = latest["runner"]
        assert isinstance(run, dict) and isinstance(runner, dict)
        cell = (CELL_SCENARIO_ID, CELL_THREAD_POINT)

        configurations: list[dict[str, object]] = []
        for index, configuration_id in enumerate(report.PPROF_TAX_CONFIGURATION_IDS):
            attrs = CONFIGURATION_ATTRS[configuration_id]
            configurations.append(
                {
                    "configuration_id": configuration_id,
                    "compiled_configuration_id": attrs[0],
                    "role": attrs[1],
                    "pprof_compiled": attrs[2],
                    "pprof_active": attrs[3],
                    "sampling_interval_bytes": attrs[4],
                    "frame_pointer_policy": attrs[5],
                    "support_status": "supported",
                    "unsupported_reason": None,
                    "executable_sha256": str(index + 1) * 64,
                    "valid_samples": 15,
                    "invalid_samples": 0,
                }
            )

        comparisons = [
            self.build_comparison(values, headline_eligible=(index == HEADLINE_INDEX))
            for index, values in enumerate(PLANTED_COMPARISON_VALUES)
        ]
        cell_comparisons = [
            self.build_comparison(values, cell=cell) for values in PLANTED_COMPARISON_VALUES
        ]

        active_telemetry = [
            {
                "configuration_id": configuration_id,
                "scenario_id": cell[0],
                "thread_point": cell[1],
                "valid_runs": 15,
                "invalid_runs": 0,
                "median_sample_count": 800,
                "median_sampled_bytes": 819200,
                "max_dropped_records": 0,
                "max_profiler_arena_bytes": 65536,
                "median_profile_size_bytes": 4096,
                "validity_status": "valid",
                "invalid_reasons": [],
            }
            for configuration_id in ACTIVE_CONFIGURATION_IDS
        ]
        rss_summaries = [
            {
                "configuration_id": configuration_id,
                "scenario_id": cell[0],
                "thread_point": cell[1],
                "median_peak_rss_bytes": 100 * 1024 * 1024 + index,
                "median_end_rss_delta_bytes": -1024 * index,
            }
            for index, configuration_id in enumerate(report.PPROF_TAX_CONFIGURATION_IDS)
        ]

        return {
            "metric_schema_version": report.PPROF_TAX_SCHEMA,
            "status": "valid",
            "mode": "full",
            "metric_comparison_key": "d" * 64,
            "run": copy.deepcopy(run),
            "runner": copy.deepcopy(runner),
            "run_seed": 1,
            "blocks": report.PPROF_TAX_MIN_BLOCKS,
            "minimum_paired_blocks": report.PPROF_TAX_MIN_BLOCKS,
            "hosted_runner_scope": "single fixed GitHub-hosted 4-vCPU ubuntu-24.04 runner",
            "configuration_manifest_sha256": "a" * 64,
            "raw_artifact_sha256": "b" * 64,
            "raw_artifact_name": "pprof-tax-output-12345.tar.zst",
            "fork_source_sha": "c" * 40,
            "upstream_source_sha": self.upstream_source_sha(latest),
            "intervals": {
                "sparse_bytes": 524288,
                "sparse_rationale": "matches the recommended production sampling interval",
                "aggressive_bytes": 4096,
                "stress_bytes": 1,
            },
            "configurations": configurations,
            "cells": [
                {
                    "scenario_id": cell[0],
                    "thread_point": cell[1],
                    "thread_count": 1,
                    "operations_per_worker": 4096,
                }
            ],
            "comparisons": comparisons,
            "cell_comparisons": cell_comparisons,
            "headline": copy.deepcopy(comparisons[HEADLINE_INDEX]),
            "active_telemetry": active_telemetry,
            "rss_summaries": rss_summaries,
            "latency": {
                "status": "not-collected",
                "reason": "pprof-tax measures throughput only; see the dedicated latency section",
            },
        }

    def with_complete_pprof_tax(self, latest: dict[str, object]) -> dict[str, object]:
        value = copy.deepcopy(latest)
        value["pprof_tax"] = self.build_pprof_tax_section(value)
        pending = value["pending_metrics"]
        assert isinstance(pending, list)
        value["pending_metrics"] = [
            item
            for item in pending
            if isinstance(item, dict) and item.get("metric_id") != "pprof-tax"
        ]
        return value

    # ---------------------------------------------------------------- validity

    def test_valid_planted_section_passes(self) -> None:
        latest = self.with_complete_pprof_tax(self.load_latest())
        report.validate_latest(latest, "latest")
        section = latest["pprof_tax"]
        assert isinstance(section, dict)
        report.validate_pprof_tax_report(section, "pprof_tax")

    def test_constants_match_the_declared_contract(self) -> None:
        self.assertEqual("pprof-tax-v1", report.PPROF_TAX_SCHEMA)
        self.assertEqual(15, report.PPROF_TAX_MIN_BLOCKS)
        self.assertEqual(40, len(report.PPROF_TAX_UPSTREAM_COMMIT))
        self.assertEqual(7, len(report.PPROF_TAX_CONFIGURATION_IDS))
        self.assertEqual(6, len(report.PPROF_TAX_COMPARISONS))
        self.assertEqual("stress-only", report.PPROF_TAX_COMPARISONS[5][3])
        for text in (
            "production-oriented",
            "aggressive",
            "stress-only",
            "unsupported",
            "invalid",
        ):
            self.assertEqual(text, report.PPROF_TAX_BADGE_TEXT[text])
        self.assertEqual(
            "insufficient paired blocks", report.PPROF_TAX_BADGE_TEXT["insufficient-paired-blocks"]
        )

    # ----------------------------------------------------- impossible states

    def test_impossible_states_and_enumerations_are_rejected(self) -> None:
        base = self.build_pprof_tax_section(self.load_latest())

        def mutate_active_without_compiled(section: dict[str, object]) -> None:
            configurations = cast(list[dict[str, object]], section["configurations"])
            for entry in configurations:
                if entry["configuration_id"] == "fork-pprof-sparse":
                    entry["pprof_compiled"] = False

        def mutate_upstream_baseline_interval(section: dict[str, object]) -> None:
            configurations = cast(list[dict[str, object]], section["configurations"])
            configurations[0]["sampling_interval_bytes"] = 524288

        def mutate_configuration_order(section: dict[str, object]) -> None:
            configurations = cast(list[dict[str, object]], section["configurations"])
            configurations[0], configurations[1] = configurations[1], configurations[0]

        def mutate_comparison_table(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[0]["numerator_configuration_id"] = "fork-pprof-on-stopped"

        def mutate_missing_frame_pointer_comparison(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            del comparisons[2]

        def mutate_frame_pointer_tax_insufficient(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[2] = {
                **comparisons[2],
                "support_status": "insufficient-paired-blocks",
                "valid_block_count": 3,
                "ratio": None,
                "ratio_lower": None,
                "ratio_upper": None,
                "overhead": None,
                "overhead_lower": None,
                "overhead_upper": None,
                "reason": "fewer than 15 paired blocks completed",
            }

        def mutate_rate1_headline_eligible(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[5]["headline_eligible"] = True

        def mutate_rate1_badge(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[5]["badge"] = "production-oriented"

        def mutate_headline_disagrees(section: dict[str, object]) -> None:
            headline = cast(dict[str, object], section["headline"])
            section["headline"] = {**headline, "overhead": 0.5}

        def mutate_headline_too_few_blocks(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[HEADLINE_INDEX]["valid_block_count"] = 3
            section["headline"] = copy.deepcopy(comparisons[HEADLINE_INDEX])

        def mutate_overhead_sign_inconsistent(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[HEADLINE_INDEX]["overhead"] = -0.02
            section["headline"] = copy.deepcopy(comparisons[HEADLINE_INDEX])

        def mutate_ratio_outside_interval(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[4]["ratio_lower"] = 0.95

        def mutate_non_supported_keeps_numbers(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[2] = {
                **comparisons[2],
                "support_status": "unsupported",
                "reason": "disabled on this toolchain",
            }

        def mutate_supported_carries_reason(section: dict[str, object]) -> None:
            comparisons = cast(list[dict[str, object]], section["comparisons"])
            comparisons[1]["reason"] = "unexpected reason on a supported comparison"

        def mutate_bad_digest(section: dict[str, object]) -> None:
            section["configuration_manifest_sha256"] = "not-hex"

        def mutate_fixed_interval(section: dict[str, object]) -> None:
            intervals = cast(dict[str, object], section["intervals"])
            section["intervals"] = {**intervals, "sparse_bytes": 1}

        def mutate_cell_comparisons_incomplete(section: dict[str, object]) -> None:
            cell_comparisons = cast(list[dict[str, object]], section["cell_comparisons"])
            del cell_comparisons[0]

        def mutate_cell_headline_eligible(section: dict[str, object]) -> None:
            cell_comparisons = cast(list[dict[str, object]], section["cell_comparisons"])
            cell_comparisons[HEADLINE_INDEX]["headline_eligible"] = True

        def mutate_active_telemetry_wrong_configuration(section: dict[str, object]) -> None:
            telemetry = cast(list[dict[str, object]], section["active_telemetry"])
            telemetry[0]["configuration_id"] = "fork-pprof-off"

        mutations = {
            "pprof_active without pprof_compiled": mutate_active_without_compiled,
            "upstream-baseline carries a sampling interval": mutate_upstream_baseline_interval,
            "configuration order is not canonical": mutate_configuration_order,
            "comparison table entry disagrees with the fixed table": mutate_comparison_table,
            "frame-pointer-tax comparison missing": mutate_missing_frame_pointer_comparison,
            "frame-pointer-tax resolves insufficient-paired-blocks": (
                mutate_frame_pointer_tax_insufficient
            ),
            "rate-1-stress is headline eligible": mutate_rate1_headline_eligible,
            "rate-1-stress badge is not stress-only": mutate_rate1_badge,
            "headline disagrees with comparisons[3]": mutate_headline_disagrees,
            "headline comparison has too few blocks": mutate_headline_too_few_blocks,
            "overhead sign inconsistent with ratio": mutate_overhead_sign_inconsistent,
            "ratio outside its own confidence interval": mutate_ratio_outside_interval,
            "non-supported comparison keeps numbers": mutate_non_supported_keeps_numbers,
            "supported comparison carries a reason": mutate_supported_carries_reason,
            "digest is not lowercase hex": mutate_bad_digest,
            "fixed sampling interval literal changed": mutate_fixed_interval,
            "cell_comparisons missing an entry": mutate_cell_comparisons_incomplete,
            "a per-cell comparison is headline eligible": mutate_cell_headline_eligible,
            "active telemetry names an inactive configuration": (
                mutate_active_telemetry_wrong_configuration
            ),
        }
        for label, mutate in mutations.items():
            section = copy.deepcopy(base)
            mutate(section)
            with self.assertRaises(report.ReportError, msg=label):
                report.validate_pprof_tax_report(section, "pprof_tax")

    def test_upstream_provenance_mismatch_fails(self) -> None:
        latest = self.with_complete_pprof_tax(self.load_latest())
        section = latest["pprof_tax"]
        assert isinstance(section, dict)
        section["upstream_source_sha"] = "9" * 40
        with self.assertRaisesRegex(report.ReportError, "upstream_source_sha"):
            report.validate_latest(latest, "latest")

    # -------------------------------------------------------- rendering/scale

    def test_pprof_tax_scale_ignores_rate1_and_floors(self) -> None:
        comparisons = [
            self.build_comparison(values, headline_eligible=(index == HEADLINE_INDEX))
            for index, values in enumerate(PLANTED_COMPARISON_VALUES)
        ]
        # The non-stress supported comparisons cap out at aggressive-sampling-tax's
        # 10% overhead; rate-1-stress's 80% must never move the scale.
        self.assertAlmostEqual(0.10, report.pprof_tax_scale(comparisons), places=6)

        # Every non-stress comparison is a near-zero 0.1% overhead here; only
        # rate-1-stress carries a real (0.2 ratio / 80% overhead) value. The scale
        # must still floor at PPROF_TAX_MIN_SCALE, not follow rate-1-stress down.
        tiny_values = [list(values) for values in PLANTED_COMPARISON_VALUES]
        for values in tiny_values:
            if values[0] != "rate-1-stress":
                values[4], values[5], values[6] = 0.999, 0.998, 1.0
        tiny = [
            self.build_comparison(cast("tuple[str, str, str, str, float, float, float]", values))
            for values in tiny_values
        ]
        self.assertEqual(report.PPROF_TAX_MIN_SCALE, report.pprof_tax_scale(tiny))

    def test_pprof_tax_png_is_960x540(self) -> None:
        section = self.build_pprof_tax_section(self.load_latest())
        png_bytes = report.pprof_tax_png(section)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "benchmark-pprof-tax.png"
            path.write_bytes(png_bytes)
            self.assertEqual((960, 540), report.png_dimensions(path))

    def test_manifest_role_is_no_longer_pending(self) -> None:
        self.assertEqual("pprof-tax-panel", report.ROLES["benchmark-pprof-tax.png"])

    # -------------------------------------------------------------- rendering

    def test_render_with_planted_section_html_and_png(self) -> None:
        latest = self.with_complete_pprof_tax(self.load_latest())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('<h2 id="pprof-tax">pprof compilation and runtime tax</h2>', page)
            for badge_text in (
                "context",
                "control",
                "production-oriented",
                "aggressive",
                "stress-only",
            ):
                self.assertIn(badge_text, page)
            self.assertIn("single fixed GitHub-hosted 4-vCPU ubuntu-24.04 runner", page)
            self.assertIn("a" * 64, page)  # configuration manifest digest
            self.assertIn("b" * 64, page)  # raw artifact digest
            self.assertIn("<details>", page)
            self.assertIn("stress-only", page)
            self.assertIn("excluded from the headline", page)
            png_path = site / "benchmark-pprof-tax.png"
            self.assertEqual((960, 540), report.png_dimensions(png_path))
            pending_bytes = report.pending_png("pprof-tax", "x")
            self.assertNotEqual(pending_bytes, png_path.read_bytes())

    def test_old_latest_without_pprof_tax_still_renders_pending_card(self) -> None:
        latest = self.load_latest()
        self.assertNotIn("pprof_tax", latest)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertNotIn('id="pprof-tax"', page)
            self.assertIn("pprof-tax: pending", page)
            self.assertNotIn("0.00%", page)
            png_path = site / "benchmark-pprof-tax.png"
            pending_metrics = latest["pending_metrics"]
            assert isinstance(pending_metrics, list)
            matches = [
                item
                for item in pending_metrics
                if isinstance(item, dict) and item.get("metric_id") == "pprof-tax"
            ]
            expected = report.pending_png("pprof-tax", cast(str, matches[0]["reason"]))
            self.assertEqual(expected, png_path.read_bytes())

    def test_old_history_row_without_pprof_tax_still_validates(self) -> None:
        line = (FIXTURE / "history.jsonl").read_text(encoding="utf-8").splitlines()[0]
        row = json.loads(line)
        self.assertNotIn("pprof_tax", row)
        report.validate_history_row(row, "history row")

    # ------------------------------------------------------------ history row

    def test_history_row_pprof_tax_projection_has_expected_keys(self) -> None:
        latest = self.with_complete_pprof_tax(self.load_latest())
        row = report.history_row(latest)
        self.assertIn("pprof_tax", row)
        projected = row["pprof_tax"]
        assert isinstance(projected, dict)
        # Hard-coded independently of PPROF_TAX_COMPACT_DROPPED_FIELDS: the full section
        # minus {runner, configurations, cells, cell_comparisons, active_telemetry,
        # rss_summaries} plus runner_fingerprint_sha256, exactly as the task's data
        # contract specifies.
        expected_keys = {
            "metric_schema_version",
            "status",
            "mode",
            "metric_comparison_key",
            "run",
            "run_seed",
            "blocks",
            "minimum_paired_blocks",
            "hosted_runner_scope",
            "configuration_manifest_sha256",
            "raw_artifact_sha256",
            "raw_artifact_name",
            "fork_source_sha",
            "upstream_source_sha",
            "intervals",
            "comparisons",
            "headline",
            "latency",
            "runner_fingerprint_sha256",
        }
        self.assertEqual(expected_keys, set(projected))
        report.validate_history_row(row, "history row")

    # --------------------------------------------------------- carry-forward

    def test_carry_forward_copies_pprof_tax_and_clears_pending(self) -> None:
        prior = self.with_complete_pprof_tax(self.load_latest())
        fresh = copy.deepcopy(prior)
        fresh.pop("pprof_tax", None)
        fresh["pending_metrics"] = [
            {
                "metric_id": metric,
                "status": "pending",
                "reason": "x",
                "phase_issue_url": "https://github.com/zackees/mimalloc-pprof/issues/187",
            }
            for metric in ("memory", "latency", "scaling", "pprof-tax")
        ]
        self.assertTrue(report.carry_forward_optional_metrics(fresh, prior))
        self.assertIn("pprof_tax", fresh)
        pending = fresh["pending_metrics"]
        assert isinstance(pending, list)
        self.assertNotIn("pprof-tax", [item["metric_id"] for item in pending])
        report.validate_latest(fresh, "carried pprof_tax")

    def test_carry_forward_skips_pprof_tax_when_upstream_pin_differs(self) -> None:
        prior = self.with_complete_pprof_tax(self.load_latest())
        fresh = copy.deepcopy(prior)
        fresh.pop("pprof_tax", None)
        allocators = fresh["allocators"]
        assert isinstance(allocators, list)
        for value in allocators:
            assert isinstance(value, dict)
            if value["allocator_id"] == "upstream-mimalloc":
                value["source_sha"] = "8" * 40
        fresh["pending_metrics"] = [
            {
                "metric_id": metric,
                "status": "pending",
                "reason": "not measured at this pin",
                "phase_issue_url": "https://github.com/zackees/mimalloc-pprof/issues/187",
            }
            for metric in ("memory", "latency", "scaling", "pprof-tax")
        ]
        self.assertFalse(report.carry_forward_optional_metrics(fresh, prior))
        self.assertNotIn("pprof_tax", fresh)
        pending = fresh["pending_metrics"]
        assert isinstance(pending, list)
        self.assertIn("pprof-tax", [item["metric_id"] for item in pending])


if __name__ == "__main__":
    unittest.main()
