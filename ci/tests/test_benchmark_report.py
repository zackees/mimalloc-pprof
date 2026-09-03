from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportIndexIssue=false

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import copy
import json
import math
import re
import subprocess
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import benchmark_report as report

FIXTURE = Path(__file__).parent / "fixtures" / "benchmark"


class BenchmarkReportTests(unittest.TestCase):
    def git(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )

    def load_latest(self) -> dict[str, object]:
        return json.loads((FIXTURE / "latest.json").read_text(encoding="utf-8"))

    def load_history_row(self) -> dict[str, object]:
        return json.loads((FIXTURE / "history.jsonl").read_text(encoding="utf-8"))

    def write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")

    def with_complete_memory(self, latest: dict[str, object]) -> dict[str, object]:
        value = copy.deepcopy(latest)
        raw_samples = value["raw_samples"]
        assert isinstance(raw_samples, list)
        memory_samples: list[dict[str, object]] = []
        templates = [
            item
            for item in raw_samples
            if isinstance(item, dict)
            and item.get("scenario_id") == "scenario-00"
            and item.get("thread_point") == "1"
        ]
        self.assertEqual(15 * len(report.ALLOCATOR_IDS), len(templates))
        for scenario, point in report.MEMORY_CELLS:
            for template in templates:
                child_value = copy.deepcopy(template)
                child_value["scenario_id"] = scenario
                child_value["thread_point"] = point
                child_value["thread_count"] = (
                    1 if point == "1" else 2 if point == "2" else value["runner"]["physical_cores"]
                )
                assert isinstance(child_value, dict)
                allocator = str(child_value["allocator_id"])
                delta_mib = {
                    "tcmalloc": 120,
                    "jemalloc": 110,
                    "upstream-mimalloc": 100,
                    "bun-mimalloc": 95,
                    "mimalloc-pprof": 90,
                }[allocator] + int(child_value["block_id"])
                baseline = 100 * 1024 * 1024
                delta = delta_mib * 1024 * 1024
                peak = baseline + delta
                post = baseline + delta // 2
                memory_samples.append(
                    {
                        "metric_schema_version": report.MEMORY_SCHEMA,
                        "block_id": child_value["block_id"],
                        "ordinal": child_value["ordinal"],
                        "workload_seed": child_value["workload_seed"],
                        "allocator_id": allocator,
                        "allocator_source_sha": child_value["allocator_source_sha"],
                        "child_binary_sha256": child_value["child_binary_sha256"],
                        "scenario_id": child_value["scenario_id"],
                        "thread_point": child_value["thread_point"],
                        "thread_count": child_value["thread_count"],
                        "baseline_ready_ns": 1_000_000,
                        "workload_active_ns": 9_000_000,
                        "workload_drained_ns": 21_000_000,
                        "post_drain_sample_100ms_ns": 121_000_000,
                        "post_drain_sample_1s_ns": 1_021_000_000,
                        "post_drain_sample_5s_ns": 5_021_000_000,
                        "sampler_pid": 100,
                        "sampled_pid": 101 + int(child_value["ordinal"]),
                        "baseline_rss_bytes": baseline,
                        "baseline_hwm_bytes": baseline,
                        "sampled_peak_rss_bytes": peak,
                        "kernel_peak_hwm_bytes": peak,
                        "peak_live_requested_bytes": child_value["peak_live_requested_bytes"],
                        "post_drain_rss_100ms_bytes": post,
                        "post_drain_rss_1s_bytes": post,
                        "post_drain_rss_5s_bytes": post,
                        "sampled_peak_rss_delta_bytes": delta,
                        "post_drain_rss_delta_100ms_bytes": delta // 2,
                        "post_drain_rss_delta_1s_bytes": delta // 2,
                        "post_drain_rss_delta_5s_bytes": delta // 2,
                        "fragmentation_proxy": delta
                        / int(child_value["peak_live_requested_bytes"]),
                        "hwm_discrepancy": False,
                        "hwm_tolerance_bytes": max(8 * 1024 * 1024, delta // 5),
                        "sampling": {
                            "target_interval_ns": 5_000_000,
                            "sample_count": 3,
                            "minimum_interval_ns": 5_000_000,
                            "median_interval_ns": 5_000_000,
                            "p95_interval_ns": 5_000_000,
                            "maximum_interval_ns": 5_000_000,
                        },
                        "timeline": [
                            {"elapsed_ns": 10_000_000, "rss_bytes": baseline + delta // 2},
                            {"elapsed_ns": 15_000_000, "rss_bytes": peak},
                            {"elapsed_ns": 20_000_000, "rss_bytes": baseline + delta * 3 // 4},
                        ],
                        "environment": {
                            "page_size_bytes": 4096,
                            "kernel": value["runner"]["kernel"],
                            "transparent_hugepage": "always [madvise] never",
                            "cgroup_memory_max": "2147483648",
                            "cgroup_memory_high": "max",
                            "hosted_runner": value["runner"]["runner_class"] == "github-hosted",
                            "purge_policy": "natural-only",
                            "allocator_runtime_options": {
                                "MIMALLOC_MEMORY_EVENTS": "0",
                                "MIMALLOC_PROF": "0",
                            },
                        },
                        "child_sample": child_value,
                    }
                )

        absolute: list[dict[str, object]] = []
        paired: list[dict[str, object]] = []
        stats = {
            "count": 15,
            "median": float(128 * 1024 * 1024),
            "min": float(120 * 1024 * 1024),
            "max": float(136 * 1024 * 1024),
            "q1": float(124 * 1024 * 1024),
            "q3": float(132 * 1024 * 1024),
            "iqr": float(8 * 1024 * 1024),
            "relative_iqr": 0.0625,
            "noisy": False,
        }
        for scenario, point in report.MEMORY_CELLS:
            for metric in report.MEMORY_METRICS:
                metric_stats = copy.deepcopy(stats)
                if metric == "fragmentation-proxy":
                    metric_stats.update(median=1.1, min=1.0, max=1.2, q1=1.05, q3=1.15, iqr=0.1)
                for allocator in report.ALLOCATOR_IDS:
                    absolute.append(
                        {
                            "scenario_id": scenario,
                            "thread_point": point,
                            "metric_id": metric,
                            "direction": "lower-is-better",
                            "allocator_id": allocator,
                            "summary": copy.deepcopy(metric_stats),
                        }
                    )
                    if allocator != "upstream-mimalloc":
                        paired.append(
                            {
                                "scenario_id": scenario,
                                "thread_point": point,
                                "metric_id": metric,
                                "summary": {
                                    "candidate_id": allocator,
                                    "reference_id": "upstream-mimalloc",
                                    "direction": "lower-is-better",
                                    "block_count": 15,
                                    "effect": 1.05,
                                    "confidence_interval": {
                                        "lower": 1.01,
                                        "upper": 1.09,
                                        "confidence_level": 0.95,
                                    },
                                    "bootstrap": {
                                        "seed": 1,
                                        "resample_count": 10_000,
                                        "method": "percentile-block-bootstrap-type7-v1",
                                        "prng": "splitmix64-rejection-v1",
                                    },
                                    "informational": True,
                                },
                            }
                        )
        value["memory"] = {
            "metric_schema_version": report.MEMORY_SCHEMA,
            "status": "complete",
            "invalid_reason": None,
            "metric_comparison_key": "a" * 64,
            "run": copy.deepcopy(value["run"]),
            "runner": copy.deepcopy(value["runner"]),
            "sampling_target_interval_ns": 5_000_000,
            "purge_policy": "natural-only",
            "units": {
                metric: "ratio" if metric == "fragmentation-proxy" else "bytes"
                for metric in report.MEMORY_METRICS
            },
            "direction": "lower-is-better",
            "informational": True,
            "methodology": copy.deepcopy(report.MEMORY_METHODOLOGY),
            "absolute_summaries": absolute,
            "paired_summaries": paired,
            "raw_samples": memory_samples,
        }
        pending = value["pending_metrics"]
        assert isinstance(pending, list)
        value["pending_metrics"] = [
            item for item in pending if isinstance(item, dict) and item.get("metric_id") != "memory"
        ]
        return value

    def with_complete_latency(self, latest: dict[str, object]) -> dict[str, object]:
        value = copy.deepcopy(latest)
        runner = value["runner"]
        allocators = value["allocators"]
        assert isinstance(runner, dict) and isinstance(allocators, list)
        allocator_sources = {
            str(item["allocator_id"]): (str(item["source_sha"]), str(item["child_binary_sha256"]))
            for item in allocators
            if isinstance(item, dict)
        }
        rates = {f"{scenario}/{point}": 1 for scenario, point in report.LATENCY_CELLS}
        raw: list[dict[str, object]] = []
        block_summaries: list[dict[str, object]] = []
        absolute: list[dict[str, object]] = []
        paired: list[dict[str, object]] = []

        def distribution(values: list[int]) -> dict[str, object]:
            return report.summarize_latency_values(values)

        definitions = {
            "tiny-fixed-64": "allocation plus required touch/checksum plus free",
            "small-log-mixed": "allocation plus required touch/checksum plus free",
            "cross-thread-producer-consumer": "producer allocation through consumer free completion, including queue and ownership transfer",
            "large-objects": "allocation plus one-byte-per-page touch/checksum plus free",
        }
        for scenario, point in report.LATENCY_CELLS:
            thread_count = 1 if point == "1" else int(runner["physical_cores"])
            for allocator_index, allocator in enumerate(report.ALLOCATOR_IDS):
                base = 1_000 + allocator_index * 100
                if allocator != "upstream-mimalloc":
                    for quantile in ("p50", "p95", "p99"):
                        paired.append(
                            {
                                "scenario_id": scenario,
                                "thread_point": point,
                                "quantile": quantile,
                                "summary": {
                                    "candidate_id": allocator,
                                    "reference_id": "upstream-mimalloc",
                                    "direction": "lower-is-better",
                                    "block_count": 15,
                                    "effect": 1.05,
                                    "confidence_interval": {
                                        "lower": 1.01,
                                        "upper": 1.09,
                                        "confidence_level": 0.95,
                                    },
                                    "bootstrap": {
                                        "seed": 1,
                                        "resample_count": 10_000,
                                        "method": "percentile-whole-block-transaction-quantile-type7-v1",
                                        "prng": "splitmix64-rejection-v1",
                                    },
                                    "informational": True,
                                },
                            }
                        )
                all_measured: list[int] = []
                all_control: list[int] = []
                for block in range(15):
                    observations = [
                        {
                            "thread_index": index % thread_count,
                            "transaction_index": index // thread_count,
                            "duration_ns": base + index % 41,
                        }
                        for index in range(667)
                    ]
                    observations.sort(
                        key=lambda item: (item["thread_index"], item["transaction_index"])
                    )
                    control_observations = [
                        item | {"duration_ns": 40 + int(item["transaction_index"]) % 3}
                        for item in observations
                    ]
                    measured_values = [int(item["duration_ns"]) for item in observations]
                    control_values = [int(item["duration_ns"]) for item in control_observations]
                    all_measured.extend(measured_values)
                    all_control.extend(control_values)
                    scheduling = {
                        "affinity_policy": runner["affinity"]["policy"],
                        "actual_cpu_ids": list(range(thread_count)),
                        "thread_count": thread_count,
                        "physical_cores": runner["physical_cores"],
                        "logical_cores": runner["logical_cores"],
                        "context_switches": {"voluntary": 0, "involuntary": 0},
                        "runner_class": runner["runner_class"],
                        "clock": {
                            "source": "monotonic",
                            "implementation": "std::time::Instant/CLOCK_MONOTONIC",
                            "resolution_ns": 1,
                        },
                    }
                    child_base = {
                        "protocol_version": report.LATENCY_CHILD_PROTOCOL,
                        "metric_schema_version": report.LATENCY_SCHEMA,
                        "completed_transactions": thread_count * 1_000,
                        "checksum": 2,
                        "scheduling": copy.deepcopy(scheduling),
                    }
                    source_sha, child_sha = allocator_sources[allocator]
                    raw.append(
                        {
                            "metric_schema_version": report.LATENCY_SCHEMA,
                            "block_id": block,
                            "ordinal": allocator_index,
                            "workload_seed": block + 1,
                            "allocator_id": allocator,
                            "allocator_source_sha": source_sha,
                            "child_binary_sha256": child_sha,
                            "scenario_id": scenario,
                            "thread_point": point,
                            "thread_count": thread_count,
                            "sample_denominator": 1,
                            "transaction_definition": definitions[scenario],
                            "measured": child_base
                            | {"control": False, "observations": observations},
                            "control": copy.deepcopy(child_base)
                            | {
                                "control": True,
                                "checksum": 1,
                                "observations": control_observations,
                            },
                        }
                    )
                    block_summaries.append(
                        {
                            "block_id": block,
                            "allocator_id": allocator,
                            "scenario_id": scenario,
                            "thread_point": point,
                            "measured": distribution(measured_values),
                            "control": distribution(control_values),
                        }
                    )
                absolute.append(
                    {
                        "allocator_id": allocator,
                        "scenario_id": scenario,
                        "thread_point": point,
                        "transaction_definition": definitions[scenario],
                        "measured": distribution(all_measured),
                        "control": distribution(all_control),
                        "overhead_valid": True,
                    }
                )
        value["latency"] = {
            "metric_schema_version": report.LATENCY_SCHEMA,
            "status": "complete",
            "invalid_reason": None,
            "metric_comparison_key": "c" * 64,
            "run": copy.deepcopy(value["run"]),
            "runner": copy.deepcopy(runner),
            "direction": "lower-is-better",
            "informational": True,
            "sampling_denominators": rates,
            "methodology": {
                "transaction_boundaries": {
                    "local": definitions["tiny-fixed-64"],
                    "cross-thread": definitions["cross-thread-producer-consumer"],
                    "large-object": definitions["large-objects"],
                },
                "quantile_method": "R/NumPy Type 7 linear interpolation",
                "sampling_schedule": "deterministic paired 1/N indices",
                "overhead_control": "reported separately and never subtracted",
                "bootstrap": "whole blocks with transaction quantile recomputation",
                "storage_decision": "raw vectors fit the public cap",
                "tail_policy": "all scheduler tails retained",
            },
            "absolute_summaries": absolute,
            "paired_summaries": paired,
            "block_summaries": block_summaries,
            "raw_samples": raw,
        }
        pending = value["pending_metrics"]
        assert isinstance(pending, list)
        value["pending_metrics"] = [
            item
            for item in pending
            if isinstance(item, dict) and item.get("metric_id") != "latency"
        ]
        return value

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

    def test_prepare_branch_preserves_git_and_replaces_the_index_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self.git(repository, "init")
            self.git(repository, "config", "user.name", "Fixture")
            self.git(repository, "config", "user.email", "fixture@example.invalid")
            (repository / ".github").mkdir()
            (repository / ".github" / "workflow.yml").write_text("fixture\n")
            (repository / ".clud").mkdir()
            (repository / ".clud" / "state").write_text("fixture\n")
            (repository / "README.md").write_text("fixture\n")
            self.git(repository, "add", "-A")
            self.git(repository, "commit", "-m", "fixture")

            worktree = root / "worktree"
            self.git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
            site, _digest = self.render_fixture(root / "rendered")

            report.prepare_branch(worktree, site)

            self.assertTrue((worktree / ".git").is_file())
            self.assertFalse((worktree / ".github").exists())
            self.assertFalse((worktree / ".clud").exists())
            self.assertEqual(report.SITE_FILES, report.git_index_files(worktree))
            self.assertEqual(b"", (worktree / ".nojekyll").read_bytes())
            for name in report.SITE_FILES:
                self.assertEqual((site / name).read_bytes(), (worktree / name).read_bytes())

            self.git(worktree, "commit", "-m", "published fixture")
            report.validate_git_revision(worktree, "HEAD", site)
            (worktree / "unexpected.txt").write_text("not sealed\n")
            self.git(worktree, "add", "unexpected.txt")
            self.git(worktree, "commit", "-m", "polluted fixture")
            with self.assertRaisesRegex(report.ReportError, "revision allowlist mismatch"):
                report.validate_git_revision(worktree, "HEAD", site)

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

    def test_html_has_stable_headline_chart_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site, _digest = self.render_fixture(Path(temporary))
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('<h2 id="throughput">Throughput</h2>', page)
            self.assertIn('<h2 id="history">Compatible history</h2>', page)

    def test_complete_memory_replaces_pending_panel_and_history_stays_compact(self) -> None:
        latest = self.with_complete_memory(self.load_latest())
        report.validate_latest(latest, "memory fixture")
        history = report.history_row(latest)
        self.assertIn("memory", history)
        memory_history = history["memory"]
        assert isinstance(memory_history, dict)
        self.assertNotIn("raw_samples", memory_history)
        report.validate_history_row(history, "memory history")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('<h2 id="memory">Linux process memory</h2>', page)
            self.assertNotIn("memory: pending", page)
            self.assertIn(
                b"Sampled peak RSS relative to upstream-mimalloc",
                (site / "benchmark-memory.png").read_bytes(),
            )
            self.assertIn(
                b"Speed-memory Pareto scatter",
                (site / "benchmark-pareto.png").read_bytes(),
            )
            self.assertIn(
                b"RSS over time with post-drain",
                (site / "benchmark-rss-timeline.png").read_bytes(),
            )
            self.assertIn(
                b"Fragmentation proxy",
                (site / "benchmark-fragmentation.png").read_bytes(),
            )

    def test_pending_memory_keeps_placeholder_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site, _digest = self.render_fixture(Path(temporary))
            pareto = (site / "benchmark-pareto.png").read_bytes()
            self.assertIn(b"PENDING speed-memory Pareto scatter", pareto)
            timeline = (site / "benchmark-rss-timeline.png").read_bytes()
            self.assertIn(b"PENDING RSS timeline", timeline)
            fragmentation = (site / "benchmark-fragmentation.png").read_bytes()
            self.assertIn(b"PENDING fragmentation proxy panel", fragmentation)

    def test_memory_bars_normalize_to_upstream_reference(self) -> None:
        medians = {
            "tcmalloc": 120 * 1024 * 1024,
            "jemalloc": 110 * 1024 * 1024,
            "upstream-mimalloc": 100 * 1024 * 1024,
            "bun-mimalloc": 95 * 1024 * 1024,
            "mimalloc-pprof": 90 * 1024 * 1024,
        }
        normalized = {
            allocator: float(median) / medians["upstream-mimalloc"]
            for allocator, median in medians.items()
        }
        self.assertAlmostEqual(1.2, normalized["tcmalloc"])

        def record(allocator: str, median: int) -> dict[str, object]:
            return {
                "scenario_id": "large-objects",
                "thread_point": "1",
                "metric_id": "sampled-peak-rss-bytes",
                "direction": "lower-is-better",
                "allocator_id": allocator,
                "summary": {
                    "count": 15,
                    "median": float(median),
                    "min": float(median) * 0.9,
                    "max": float(median) * 1.1,
                    "q1": float(median) * 0.95,
                    "q3": float(median) * 1.05,
                    "iqr": float(median) * 0.1,
                    "relative_iqr": 0.1,
                    "noisy": False,
                },
            }

        memory = {
            "absolute_summaries": [
                record(allocator, median) for allocator, median in medians.items()
            ]
        }
        canvas = report.Canvas(
            report.MEMORY_PANEL_WIDTH, report.MEMORY_PANEL_HEIGHT, (248, 250, 252)
        )
        cells = report.memory_bar_cells(memory, "sampled-peak-rss-bytes")
        normalized_cells = {
            key: {
                allocator: value / values["upstream-mimalloc"]
                for allocator, value in values.items()
            }
            for key, values in cells.items()
        }
        report.draw_ratio_bar_grid(canvas, normalized_cells)
        # Cell 0 sits at (45, 85); bars start at x=59, allocator rows are 15 px
        # apart, and the largest normalized value (1.2x) fills the 380 px bar
        # zone. The 1.0 reference line lands at 380 * 1.0/1.2 = 316 px.
        left, top = 45, 85
        bar_left = left + 14
        baseline_x = bar_left + 316

        def pixel(x: int, y: int) -> tuple[int, int, int]:
            offset = (y * report.MEMORY_PANEL_WIDTH + x) * 3
            value = canvas.pixels[offset : offset + 3]
            return value[0], value[1], value[2]

        # tcmalloc (1.2x) fills the bar zone; its row is index 0.
        self.assertEqual(report.COLORS[0], pixel(bar_left + 379, top + 10 + 4))
        # upstream-mimalloc (1.0x) ends at 316 px; the reference line drawn on
        # top occupies that exact column.
        self.assertEqual(report.COLORS[2], pixel(bar_left + 313, top + 10 + 2 * 15 + 4))
        self.assertEqual((90, 102, 115), pixel(baseline_x, top + 10 + 2 * 15 + 4))
        # mimalloc-pprof (0.9x) ends at 285 px; beyond it is background.
        self.assertEqual(report.COLORS[4], pixel(bar_left + 284, top + 10 + 4 * 15 + 4))
        self.assertEqual((235, 240, 246), pixel(bar_left + 286, top + 10 + 4 * 15 + 4))
        # The reference line stays visible across the tcmalloc bar too.
        self.assertEqual((90, 102, 115), pixel(baseline_x, top + 10 + 5))

    def test_fragmentation_panel_draws_ratio_bars_with_reference_line(self) -> None:
        ratios = {
            "tcmalloc": 1.4,
            "jemalloc": 1.2,
            "upstream-mimalloc": 1.0,
            "bun-mimalloc": 0.9,
            "mimalloc-pprof": 0.8,
        }

        def record(allocator: str, ratio: float) -> dict[str, object]:
            return {
                "scenario_id": "sawtooth-retain-drain",
                "thread_point": "physical-core",
                "metric_id": "fragmentation-proxy",
                "direction": "lower-is-better",
                "allocator_id": allocator,
                "summary": {
                    "count": 15,
                    "median": ratio,
                    "min": ratio * 0.9,
                    "max": ratio * 1.1,
                    "q1": ratio * 0.95,
                    "q3": ratio * 1.05,
                    "iqr": ratio * 0.1,
                    "relative_iqr": 0.1,
                    "noisy": False,
                },
            }

        memory = {
            "absolute_summaries": [record(allocator, ratio) for allocator, ratio in ratios.items()]
        }
        cells = report.memory_bar_cells(memory, "fragmentation-proxy")
        canvas = report.Canvas(
            report.MEMORY_PANEL_WIDTH, report.MEMORY_PANEL_HEIGHT, (248, 250, 252)
        )
        report.draw_ratio_bar_grid(canvas, cells)
        # 1.4 is the cell maximum: bars scale to 380 px, the 1.0 reference
        # line lands at 380 * 1/1.4 = 271 px.
        left, top = 45, 85
        bar_left = left + 14
        baseline_x = bar_left + 271

        def pixel(x: int, y: int) -> tuple[int, int, int]:
            offset = (y * report.MEMORY_PANEL_WIDTH + x) * 3
            value = canvas.pixels[offset : offset + 3]
            return value[0], value[1], value[2]

        self.assertEqual(report.COLORS[0], pixel(bar_left + 379, top + 10 + 4))
        # upstream-mimalloc (1.0) ends at 271 px; the reference line drawn on
        # top occupies that exact column.
        self.assertEqual(report.COLORS[2], pixel(bar_left + 268, top + 10 + 2 * 15 + 4))
        self.assertEqual((90, 102, 115), pixel(baseline_x, top + 10 + 2 * 15 + 4))
        # mimalloc-pprof (0.8) ends at 217 px; beyond it is background.
        self.assertEqual(report.COLORS[4], pixel(bar_left + 216, top + 10 + 4 * 15 + 4))
        self.assertEqual((235, 240, 246), pixel(bar_left + 218, top + 10 + 4 * 15 + 4))

    def test_memory_section_sits_next_to_throughput(self) -> None:
        latest = self.with_complete_memory(self.load_latest())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            page = (site / "index.html").read_text(encoding="utf-8")
            throughput = page.find('<h2 id="throughput">Throughput</h2>')
            memory = page.find('<h2 id="memory">Linux process memory</h2>')
            paired = page.find("<h2>Paired effects</h2>")
            self.assertGreater(throughput, -1)
            self.assertLess(throughput, memory)
            self.assertLess(memory, paired, "memory must pair with throughput, not history")
            self.assertIn("vs upstream", page)
            self.assertIn("1.00x", page, "the upstream allocator row must read 1.00x")

    def test_rss_timeline_sawtooth_rises_falls_and_decay_markers_at_offsets(self) -> None:
        # One synthetic cell: every allocator gets a sawtooth timeline that
        # rises to a distinct peak, falls back, and then decays through the
        # three post-drain return-to-OS points at distinct RSS values.
        samples: list[dict[str, object]] = []
        for index, allocator in enumerate(report.ALLOCATOR_IDS):
            peak = (300 + 20 * index) * 1024 * 1024
            samples.append(
                {
                    "scenario_id": "sawtooth-retain-drain",
                    "thread_point": "physical-core",
                    "allocator_id": allocator,
                    "workload_active_ns": 10_000_000,
                    "workload_drained_ns": 50_000_000,
                    "post_drain_sample_100ms_ns": 150_000_000,
                    "post_drain_sample_1s_ns": 1_050_000_000,
                    "post_drain_sample_5s_ns": 5_050_000_000,
                    "post_drain_rss_100ms_bytes": 160 * 1024 * 1024,
                    "post_drain_rss_1s_bytes": 130 * 1024 * 1024,
                    "post_drain_rss_5s_bytes": (110 + 20 * index) * 1024 * 1024,
                    "timeline": [
                        {"elapsed_ns": 15_000_000, "rss_bytes": 120 * 1024 * 1024},
                        {"elapsed_ns": 25_000_000, "rss_bytes": 180 * 1024 * 1024},
                        {"elapsed_ns": 35_000_000, "rss_bytes": peak},
                        {"elapsed_ns": 45_000_000, "rss_bytes": 200 * 1024 * 1024},
                    ],
                }
            )
        memory = {"raw_samples": samples}
        cells = report.timeline_cells(memory)
        self.assertEqual(1, len(cells))
        t_min, t_max, rss_min, rss_max = report.timeline_domain(cells)
        slot_width = report.TIMELINE_WIDTH // report.TIMELINE_COLS
        slot_height = report.TIMELINE_HEIGHT // report.TIMELINE_ROWS
        left, top, right, bottom = report.TIMELINE_SLOT_MARGINS
        plot_width = slot_width - left - right
        plot_height = slot_height - top - bottom
        canvas = report.Canvas(report.TIMELINE_WIDTH, report.TIMELINE_HEIGHT, (248, 250, 252))
        report.draw_rss_timeline(canvas, cells)
        # Time maps monotonically increasing; RSS maps monotonically decreasing.
        self.assertGreater(report.timeline_x(20_000_000, t_min, t_max, left, plot_width), left)
        self.assertGreater(
            report.timeline_x(40_000_000, t_min, t_max, left, plot_width),
            report.timeline_x(20_000_000, t_min, t_max, left, plot_width),
        )
        self.assertLess(
            report.timeline_y(300 * 1024 * 1024, rss_min, rss_max, top, plot_height),
            report.timeline_y(150 * 1024 * 1024, rss_min, rss_max, top, plot_height),
        )
        for index, allocator in enumerate(report.ALLOCATOR_IDS):
            color = report.COLORS[report.ALLOCATOR_IDS.index(allocator)]
            peak = (300 + 20 * index) * 1024 * 1024
            # The rise is visible: the peak pixel carries the allocator color.
            x = round(report.timeline_x(35_000_000, t_min, t_max, left, plot_width))
            y = round(report.timeline_y(peak, rss_min, rss_max, top, plot_height))
            offset = (y * report.TIMELINE_WIDTH + x) * 3
            self.assertEqual(color, tuple(canvas.pixels[offset : offset + 3]), f"{allocator} peak")
            # The fall is visible: the later, lower point sits below the peak.
            self.assertGreater(
                report.timeline_y(200 * 1024 * 1024, rss_min, rss_max, top, plot_height), y
            )
            # The 5 s post-drain diamond lands at the recorded elapsed offset.
            decay_x = round(report.timeline_x(5_050_000_000, t_min, t_max, left, plot_width))
            decay_y = round(
                report.timeline_y(
                    (110 + 20 * index) * 1024 * 1024, rss_min, rss_max, top, plot_height
                )
            )
            # The diamond is a hollow outline; probe its top vertex.
            offset = ((decay_y - 5) * report.TIMELINE_WIDTH + decay_x) * 3
            self.assertEqual(
                color, tuple(canvas.pixels[offset : offset + 3]), f"{allocator} 5s decay"
            )
        # The workload-drained dashed marker starts at the recorded drained
        # offset at the top of the plot.
        drained_x = round(report.timeline_x(50_000_000, t_min, t_max, left, plot_width))
        offset = ((top + 2) * report.TIMELINE_WIDTH + drained_x) * 3
        self.assertEqual(report.TIMELINE_DRAIN_COLOR, tuple(canvas.pixels[offset : offset + 3]))
        # The axis carries the three post-drain offset diamonds at their
        # recorded elapsed offsets.
        for delay_ns in (100_000_000, 1_000_000_000, 5_000_000_000):
            x = round(report.timeline_x(50_000_000 + delay_ns, t_min, t_max, left, plot_width))
            # The axis diamonds are hollow outlines; probe their top vertex.
            axis_diamond = ((top + plot_height + 10 - 3) * report.TIMELINE_WIDTH + x) * 3
            self.assertEqual(
                (90, 102, 115),
                tuple(canvas.pixels[axis_diamond : axis_diamond + 3]),
                f"axis diamond at +{delay_ns} ns",
            )

    def test_pareto_places_known_points_at_expected_coordinates(self) -> None:
        cells = (("scenario-00", "1"), ("scenario-01", "2"))
        fragmentation = {
            ("scenario-00", "1"): {
                "tcmalloc": 1.6,
                "jemalloc": 1.4,
                "upstream-mimalloc": 1.2,
                "bun-mimalloc": 1.1,
                "mimalloc-pprof": 1.0,
            },
            ("scenario-01", "2"): {
                "tcmalloc": 2.0,
                "jemalloc": 1.8,
                "upstream-mimalloc": 1.6,
                "bun-mimalloc": 1.4,
                "mimalloc-pprof": 1.2,
            },
        }
        throughput = {
            ("scenario-00", "1"): {
                "tcmalloc": 3.0e8,
                "jemalloc": 3.5e8,
                "upstream-mimalloc": 4.0e8,
                "bun-mimalloc": 4.2e8,
                "mimalloc-pprof": 4.5e8,
            },
            ("scenario-01", "2"): {
                "tcmalloc": 5.0e8,
                "jemalloc": 5.5e8,
                "upstream-mimalloc": 6.0e8,
                "bun-mimalloc": 6.2e8,
                "mimalloc-pprof": 6.5e8,
            },
        }

        def record(
            scenario: str, point: str, metric: str, allocator: str, median: float
        ) -> dict[str, object]:
            return {
                "scenario_id": scenario,
                "thread_point": point,
                "metric_id": metric,
                "direction": (
                    "lower-is-better" if metric == "fragmentation-proxy" else "higher-is-better"
                ),
                "allocator_id": allocator,
                "summary": {
                    "count": 15,
                    "median": median,
                    "min": median * 0.9,
                    "max": median * 1.1,
                    "q1": median * 0.95,
                    "q3": median * 1.05,
                    "iqr": median * 0.1,
                    "relative_iqr": 0.1,
                    "noisy": False,
                },
            }

        memory_records = [
            record(scenario, point, "fragmentation-proxy", allocator, median)
            for scenario, point in cells
            for allocator, median in fragmentation[(scenario, point)].items()
        ]
        core_records = [
            record(
                scenario,
                point,
                "throughput-operations-per-second",
                allocator,
                median,
            )
            for scenario, point in cells
            for allocator, median in throughput[(scenario, point)].items()
        ]
        memory_only: dict[str, object] = {"absolute_summaries": memory_records}
        latest: dict[str, object] = {
            "memory": memory_only,
            "absolute_summaries": core_records,
        }
        points = report.pareto_points(memory_only, latest)
        self.assertEqual(2 * len(report.ALLOCATOR_IDS), len(points))
        canvas = report.Canvas(report.PARETO_WIDTH, report.PARETO_HEIGHT, (248, 250, 252))
        report.draw_pareto(canvas, points)
        x_max, y_max = report.pareto_scale(points)
        self.assertAlmostEqual(x_max, 2.0 * 1.15)
        self.assertAlmostEqual(y_max, 6.5e8 * 1.15)
        by_cell = {
            (scenario, point, allocator): (frag, ops)
            for allocator, frag, ops, scenario, point in points
        }
        for scenario, point in cells:
            for allocator in report.ALLOCATOR_IDS:
                frag, ops = by_cell[(scenario, point, allocator)]
                x = round(report.pareto_x(frag, x_max))
                y = round(report.pareto_y(ops, y_max))
                color = report.COLORS[report.ALLOCATOR_IDS.index(allocator)]
                offset = (y * report.PARETO_WIDTH + x) * 3
                self.assertEqual(
                    color,
                    tuple(canvas.pixels[offset : offset + 3]),
                    f"marker for {allocator} at {scenario}/{point}",
                )
        # Placement invariants, from first principles: zero maps onto the axes,
        # the largest observed fragmentation maps 1/1.15 of the way across, and
        # both axes are monotonic in the value they carry.
        left, _top, right, bottom = report.PARETO_MARGINS
        self.assertEqual(round(report.pareto_x(0.0, x_max)), left)
        self.assertEqual(round(report.pareto_y(0.0, y_max)), report.PARETO_HEIGHT - bottom)
        max_frag = max(frag for _allocator, frag, _ops, _scenario, _point in points)
        self.assertAlmostEqual(
            report.pareto_x(max_frag, x_max),
            left + (report.PARETO_WIDTH - left - right) / 1.15,
        )
        self.assertGreater(report.pareto_x(1.5, x_max), report.pareto_x(1.0, x_max))
        self.assertLess(report.pareto_y(5.0, y_max), report.pareto_y(1.0, y_max))

    def test_pareto_renders_empty_state_when_no_cells_match(self) -> None:
        latest = self.with_complete_memory(self.load_latest())
        memory = report.validate_memory_report(latest["memory"], "memory fixture")
        # The fixture memory scenarios (large-objects, sawtooth-retain-drain,
        # ...) deliberately have no counterpart among the core scenario-00..07
        # cells, so the chart must say so instead of fabricating points.
        self.assertEqual([], report.pareto_points(memory, latest))
        canvas = report.Canvas(report.PARETO_WIDTH, report.PARETO_HEIGHT, (248, 250, 252))
        report.draw_pareto(canvas, [])
        muted = (117, 126, 140)
        self.assertTrue(
            any(
                tuple(canvas.pixels[index : index + 3]) == muted
                for index in range(0, len(canvas.pixels), 3)
            ),
            "empty Pareto chart must carry the no-match note",
        )

    def test_incomplete_memory_never_replaces_pending_and_prior_complete_is_carried(self) -> None:
        prior = self.with_complete_memory(self.load_latest())
        incomplete = copy.deepcopy(prior)
        memory = incomplete["memory"]
        assert isinstance(memory, dict)
        samples = memory["raw_samples"]
        assert isinstance(samples, list)
        samples.pop()
        with self.assertRaisesRegex(report.ReportError, "complete memory pair"):
            report.validate_latest(incomplete, "incomplete")

        bad_hwm = copy.deepcopy(prior)
        memory = bad_hwm["memory"]
        assert isinstance(memory, dict)
        samples = memory["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        samples[0]["hwm_discrepancy"] = True
        with self.assertRaisesRegex(report.ReportError, "VmHWM discrepancy"):
            report.validate_latest(bad_hwm, "bad hwm")

        bad_intervals = copy.deepcopy(prior)
        memory = bad_intervals["memory"]
        assert isinstance(memory, dict)
        samples = memory["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        sampling = samples[0]["sampling"]
        assert isinstance(sampling, dict)
        sampling["median_interval_ns"] = 1
        with self.assertRaisesRegex(report.ReportError, "interval distribution"):
            report.validate_latest(bad_intervals, "bad intervals")

        mixed_environment = copy.deepcopy(prior)
        memory = mixed_environment["memory"]
        assert isinstance(memory, dict)
        samples = memory["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        environment = samples[0]["environment"]
        assert isinstance(environment, dict)
        environment["kernel"] = "other"
        with self.assertRaisesRegex(report.ReportError, "mixed memory environments"):
            report.validate_latest(mixed_environment, "mixed environment")

        bad_child = copy.deepcopy(prior)
        memory = bad_child["memory"]
        assert isinstance(memory, dict)
        samples = memory["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        child = samples[0]["child_sample"]
        assert isinstance(child, dict)
        child["run_seed"] = 0
        with self.assertRaisesRegex(report.ReportError, "run_seed"):
            report.validate_latest(bad_child, "bad child")

        mismatched_pair = copy.deepcopy(prior)
        memory = mismatched_pair["memory"]
        assert isinstance(memory, dict)
        samples = memory["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        child = samples[0]["child_sample"]
        assert isinstance(child, dict)
        child["checksum"] = int(child["checksum"]) + 1
        with self.assertRaisesRegex(report.ReportError, "mismatched workload identity"):
            report.validate_latest(mismatched_pair, "mismatched pair")

        duplicate_summary = copy.deepcopy(prior)
        memory = duplicate_summary["memory"]
        assert isinstance(memory, dict)
        absolute = memory["absolute_summaries"]
        assert isinstance(absolute, list)
        absolute.append(copy.deepcopy(absolute[0]))
        with self.assertRaisesRegex(report.ReportError, "duplicate memory summary"):
            report.validate_latest(duplicate_summary, "duplicate summary")

        fresh = self.load_latest()
        self.assertTrue(report.carry_forward_optional_metrics(fresh, prior))
        report.validate_latest(fresh, "carried")
        self.assertIn("memory", fresh)
        self.assertNotIn("memory", report.history_row(fresh, include_optional_metrics=False))
        pending = fresh["pending_metrics"]
        assert isinstance(pending, list)
        self.assertNotIn(
            "memory", [item["metric_id"] for item in pending if isinstance(item, dict)]
        )

    def test_complete_latency_replaces_pending_with_transaction_chart_and_compact_history(
        self,
    ) -> None:
        latest = self.with_complete_latency(self.load_latest())
        report.validate_latest(latest, "latency fixture")
        history = report.history_row(latest)
        self.assertIn("latency", history)
        latency_history = history["latency"]
        assert isinstance(latency_history, dict)
        self.assertNotIn("raw_samples", latency_history)
        report.validate_history_row(history, "latency history")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('<h2 id="latency">Transaction latency</h2>', page)
            self.assertIn("never allocator-call latencies", page)
            self.assertIn("p50 ns", page)
            self.assertIn("p95 ns", page)
            self.assertIn("p99 ns", page)
            self.assertNotIn("latency: pending", page)
            chart = (site / "benchmark-latency.png").read_bytes()
            self.assertIn(b"Transaction latency", chart)
            self.assertIn(b"through free", chart)

    def test_incomplete_or_failed_control_latency_cannot_replace_pending(self) -> None:
        complete = self.with_complete_latency(self.load_latest())
        incomplete = copy.deepcopy(complete)
        latency = incomplete["latency"]
        assert isinstance(latency, dict)
        raw = latency["raw_samples"]
        assert isinstance(raw, list)
        raw.pop()
        with self.assertRaisesRegex(report.ReportError, "incomplete"):
            report.validate_latest(incomplete, "incomplete latency")

        failed_control = copy.deepcopy(complete)
        latency = failed_control["latency"]
        assert isinstance(latency, dict)
        absolute = latency["absolute_summaries"]
        assert isinstance(absolute, list) and isinstance(absolute[0], dict)
        control = absolute[0]["control"]
        assert isinstance(control, dict)
        control.update(p50_ns=60.0, p95_ns=61.0, p99_ns=62.0, max_ns=62)
        with self.assertRaisesRegex(report.ReportError, "control/sample threshold"):
            report.validate_latest(failed_control, "failed latency control")

        carried = self.load_latest()
        self.assertTrue(report.carry_forward_optional_metrics(carried, complete))
        report.validate_latest(carried, "carried latency")
        self.assertIn("latency", carried)
        pending = carried["pending_metrics"]
        assert isinstance(pending, list)
        self.assertNotIn(
            "latency", [item["metric_id"] for item in pending if isinstance(item, dict)]
        )

    def test_readme_embeds_only_real_charts_and_clicks_through_to_pages(self) -> None:
        readme = Path(__file__).resolve().parents[2] / "README.md"
        source = readme.read_text(encoding="utf-8")
        # #210 dropped the unlabeled throughput/history PNG stubs from the
        # README: only charts with real axes, legend, and values are embedded.
        # The headline numbers stay live dashboard links instead.
        expected = {}
        # Every scaling panel is embedded and clicks through to its section.
        for name in report.SCALING_PANELS.values():
            expected[name] = "https://zackees.github.io/mimalloc-pprof/#scaling"
        for image, destination in expected.items():
            raw = (
                f"https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/{image}"
            )
            self.assertIn(f"]({raw})]({destination})", source)
        # Every embedded name must be a file the renderer actually emits, or
        # the README links 404 on the published branch.
        self.assertTrue(set(expected) <= report.SITE_FILES)
        # The headline numbers link to the live dashboard (the README's Performance
        # section was restructured in #257-#263; it links the dashboard root now).
        self.assertIn("](https://zackees.github.io/mimalloc-pprof/)", source)
        for not_embedded in (
            "benchmark-throughput.png",
            "benchmark-history.png",
            "benchmark-memory.png",
            "benchmark-latency.png",
            "benchmark-scaling.png",
            "benchmark-pprof-tax.png",
        ):
            self.assertNotIn(not_embedded, source)
        self.assertIn("https://github.com/zackees/mimalloc-pprof/tree/benchmark-stats", source)
        self.assertIn("https://zackees.github.io/mimalloc-pprof/latest.json", source)

    def test_pages_audit_compares_every_public_payload_byte(self) -> None:
        class Response:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int) -> bytes:
                return self.data[:limit]

        with tempfile.TemporaryDirectory() as temporary:
            site, _digest = self.render_fixture(Path(temporary))
            requested: set[str] = set()

            def open_fixture(request: urllib.request.Request, timeout: int) -> Response:
                self.assertEqual(30, timeout)
                url = request.full_url
                name = url.split("/")[-1].split("?", 1)[0]
                requested.add(name)
                return Response((site / name).read_bytes())

            with mock.patch.object(report.urllib.request, "urlopen", side_effect=open_fixture):
                report.audit_pages(site, "https://example.invalid/benchmarks/", 1, 0)
            self.assertEqual(report.SITE_FILES - {".nojekyll"}, requested)

            def open_corrupt(request: urllib.request.Request, timeout: int) -> Response:
                response = open_fixture(request, timeout)
                return Response(response.data + b"corrupt")

            with (
                mock.patch.object(report.urllib.request, "urlopen", side_effect=open_corrupt),
                self.assertRaisesRegex(report.ReportError, "deployed bytes differ"),
            ):
                report.audit_pages(site, "https://example.invalid/benchmarks/", 1, 0)

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

    def with_complete_scaling(self, latest: dict[str, object]) -> dict[str, object]:
        value = copy.deepcopy(latest)
        runner = value["runner"]
        allocators = value["allocators"]
        assert isinstance(runner, dict) and isinstance(allocators, list)
        allocator_sources = {
            str(item["allocator_id"]): (str(item["source_sha"]), str(item["child_binary_sha256"]))
            for item in allocators
            if isinstance(item, dict)
        }
        # The production sweep runs on the 2P/4L hosted runner, so the dense
        # 6/8 points are oversubscribed there; model that runner exactly.
        allowed = 4
        summaries: list[dict[str, object]] = []
        raw: list[dict[str, object]] = []
        for pattern in report.SCALING_PATTERN_IDS:
            for threads in report.SCALING_THREAD_POINTS:
                for index, allocator in enumerate(report.ALLOCATOR_IDS):
                    median = 1_000_000.0 * threads * (1.0 + index / 10.0)
                    summaries.append(
                        {
                            "pattern": pattern,
                            "thread_count": threads,
                            "oversubscription_factor": threads / allowed,
                            "oversubscribed": threads / allowed > 1.0,
                            "allocator_id": allocator,
                            "block_count": report.SCALING_BLOCKS,
                            "median_throughput": median,
                            "min_throughput": median * 0.97,
                            "max_throughput": median * 1.03,
                            "speedup_vs_single_worker": float(threads),
                        }
                    )
                    source_sha, child_sha = allocator_sources[allocator]
                    for block in range(report.SCALING_BLOCKS):
                        raw.append(
                            {
                                "metric_schema_version": report.SCALING_SCHEMA,
                                "block_id": block,
                                "ordinal": index,
                                "pattern": pattern,
                                "thread_count": threads,
                                "allocator_id": allocator,
                                "allocator_source_sha": source_sha,
                                "child_binary_sha256": child_sha,
                                "operations_per_worker": 4096,
                                "reproduction_command": "benchmark-scaling-run --run-seed 1",
                                "response": {
                                    "protocol_version": "throughput-scaling-sparse-child-v1",
                                    "metric_schema_version": report.SCALING_SCHEMA,
                                    "allocator_id": allocator,
                                    "thread_count": threads,
                                    "alloc_calls": 1000,
                                    "realloc_calls": 10,
                                    "free_calls": 1000,
                                    "operation_count": 2010,
                                    "checksum": 12345,
                                    "remote_free_calls": 0,
                                    "producer_fallback_frees": 0,
                                    "setup_ns": 1,
                                    "warmup_ns": 0,
                                    "elapsed_ns": 750_000_000,
                                    "teardown_ns": 1,
                                    "throughput_operations_per_second": median,
                                },
                            }
                        )
        value["scaling"] = {
            "metric_schema_version": report.SCALING_SCHEMA,
            "status": "complete",
            "invalid_reason": None,
            "metric_comparison_key": "c" * 64,
            "run": copy.deepcopy(value["run"]),
            "runner": copy.deepcopy(runner),
            "topology": {
                # The production sweep runs on the 2P/4L hosted runner; the
                # dense 6/8 points are oversubscribed there and must be
                # shaded. The fixture models that runner, not the core
                # envelope's.
                "physical_cores": 2,
                "logical_cores": 4,
                "allowed_logical_cpus": 4,
                "affinity_policy": "unrestricted",
            },
            "direction": "higher-is-better",
            "informational": True,
            "rigor_label": report.SCALING_RIGOR_LABEL,
            "thread_points": list(report.SCALING_THREAD_POINTS),
            "patterns": [
                {
                    "pattern": pattern,
                    "description": "seeded random operation stream",
                    "min_size_bytes": 16,
                    "max_size_bytes": 4096,
                    "live_set_capacity": 256,
                    "cross_thread": pattern == "sparse-cross-thread",
                }
                for pattern in report.SCALING_PATTERN_IDS
            ],
            "methodology": {
                "rigor": report.SCALING_RIGOR_LABEL,
                "blocks_per_cell": report.SCALING_BLOCKS,
                "aggregation": "median with min/max",
                "operation_stream": "seeded random operation stream",
                "seed_chain": "splitmix64 chain over (run seed, pattern, threads, block, worker)",
                "pairing": "one stream replayed by all five allocators",
                "work_normalization": "frozen per-worker operation count",
                "oversubscription": "literal worker counts; oversubscribed points labeled",
                "cross_thread_backpressure": "bounded mailbox with producer self-free fallback",
                "statistics_omitted": "no bootstrap intervals and no noise gating",
            },
            "cell_summaries": summaries,
            "raw_samples": raw,
            "rss": {
                "metric_schema_version": report.SCALING_RSS_SCHEMA,
                "sampling": {
                    "source": "external /proc/<pid>/smaps_rollup Rss",
                    "method": "polled while the measured block runs; peak retained",
                    "poll_interval_ns": 5_000_000,
                },
                "cell_summaries": [
                    {
                        "pattern": pattern,
                        "thread_count": threads,
                        "allocator_id": allocator,
                        "block_count": report.SCALING_BLOCKS,
                        "median_peak_rss_bytes": (32 + 4 * threads + index) * 1024 * 1024,
                        "min_peak_rss_bytes": (30 + 4 * threads + index) * 1024 * 1024,
                        "max_peak_rss_bytes": (34 + 4 * threads + index) * 1024 * 1024,
                    }
                    for pattern in report.SCALING_PATTERN_IDS
                    for threads in report.SCALING_THREAD_POINTS
                    for index, allocator in enumerate(report.ALLOCATOR_IDS)
                ],
            },
        }
        pending = value["pending_metrics"]
        assert isinstance(pending, list)
        value["pending_metrics"] = [
            item
            for item in pending
            if not isinstance(item, dict) or item.get("metric_id") != "scaling"
        ]
        return value

    def test_complete_scaling_publishes_one_dark_panel_per_pattern(self) -> None:
        latest = self.with_complete_scaling(self.load_latest())
        report.validate_latest(latest, "scaling fixture")
        history = report.history_row(latest)
        self.assertIn("scaling", history)
        scaling_history = history["scaling"]
        assert isinstance(scaling_history, dict)
        self.assertNotIn("raw_samples", scaling_history)
        report.validate_history_row(history, "scaling history")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('<h2 id="scaling">Thread scaling (sparse sweep)</h2>', page)
            self.assertIn(report.SCALING_RIGOR_LABEL, page)
            self.assertNotIn("scaling: pending", page)
            for pattern, name in report.SCALING_PANELS.items():
                panel = (site / name).read_text(encoding="utf-8")
                self.assertTrue(panel.startswith("<svg"))
                # Baked-in dark background: the panel must not inherit a theme.
                self.assertIn(report.SCALING_INK["background"], panel)
                self.assertIn(report.SCALING_PANEL_TITLES[pattern], panel)
                self.assertIn(report.SCALING_RIGOR_LABEL, panel)
                for allocator in report.ALLOCATOR_IDS:
                    self.assertIn(allocator, panel)
                self.assertIn("oversubscribed", panel)
                # The RSS side-car must render its own side-by-side panel.
                self.assertIn("peak RSS by worker count", panel)
                self.assertIn("external smaps_rollup peak", panel)
                # The shaded band must actually be visible, not a zero-width
                # rectangle collapsed onto the last tick.
                band = re.search(
                    rf'<rect x="([\d.]+)" y="\d+" width="([\d.]+)" [^>]*'
                    rf'fill="{report.SCALING_INK["oversubscribed"]}"',
                    panel,
                )
                self.assertIsNotNone(band, "oversubscription band is missing")
                assert band is not None
                self.assertGreater(float(band.group(2)), 20.0)

    def test_pending_scaling_panels_are_dark_and_carry_no_numbers(self) -> None:
        latest = self.load_latest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            self.write_json(source, latest)
            site = root / "site"
            report.render(source, FIXTURE / "history.jsonl", site, root / "digest", False)
            for name in report.SCALING_PANELS.values():
                panel = (site / name).read_text(encoding="utf-8")
                self.assertIn(report.SCALING_INK["background"], panel)
                self.assertIn("pending", panel)
                report.validate_svg(site / name)

    def test_scaling_report_rejects_downgraded_rigor_and_incomplete_matrices(self) -> None:
        latest = self.with_complete_scaling(self.load_latest())

        relabeled = copy.deepcopy(latest)
        scaling = relabeled["scaling"]
        assert isinstance(scaling, dict)
        scaling["rigor_label"] = "headline quality"
        with self.assertRaisesRegex(report.ReportError, "coverage-mode"):
            report.validate_latest(relabeled, "relabeled")

        widened = copy.deepcopy(latest)
        scaling = widened["scaling"]
        assert isinstance(scaling, dict)
        scaling["thread_points"] = [1, 4, 16, 64]
        with self.assertRaisesRegex(report.ReportError, "thread_points"):
            report.validate_latest(widened, "widened")

        truncated = copy.deepcopy(latest)
        scaling = truncated["scaling"]
        assert isinstance(scaling, dict)
        summaries = scaling["cell_summaries"]
        assert isinstance(summaries, list)
        summaries.pop()
        with self.assertRaisesRegex(
            report.ReportError,
            f"{len(report.SCALING_PATTERN_IDS) * len(report.SCALING_THREAD_POINTS) * len(report.ALLOCATOR_IDS)} cells",
        ):
            report.validate_latest(truncated, "truncated")

        mispinned = copy.deepcopy(latest)
        scaling = mispinned["scaling"]
        assert isinstance(scaling, dict)
        samples = scaling["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        for sample in samples:
            assert isinstance(sample, dict)
            if sample["allocator_id"] == "upstream-mimalloc":
                sample["allocator_source_sha"] = "f" * 40
        with self.assertRaisesRegex(report.ReportError, "allocator source pins"):
            report.validate_latest(mispinned, "mispinned")

        # The sweep runs weekly against whichever daily core envelope is
        # published, so the fork is normally built from a newer commit. That
        # must overlay cleanly; only the lock-pinned competitors are frozen.
        newer_fork = copy.deepcopy(latest)
        scaling = newer_fork["scaling"]
        assert isinstance(scaling, dict)
        samples = scaling["raw_samples"]
        assert isinstance(samples, list)
        for sample in samples:
            assert isinstance(sample, dict)
            if sample["allocator_id"] == "mimalloc-pprof":
                sample["allocator_source_sha"] = "a" * 40
        report.validate_latest(newer_fork, "newer fork build")

        mixed_forks = copy.deepcopy(latest)
        scaling = mixed_forks["scaling"]
        assert isinstance(scaling, dict)
        samples = scaling["raw_samples"]
        assert isinstance(samples, list)
        changed = False
        for sample in samples:
            assert isinstance(sample, dict)
            if sample["allocator_id"] == "mimalloc-pprof" and not changed:
                sample["allocator_source_sha"] = "b" * 40
                changed = True
        with self.assertRaisesRegex(report.ReportError, "one mimalloc-pprof build"):
            report.validate_latest(mixed_forks, "mixed fork builds")

        still_pending = copy.deepcopy(latest)
        pending = still_pending["pending_metrics"]
        assert isinstance(pending, list)
        pending.append(
            {
                "metric_id": "scaling",
                "status": "pending",
                "reason": "pending - metric protocol not implemented",
                "phase_issue_url": "https://github.com/zackees/mimalloc-pprof/issues/203",
            }
        )
        with self.assertRaisesRegex(report.ReportError, "pending_metrics"):
            report.validate_latest(still_pending, "still pending")

    def test_scaling_rss_panel_places_known_points_at_expected_coordinates(self) -> None:
        latest = self.with_complete_scaling(self.load_latest())
        scaling = latest["scaling"]
        assert isinstance(scaling, dict)
        panel = report.scaling_svg(scaling, "sparse-tiny-hot").decode("utf-8")
        self.assertIn("peak RSS by worker count", panel)
        # The fixture RSS medians are (32 + 4*threads + index) MiB; the y
        # ceiling is the largest median across all cells with 12% headroom,
        # and the x axis is log2. Circles must land at the exact computed
        # coordinates on the right-hand RSS panel.
        rss_peak = (
            (32 + 4 * report.SCALING_THREAD_POINTS[-1] + len(report.ALLOCATOR_IDS) - 1)
            * 1024
            * 1024
        )
        ceiling = rss_peak * 1.12
        plot_height = report.SCALING_HEIGHT - report.SCALING_TOP - report.SCALING_BOTTOM
        for allocator_index in range(len(report.ALLOCATOR_IDS)):
            for threads in report.SCALING_THREAD_POINTS:
                median = (32 + 4 * threads + allocator_index) * 1024 * 1024
                cx = report.scaling_x_of(
                    threads, report.SCALING_DUAL_RSS_LEFT, report.SCALING_DUAL_PLOT_WIDTH
                )
                cy = report.scaling_y_of(median, ceiling, report.SCALING_TOP, plot_height)
                self.assertIn(
                    f'<circle cx="{cx:.1f}" cy="{cy:.1f}"',
                    panel,
                    f"RSS circle for allocator {allocator_index} at {threads} workers",
                )
        # Both panels render: throughput on the left, RSS on the right.
        self.assertIn(f'<rect x="{report.SCALING_LEFT}" y="{report.SCALING_TOP}"', panel)
        self.assertIn(f'<rect x="{report.SCALING_DUAL_RSS_LEFT}" y="{report.SCALING_TOP}"', panel)
        # First-principles axis invariants.
        self.assertLess(
            report.scaling_x_of(2, report.SCALING_LEFT, 100),
            report.scaling_x_of(4, report.SCALING_LEFT, 100),
        )
        self.assertLess(
            report.scaling_y_of(200.0, 100.0, report.SCALING_TOP, plot_height),
            report.scaling_y_of(50.0, 100.0, report.SCALING_TOP, plot_height),
        )

    def test_previous_sweep_lineage_still_validates_and_renders(self) -> None:
        # The published benchmark-stats branch carries a latest.json and history
        # rows recorded under the sparse (1, 4, 16) sweep. Densifying the thread
        # points must not make that data unreadable: without this, the first
        # daily run after the change fails on its own prior site.
        legacy = report.SCALING_THREAD_POINT_LINEAGES[0]
        self.assertNotEqual(legacy, report.SCALING_THREAD_POINTS)
        current = report.SCALING_THREAD_POINTS
        try:
            report.SCALING_THREAD_POINTS = legacy
            latest = self.with_complete_scaling(self.load_latest())
        finally:
            report.SCALING_THREAD_POINTS = current

        scaling = latest["scaling"]
        assert isinstance(scaling, dict)
        self.assertEqual(scaling["thread_points"], list(legacy))
        cells = scaling["cell_summaries"]
        assert isinstance(cells, list)
        self.assertEqual(
            len(cells),  # pyright: ignore[reportUnknownArgumentType]
            len(report.SCALING_PATTERN_IDS) * len(legacy) * len(report.ALLOCATOR_IDS),
        )
        # Validates as a known lineage rather than being rejected outright.
        report.validate_latest(latest, "legacy lineage")

        # And renders on its own axis: the last legacy point sits at the right
        # edge, which it would not if the axis spanned the current sweep.
        panel = report.scaling_svg(scaling, "sparse-tiny-hot").decode("utf-8")
        self.assertAlmostEqual(
            report.scaling_x_of(legacy[-1], 0, 100, legacy),
            100.0,
            places=6,
        )
        # The fixture carries the RSS side-car, so this is the dual-panel layout.
        for threads in legacy:
            x = report.scaling_x_of(
                threads, report.SCALING_LEFT, report.SCALING_DUAL_PLOT_WIDTH, legacy
            )
            self.assertIn(f'<line x1="{x:.1f}"', panel)
        self.assertIn(">16<", panel)
        self.assertNotIn(">6<", panel)
        # A shape that belongs to no lineage is still rejected.
        orphan = copy.deepcopy(latest)
        orphan_scaling = orphan["scaling"]
        assert isinstance(orphan_scaling, dict)
        orphan_scaling["thread_points"] = [1, 5, 25]
        with self.assertRaisesRegex(report.ReportError, "lineage"):
            report.validate_latest(orphan, "orphan lineage")

    def test_scaling_without_rss_sidecar_renders_a_single_panel(self) -> None:
        latest = self.with_complete_scaling(self.load_latest())
        scaling = latest["scaling"]
        assert isinstance(scaling, dict)
        del scaling["rss"]
        panel = report.scaling_svg(scaling, "sparse-tiny-hot").decode("utf-8")
        self.assertNotIn("peak RSS by worker count", panel)
        self.assertIn('viewBox="0 0 1000 590"', panel)

    def test_scaling_svg_is_inert_and_self_contained(self) -> None:
        latest = self.with_complete_scaling(self.load_latest())
        scaling = latest["scaling"]
        assert isinstance(scaling, dict)
        panel = report.scaling_svg(scaling, "sparse-tiny-hot").decode("utf-8")
        for forbidden in ("<script", "xlink:href", "<foreignObject", "<image", "@import"):
            self.assertNotIn(forbidden, panel)
        # The SVG namespace is the only permitted URL; nothing may be fetched.
        self.assertEqual(1, panel.count("http"))
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', panel)
        self.assertIn("viewBox=", panel)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "panel.svg"
            path.write_text(panel, encoding="utf-8")
            report.validate_svg(path)
            path.write_text(panel.replace("<svg", "<svg onload='x()'", 1), encoding="utf-8")
            with self.assertRaisesRegex(report.ReportError, "event handlers"):
                report.validate_svg(path)

    def test_history_merge_tolerates_float_round_trip_but_not_real_edits(self) -> None:
        # A published latest.json that passes back through the Rust overlay
        # binaries returns with floats occasionally shifted by one ULP
        # (serde_json parse/re-emit is not bit-identical to Python's). Observed
        # live on relative_iqr: ...413 in, ...412 out.
        self.assertTrue(
            report.equivalent_payload(
                {"relative_iqr": 0.11300528823968413},
                {"relative_iqr": 0.11300528823968412},
            )
        )
        self.assertFalse(
            report.equivalent_payload({"median": 100.0}, {"median": 100.0001}),
            "a real numeric change must still be rejected",
        )
        self.assertFalse(report.equivalent_payload({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(report.equivalent_payload([1.0, 2.0], [1.0]))
        self.assertFalse(
            report.equivalent_payload({"noisy": True}, {"noisy": 1}),
            "booleans must not compare equal to their integer value",
        )

        base = self.load_history_row()
        rounded = copy.deepcopy(base)
        absolute = rounded["absolute_summaries"]
        assert isinstance(absolute, list) and isinstance(absolute[0], dict)
        summary = absolute[0]["summary"]
        assert isinstance(summary, dict)
        median = float(summary["median"])  # pyright: ignore[reportArgumentType]
        summary["median"] = math.nextafter(median, math.inf)
        gained = copy.deepcopy(rounded)
        gained["scaling"] = {"placeholder": True}
        merged = report.merge_history([base], gained)
        self.assertEqual(1, len(merged))
        self.assertIn("scaling", merged[0])
        # The already-published row keeps its original bytes; only the new
        # metric is added.
        first = merged[0]["absolute_summaries"]
        assert isinstance(first, list) and isinstance(first[0], dict)
        original_summary = first[0]["summary"]
        assert isinstance(original_summary, dict)
        self.assertEqual(median, original_summary["median"])

        moved = copy.deepcopy(rounded)
        moved_absolute = moved["absolute_summaries"]
        assert isinstance(moved_absolute, list) and isinstance(moved_absolute[0], dict)
        moved_summary = moved_absolute[0]["summary"]
        assert isinstance(moved_summary, dict)
        moved_summary["median"] = median * 1.01
        moved["scaling"] = {"placeholder": True}
        with self.assertRaisesRegex(report.ReportError, "may only gain"):
            report.merge_history([base], moved)

    def test_axis_uses_one_unit_for_every_tick(self) -> None:
        unit = report.axis_unit(1_644.0)
        self.assertEqual(
            ["0", "0.4k", "0.8k", "1.2k", "1.6k"],
            [report.format_throughput(1_644.0 * step / 4, unit) for step in range(5)],
        )
        megabyte_unit = report.axis_unit(5_000_000.0)
        self.assertEqual("5.0M", report.format_throughput(5_000_000.0, megabyte_unit))


class LegacyAllocatorLineageTests(unittest.TestCase):
    """The published branch still carries artifacts recorded before the Bun row
    landed (#325). They must keep validating, carrying forward, and rendering:
    a lineage that stops parsing is a lineage that has been silently rewritten."""

    LEGACY = FIXTURE / "legacy"

    def load_legacy_latest(self) -> dict[str, object]:
        return json.loads((self.LEGACY / "latest.json").read_text(encoding="utf-8"))

    def legacy_with_optional_sections(self) -> dict[str, object]:
        """The pre-#325 envelope with complete memory/latency/scaling sections,
        built against the four-allocator set those runs actually measured."""

        helper = BenchmarkReportTests("test_memory_section_sits_next_to_throughput")
        with mock.patch.object(report, "ALLOCATOR_IDS", report.LEGACY_ALLOCATOR_IDS):
            legacy = self.load_legacy_latest()
            legacy = helper.with_complete_memory(legacy)
            legacy = helper.with_complete_latency(legacy)
            return helper.with_complete_scaling(legacy)

    def test_the_current_allocator_set_is_the_newest_accepted_lineage(self) -> None:
        self.assertEqual(report.ALLOCATOR_IDS, report.ALLOCATOR_ID_LINEAGES[-1])
        self.assertIn(report.LEGACY_ALLOCATOR_IDS, report.ALLOCATOR_ID_LINEAGES)
        self.assertNotIn("bun-mimalloc", report.LEGACY_ALLOCATOR_IDS)

    def test_an_unknown_allocator_set_is_still_rejected(self) -> None:
        with self.assertRaises(report.ReportError):
            report.declared_allocators(["tcmalloc", "jemalloc"], "truncated")
        with self.assertRaises(report.ReportError):
            report.declared_allocator_order(list(reversed(report.ALLOCATOR_IDS)), "out of order")

    def test_a_four_allocator_history_row_still_validates(self) -> None:
        row = json.loads((self.LEGACY / "history.jsonl").read_text(encoding="utf-8"))
        identities = row["allocator_identities"]
        assert isinstance(identities, list)
        self.assertEqual(
            list(report.LEGACY_ALLOCATOR_IDS),
            [item["allocator_id"] for item in identities],
        )
        report.validate_history_row(row, "legacy history")

    def test_a_four_allocator_latest_still_validates(self) -> None:
        report.validate_latest(self.legacy_with_optional_sections(), "legacy latest")

    def test_sections_measured_before_the_bun_row_carry_onto_a_five_row_latest(self) -> None:
        current = json.loads((FIXTURE / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("memory", current)
        self.assertTrue(
            report.carry_forward_optional_metrics(current, self.legacy_with_optional_sections())
        )
        # The core run is five rows, the carried sections are four; the envelope
        # must still validate and render rather than going red on the first run
        # after a new allocator lands.
        report.validate_latest(current, "mixed latest")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.json"
            source.write_text(json.dumps(current) + "\n", encoding="utf-8", newline="\n")
            site = root / "site"
            report.render(source, self.LEGACY / "history.jsonl", site, root / "digest", False)
            self.assertTrue((site / "index.html").is_file())
            self.assertTrue((site / "benchmark-rss-timeline.png").is_file())

    def test_a_carried_section_may_not_carry_a_different_pin(self) -> None:
        """Tolerating a missing row must not tolerate a moved one: if the core
        run rebuilt a competitor from another commit, its carried-forward memory
        numbers no longer describe that build."""

        current = json.loads((FIXTURE / "latest.json").read_text(encoding="utf-8"))
        self.assertTrue(
            report.carry_forward_optional_metrics(current, self.legacy_with_optional_sections())
        )
        report.validate_latest(current, "mixed latest")
        allocators = current["allocators"]
        assert isinstance(allocators, list)
        moved = next(
            item
            for item in allocators
            if isinstance(item, dict) and item["allocator_id"] == "upstream-mimalloc"
        )
        moved["source_sha"] = "f" * 40
        with self.assertRaisesRegex(report.ReportError, "allocator source pins"):
            report.validate_latest(current, "moved competitor pin")


if __name__ == "__main__":
    unittest.main()
