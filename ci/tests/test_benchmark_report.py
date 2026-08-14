from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportIndexIssue=false

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
        self.assertEqual(15 * 4, len(templates))
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
            self.assertIn(b"Linux process RSS", (site / "benchmark-memory.png").read_bytes())

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
        expected = {
            "benchmark-throughput.png": "https://zackees.github.io/mimalloc-pprof/#throughput",
            "benchmark-history.png": "https://zackees.github.io/mimalloc-pprof/#history",
        }
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
        for pending in (
            "benchmark-memory.png",
            "benchmark-latency.png",
            "benchmark-scaling.png",
            "benchmark-pprof-tax.png",
        ):
            self.assertNotIn(pending, source)
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
        allowed = int(runner["logical_cores"])
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
                "physical_cores": int(runner["physical_cores"]),
                "logical_cores": allowed,
                "allowed_logical_cpus": allowed,
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
                "pairing": "one stream replayed by all four allocators",
                "work_normalization": "frozen per-worker operation count",
                "oversubscription": "literal worker counts; oversubscribed points labeled",
                "cross_thread_backpressure": "bounded mailbox with producer self-free fallback",
                "statistics_omitted": "no bootstrap intervals and no noise gating",
            },
            "cell_summaries": summaries,
            "raw_samples": raw,
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
        with self.assertRaisesRegex(report.ReportError, "48 cells"):
            report.validate_latest(truncated, "truncated")

        mispinned = copy.deepcopy(latest)
        scaling = mispinned["scaling"]
        assert isinstance(scaling, dict)
        samples = scaling["raw_samples"]
        assert isinstance(samples, list) and isinstance(samples[0], dict)
        samples[0]["allocator_source_sha"] = "f" * 40
        with self.assertRaisesRegex(report.ReportError, "allocator source pins"):
            report.validate_latest(mispinned, "mispinned")

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

    def test_axis_uses_one_unit_for_every_tick(self) -> None:
        unit = report.axis_unit(1_644.0)
        self.assertEqual(
            ["0", "0.4k", "0.8k", "1.2k", "1.6k"],
            [report.format_throughput(1_644.0 * step / 4, unit) for step in range(5)],
        )
        megabyte_unit = report.axis_unit(5_000_000.0)
        self.assertEqual("5.0M", report.format_throughput(5_000_000.0, megabyte_unit))


if __name__ == "__main__":
    unittest.main()
