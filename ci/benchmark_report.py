#!/usr/bin/env python3
"""Render and seal validator-approved mimalloc benchmark reports.

The renderer deliberately owns no benchmark statistics.  It accepts only the
strict ``benchmark-latest-v1`` publication envelope emitted by the Rust
validator and rejects raw or aggregate-only inputs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

LATEST_SCHEMA = "benchmark-latest-v1"
RAW_SCHEMA = "benchmark-raw-v1"
HISTORY_SCHEMA = "benchmark-history-v1"
STATISTICS_VERSION = "paired-log-median-bootstrap-v1"
SUITE_VERSION = "core-throughput-v1"
VALIDATOR_VERSION = "benchmark-validator-v1"
ALLOCATOR_IDS = (
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "bun-mimalloc",
    "mimalloc-pprof",
)
# Allocators whose source commit is fixed by allocator-lock.json. A secondary
# metric overlaid onto an older core envelope must match these exactly; the
# fork's own commit legitimately moves between the two runs.
LOCK_PINNED_ALLOCATORS = ("tcmalloc", "jemalloc", "upstream-mimalloc", "bun-mimalloc")
VALIDATION_CHECKS = (
    "schema-and-versions",
    "run-identity",
    "runner-fingerprint",
    "allocator-provenance",
    "core-matrix-completeness",
    "paired-block-integrity",
    "numeric-and-status-validity",
    "calibration-protocol",
)
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
TOP_LEVEL_FIELDS = {
    "latest_schema_version",
    "raw_schema_version",
    "statistics_version",
    "suite_version",
    "validation_report",
    "run",
    "runner",
    "allocators",
    "calibrations",
    "raw_samples",
    "absolute_summaries",
    "paired_summaries",
    "comparison_key",
    "methodology",
    "pending_metrics",
    "canonical_urls",
    "reproduction_command",
    "actions_run_url",
}
OPTIONAL_TOP_LEVEL_FIELDS = {"memory", "latency", "scaling"}
HISTORY_FIELDS = {
    "history_schema_version",
    "statistics_version",
    "suite_version",
    "run",
    "comparison_key",
    "runner",
    "allocator_identities",
    "absolute_summaries",
    "paired_summaries",
}
OPTIONAL_HISTORY_FIELDS = {"memory", "latency", "scaling"}
RUN_FIELDS = {
    "source_repository",
    "source_sha",
    "source_ref",
    "run_origin",
    "run_id",
    "run_attempt",
    "generated_at_utc",
}
RUNNER_FIELDS = {
    "runner_class",
    "stable_host_id",
    "fingerprint_sha256",
    "cpu_model",
    "os",
    "os_image",
    "os_version",
    "kernel",
    "architecture",
    "physical_cores",
    "logical_cores",
    "target",
    "rustc",
    "affinity",
    "power",
}
SCALING_SCHEMA = "throughput-scaling-sparse-v1"
SCALING_RSS_SCHEMA = "throughput-scaling-rss-v1"
SCALING_RIGOR_LABEL = "coverage mode - reduced statistical rigor (3 blocks)"
SCALING_BLOCKS = 3
# Dense sweep up to 2x the 4-vCPU hosted runner's logical cores. The 6/8
# points are oversubscribed and describe contention, not core scaling. The
# thread points are part of the metric comparison key, so changing them
# starts a new history lineage instead of rewriting the sparse one.
SCALING_THREAD_POINTS = (1, 2, 3, 4, 6, 8)
# Every sweep shape this validator will accept, oldest first. New production
# runs must use SCALING_THREAD_POINTS, but the published branch still carries
# rows -- and a `latest.json` -- recorded under the earlier shape, and those
# must keep validating and rendering forever: a lineage that stops parsing is
# a lineage that has been silently rewritten. A report is checked against the
# shape it declares, not against the current one.
SCALING_THREAD_POINT_LINEAGES = ((1, 4, 16), SCALING_THREAD_POINTS)
SCALING_PATTERN_IDS = (
    "sparse-tiny-hot",
    "sparse-mixed-general",
    "sparse-large-buffers",
    "sparse-cross-thread",
)
# One panel per allocation pattern. Separate files (rather than one facet grid)
# keep each chart legible in the README and on a phone.
SCALING_PANELS = {
    pattern: f"benchmark-scaling-{pattern.removeprefix('sparse-')}.svg"
    for pattern in SCALING_PATTERN_IDS
}
SCALING_PANEL_TITLES = {
    "sparse-tiny-hot": "Tiny hot path (16-64 B)",
    "sparse-mixed-general": "General mix (8 B-4 KiB, with realloc)",
    "sparse-large-buffers": "Large buffers (64 KiB-4 MiB, page-touched)",
    "sparse-cross-thread": "Cross-thread handoff (16-512 B, remote free)",
}
SITE_FILES = {
    ".nojekyll",
    "index.html",
    "latest.json",
    "manifest.json",
    "history.jsonl",
    "benchmark-throughput.png",
    "benchmark-history.png",
    "benchmark-memory.png",
    "benchmark-pareto.png",
    "benchmark-rss-timeline.png",
    "benchmark-fragmentation.png",
    "benchmark-latency.png",
    "benchmark-pprof-tax.png",
    *SCALING_PANELS.values(),
}
PNG_DIMENSIONS = {
    "benchmark-throughput.png": (1280, 720),
    "benchmark-history.png": (1280, 720),
    "benchmark-memory.png": (960, 540),
    "benchmark-pareto.png": (960, 540),
    "benchmark-rss-timeline.png": (1280, 720),
    "benchmark-fragmentation.png": (960, 540),
    "benchmark-latency.png": (960, 540),
    "benchmark-pprof-tax.png": (960, 540),
}
SVG_FILES = frozenset(SCALING_PANELS.values())
FILE_CAPS = {
    ".nojekyll": 0,
    "index.html": 2 * 1024 * 1024,
    "latest.json": 128 * 1024 * 1024,
    "manifest.json": 1024 * 1024,
    "history.jsonl": 32 * 1024 * 1024,
    **dict.fromkeys(PNG_DIMENSIONS, 12 * 1024 * 1024),
    **dict.fromkeys(SVG_FILES, 1024 * 1024),
}
MEDIA_TYPES = {
    ".nojekyll": "application/octet-stream",
    "index.html": "text/html; charset=utf-8",
    "latest.json": "application/json",
    "history.jsonl": "application/x-ndjson",
    **dict.fromkeys(PNG_DIMENSIONS, "image/png"),
    **dict.fromkeys(SVG_FILES, "image/svg+xml"),
}
ROLES = {
    ".nojekyll": "github-pages-marker",
    "index.html": "report-index",
    "latest.json": "validated-latest-data",
    "history.jsonl": "bounded-compatible-history",
    "benchmark-throughput.png": "throughput-chart",
    "benchmark-history.png": "history-chart",
    "benchmark-memory.png": "memory-panel",
    "benchmark-pareto.png": "pareto-panel",
    "benchmark-rss-timeline.png": "rss-timeline-panel",
    "benchmark-fragmentation.png": "fragmentation-panel",
    "benchmark-latency.png": "latency-panel",
    "benchmark-pprof-tax.png": "pending-pprof-tax-panel",
    **dict.fromkeys(SCALING_PANELS.values(), "scaling-panel"),
}

MEMORY_SCHEMA = "linux-process-memory-v1"
MEMORY_METRICS = (
    "sampled-peak-rss-bytes",
    "post-drain-rss-100ms-bytes",
    "post-drain-rss-1s-bytes",
    "post-drain-rss-5s-bytes",
    "fragmentation-proxy",
)
MEMORY_CELLS = (
    ("large-objects", "1"),
    ("large-objects", "2"),
    ("sawtooth-retain-drain", "1"),
    ("sawtooth-retain-drain", "physical-core"),
    ("small-log-mixed", "physical-core"),
    ("cross-thread-producer-consumer", "physical-core"),
    ("thread-churn", "physical-core"),
)
LATENCY_SCHEMA = "transaction-latency-v1"
LATENCY_CHILD_PROTOCOL = "transaction-latency-child-v1"
LATENCY_QUANTILES = ("p50", "p95", "p99")
LATENCY_CELLS = (
    ("tiny-fixed-64", "1"),
    ("small-log-mixed", "1"),
    ("small-log-mixed", "physical-core"),
    ("cross-thread-producer-consumer", "physical-core"),
    ("large-objects", "1"),
)
LATENCY_REPORT_FIELDS = {
    "metric_schema_version",
    "status",
    "invalid_reason",
    "metric_comparison_key",
    "run",
    "runner",
    "direction",
    "informational",
    "sampling_denominators",
    "methodology",
    "absolute_summaries",
    "paired_summaries",
    "block_summaries",
    "raw_samples",
}
MEMORY_REPORT_FIELDS = {
    "metric_schema_version",
    "status",
    "invalid_reason",
    "metric_comparison_key",
    "run",
    "runner",
    "sampling_target_interval_ns",
    "purge_policy",
    "units",
    "direction",
    "informational",
    "methodology",
    "absolute_summaries",
    "paired_summaries",
    "raw_samples",
}
MEMORY_METHODOLOGY = {
    "rss_source": "/proc/<pid>/smaps_rollup Rss, parsed as integer kB * 1024",
    "hwm_source": "/proc/<pid>/status VmHWM, parsed as integer kB * 1024; cross-check only",
    "baseline_definition": "external RSS/HWM after child warmup and baseline-ready, before begin",
    "sampled_peak_definition": "maximum external smaps_rollup RSS timestamped inside workload-active..workload-drained",
    "post_drain_definition": "external smaps_rollup RSS at >=100ms, >=1s, and >=5s after workload-drained",
    "fragmentation_formula": "(sampled_peak_rss_bytes - baseline_rss_bytes) / peak_live_requested_bytes; both operands must be positive",
    "hwm_discrepancy_tolerance": "flag when abs(sampled RSS delta - VmHWM delta) > max(8 MiB, 20% of the larger positive delta)",
    "page_touch_contract": "every allocation touches deterministic boundary bytes and at least one byte per OS page",
    "purge_policy": "natural allocator behavior only; no allocator-specific purge call",
}
MEMORY_HISTORY_FIELDS = MEMORY_REPORT_FIELDS - {"invalid_reason", "runner", "raw_samples"} | {
    "runner_fingerprint_sha256"
}
MEMORY_SAMPLE_FIELDS = {
    "metric_schema_version",
    "block_id",
    "ordinal",
    "workload_seed",
    "allocator_id",
    "allocator_source_sha",
    "child_binary_sha256",
    "scenario_id",
    "thread_point",
    "thread_count",
    "baseline_ready_ns",
    "workload_active_ns",
    "workload_drained_ns",
    "post_drain_sample_100ms_ns",
    "post_drain_sample_1s_ns",
    "post_drain_sample_5s_ns",
    "sampler_pid",
    "sampled_pid",
    "baseline_rss_bytes",
    "baseline_hwm_bytes",
    "sampled_peak_rss_bytes",
    "kernel_peak_hwm_bytes",
    "peak_live_requested_bytes",
    "post_drain_rss_100ms_bytes",
    "post_drain_rss_1s_bytes",
    "post_drain_rss_5s_bytes",
    "sampled_peak_rss_delta_bytes",
    "post_drain_rss_delta_100ms_bytes",
    "post_drain_rss_delta_1s_bytes",
    "post_drain_rss_delta_5s_bytes",
    "fragmentation_proxy",
    "hwm_discrepancy",
    "hwm_tolerance_bytes",
    "sampling",
    "timeline",
    "environment",
    "child_sample",
}
CHILD_SAMPLE_FIELDS = {
    "schema_version",
    "suite_version",
    "run_kind",
    "execution_mode",
    "run_seed",
    "block_id",
    "ordinal",
    "workload_seed",
    "allocator_id",
    "allocator_version",
    "allocator_source_sha",
    "allocator_library_sha256",
    "child_binary_sha256",
    "scenario_id",
    "scenario_version",
    "thread_point",
    "thread_count",
    "operation_unit",
    "operation_count",
    "requested_transactions",
    "completed_transactions",
    "allocation_calls",
    "calloc_calls",
    "aligned_allocation_calls",
    "free_calls",
    "realloc_calls",
    "setup_ns",
    "warmup_ns",
    "elapsed_ns",
    "teardown_ns",
    "throughput_operations_per_second",
    "checksum",
    "peak_live_requested_bytes",
    "timed_out",
    "crashed",
    "exit_code",
    "signal",
    "runner",
    "toolchain",
    "reproduction_command",
}
CHILD_RUNNER_FIELDS = {"os", "architecture", "physical_cores", "logical_cores"}
CHILD_TOOLCHAIN_FIELDS = {"rustc", "target", "compiler", "linker"}
CHILD_BLOCK_IDENTITY_FIELDS = {
    "run_seed",
    "workload_seed",
    "scenario_id",
    "scenario_version",
    "thread_point",
    "thread_count",
    "operation_unit",
    "operation_count",
    "requested_transactions",
    "completed_transactions",
    "allocation_calls",
    "calloc_calls",
    "aligned_allocation_calls",
    "free_calls",
    "realloc_calls",
    "checksum",
}


class ReportError(RuntimeError):
    """A path-qualified publication contract failure."""


def fail(message: str) -> NoReturn:
    raise ReportError(message)


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{label}: expected object")
    return cast(dict[str, object], value)


def list_value(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        fail(f"{label}: expected array")
    return cast(list[object], value)


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{label}: expected non-empty string")
    return value


def https_url_value(value: object, label: str) -> str:
    result = string_value(value, label)
    if not result.startswith("https://") or any(character in result for character in "\r\n\t"):
        fail(f"{label}: expected an HTTPS URL")
    return result


def int_value(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(f"{label}: expected integer >= {minimum}")
    return value


def float_value(value: object, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{label}: expected numeric value")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        fail(f"{label}: expected {'positive ' if positive else ''}finite value")
    return result


def exact_fields(value: Mapping[str, object], fields: set[str], label: str) -> None:
    actual = set(value)
    if actual != fields:
        fail(
            f"{label}: fields mismatch; missing={sorted(fields - actual)} unexpected={sorted(actual - fields)}"
        )


def exact_fields_with_optional(
    value: Mapping[str, object], required: set[str], optional: set[str], label: str
) -> None:
    actual = set(value)
    missing = required - actual
    unexpected = actual - required - optional
    if missing or unexpected:
        fail(f"{label}: fields mismatch; missing={sorted(missing)} unexpected={sorted(unexpected)}")


def reject_nonfinite(value: object, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        fail(f"{label}: non-finite JSON number")
    if isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            reject_nonfinite(item, f"{label}[{index}]")
    elif isinstance(value, dict):
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                fail(f"{label}: object key is not a string")
            reject_nonfinite(item, f"{label}.{key}")


def parse_json_bytes(data: bytes, label: str) -> dict[str, object]:
    def invalid_constant(value: str) -> NoReturn:
        fail(f"{label}: invalid JSON number {value}")

    try:
        parsed: object = json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"{label}: invalid UTF-8 JSON: {error}")
    reject_nonfinite(parsed, label)
    return object_value(parsed, label)


def read_json(path: Path) -> dict[str, object]:
    try:
        return parse_json_bytes(path.read_bytes(), str(path))
    except OSError as error:
        fail(f"{path}: cannot read: {error}")


def comparison_digest(value: object, label: str) -> str:
    digest = string_value(value, label)
    if not HEX_64.fullmatch(digest):
        fail(f"{label}: SHA-256 must be lowercase hexadecimal")
    return digest


def validate_run(value: object, label: str) -> dict[str, object]:
    run = object_value(value, label)
    exact_fields(run, RUN_FIELDS, label)
    if run.get("source_repository") != "https://github.com/zackees/mimalloc-pprof":
        fail(f"{label}.source_repository: unexpected repository")
    if not HEX_40.fullmatch(string_value(run.get("source_sha"), f"{label}.source_sha")):
        fail(f"{label}.source_sha: expected full lowercase commit")
    source_ref = string_value(run.get("source_ref"), f"{label}.source_ref")
    if source_ref in ("HEAD", "latest"):
        fail(f"{label}.source_ref: floating references are forbidden")
    if run.get("run_origin") not in ("github-actions", "local"):
        fail(f"{label}.run_origin: unsupported origin")
    string_value(run.get("run_id"), f"{label}.run_id")
    int_value(run.get("run_attempt"), f"{label}.run_attempt", 1)
    parse_timestamp(
        string_value(run.get("generated_at_utc"), f"{label}.generated_at_utc"),
        f"{label}.generated_at_utc",
    )
    return run


def validate_runner(value: object, label: str) -> dict[str, object]:
    runner = object_value(value, label)
    exact_fields(runner, RUNNER_FIELDS, label)
    if runner.get("runner_class") not in (
        "github-hosted",
        "self-hosted-informational",
        "stable-host",
    ):
        fail(f"{label}.runner_class: unsupported class")
    if not isinstance(runner.get("stable_host_id"), str):
        fail(f"{label}.stable_host_id: expected string")
    fingerprint = string_value(runner.get("fingerprint_sha256"), f"{label}.fingerprint_sha256")
    if not HEX_64.fullmatch(fingerprint):
        fail(f"{label}.fingerprint_sha256: invalid digest")
    for field in ("cpu_model", "os", "os_image", "os_version", "kernel", "architecture", "rustc"):
        string_value(runner.get(field), f"{label}.{field}")
    for field in ("physical_cores", "logical_cores"):
        int_value(runner.get(field), f"{label}.{field}", 1)
    if runner.get("target") != "x86_64-unknown-linux-gnu":
        fail(f"{label}.target: unsupported publication target")
    affinity = object_value(runner.get("affinity"), f"{label}.affinity")
    exact_fields(affinity, {"policy", "logical_cpu_ids"}, f"{label}.affinity")
    if affinity.get("policy") not in ("unrestricted", "pinned"):
        fail(f"{label}.affinity.policy: unsupported policy")
    cpu_ids = [
        int_value(item, f"{label}.affinity.logical_cpu_ids")
        for item in list_value(affinity.get("logical_cpu_ids"), f"{label}.affinity.logical_cpu_ids")
    ]
    if len(cpu_ids) != len(set(cpu_ids)):
        fail(f"{label}.affinity.logical_cpu_ids: duplicates are forbidden")
    power = object_value(runner.get("power"), f"{label}.power")
    exact_fields(power, {"governor", "boost", "frequency_policy"}, f"{label}.power")
    for field in power:
        string_value(power[field], f"{label}.power.{field}")
    return runner


def validate_allocator_build(value: object, label: str, expected_id: str) -> dict[str, object]:
    allocator = object_value(value, label)
    fields = {
        "allocator_id",
        "allocator_version",
        "source_kind",
        "canonical_repository",
        "source_sha",
        "source_archive_url",
        "source_archive_sha256",
        "source_tree_sha256",
        "source_patches",
        "build_system",
        "build_commands",
        "build_flags",
        "compiler",
        "linker",
        "static_library_sha256",
        "child_binary_sha256",
        "options",
    }
    exact_fields(allocator, fields, label)
    if allocator.get("allocator_id") != expected_id:
        fail(f"{label}.allocator_id: expected {expected_id}")
    for field in (
        "allocator_version",
        "canonical_repository",
        "source_archive_url",
        "build_system",
        "compiler",
        "linker",
    ):
        string_value(allocator.get(field), f"{label}.{field}")
    if allocator.get("source_kind") not in ("archive", "checkout"):
        fail(f"{label}.source_kind: invalid")
    for field, pattern in (
        ("source_sha", HEX_40),
        ("source_tree_sha256", HEX_64),
        ("static_library_sha256", HEX_64),
        ("child_binary_sha256", HEX_64),
    ):
        if not pattern.fullmatch(string_value(allocator.get(field), f"{label}.{field}")):
            fail(f"{label}.{field}: invalid digest")
    archive_digest = string_value(
        allocator.get("source_archive_sha256"), f"{label}.source_archive_sha256"
    )
    if archive_digest != "not-applicable" and not HEX_64.fullmatch(archive_digest):
        fail(f"{label}.source_archive_sha256: invalid digest")
    for index, value in enumerate(
        list_value(allocator.get("source_patches"), f"{label}.source_patches")
    ):
        patch = object_value(value, f"{label}.source_patches[{index}]")
        exact_fields(patch, {"file", "sha256"}, f"{label}.source_patches[{index}]")
        string_value(patch.get("file"), f"{label}.source_patches[{index}].file")
        if not HEX_64.fullmatch(
            string_value(patch.get("sha256"), f"{label}.source_patches[{index}].sha256")
        ):
            fail(f"{label}.source_patches[{index}].sha256: invalid digest")
    commands = list_value(allocator.get("build_commands"), f"{label}.build_commands")
    if not commands:
        fail(f"{label}.build_commands: expected nonempty command list")
    for index, value in enumerate(commands):
        command = list_value(value, f"{label}.build_commands[{index}]")
        if not command:
            fail(f"{label}.build_commands[{index}]: expected nonempty command")
        for argument in command:
            string_value(argument, f"{label}.build_commands[{index}]")
    for argument in list_value(allocator.get("build_flags"), f"{label}.build_flags"):
        string_value(argument, f"{label}.build_flags")
    options = object_value(allocator.get("options"), f"{label}.options")
    option_fields = {
        "pprof_compiled",
        "pprof_runtime",
        "memory_events_compiled",
        "memory_events_runtime",
        "frame_pointers",
        "opt_arch",
        "opt_simd",
    }
    exact_fields(options, option_fields, f"{label}.options")
    for field in options:
        if options[field] not in ("enabled", "disabled", "not-applicable"):
            fail(f"{label}.options.{field}: invalid feature state")
    return allocator


def validate_absolute(record: object, label: str) -> dict[str, object]:
    item = object_value(record, label)
    exact_fields(
        item,
        {"scenario_id", "thread_point", "metric_id", "direction", "allocator_id", "summary"},
        label,
    )
    string_value(item.get("scenario_id"), f"{label}.scenario_id")
    string_value(item.get("thread_point"), f"{label}.thread_point")
    string_value(item.get("metric_id"), f"{label}.metric_id")
    if string_value(item.get("allocator_id"), f"{label}.allocator_id") not in ALLOCATOR_IDS:
        fail(f"{label}.allocator_id: unknown allocator")
    if item.get("direction") not in ("higher-is-better", "lower-is-better"):
        fail(f"{label}.direction: invalid direction")
    summary = object_value(item.get("summary"), f"{label}.summary")
    exact_fields(
        summary,
        {"count", "median", "min", "max", "q1", "q3", "iqr", "relative_iqr", "noisy"},
        f"{label}.summary",
    )
    int_value(summary.get("count"), f"{label}.summary.count", 15)
    for field in ("median", "min", "max", "q1", "q3"):
        float_value(summary.get(field), f"{label}.summary.{field}", positive=True)
    for field in ("iqr", "relative_iqr"):
        if float_value(summary.get(field), f"{label}.summary.{field}") < 0:
            fail(f"{label}.summary.{field}: must be nonnegative")
    if not isinstance(summary.get("noisy"), bool):
        fail(f"{label}.summary.noisy: expected boolean")
    return item


def validate_paired(record: object, label: str) -> dict[str, object]:
    item = object_value(record, label)
    exact_fields(item, {"scenario_id", "thread_point", "metric_id", "summary"}, label)
    for field in ("scenario_id", "thread_point", "metric_id"):
        string_value(item.get(field), f"{label}.{field}")
    summary = object_value(item.get("summary"), f"{label}.summary")
    exact_fields(
        summary,
        {
            "candidate_id",
            "reference_id",
            "direction",
            "block_count",
            "effect",
            "confidence_interval",
            "bootstrap",
            "informational",
        },
        f"{label}.summary",
    )
    candidate = string_value(summary.get("candidate_id"), f"{label}.summary.candidate_id")
    reference = string_value(summary.get("reference_id"), f"{label}.summary.reference_id")
    if candidate not in ALLOCATOR_IDS or reference != "upstream-mimalloc":
        fail(f"{label}.summary: invalid candidate/reference")
    if summary.get("direction") not in ("higher-is-better", "lower-is-better"):
        fail(f"{label}.summary.direction: invalid direction")
    int_value(summary.get("block_count"), f"{label}.summary.block_count", 15)
    float_value(summary.get("effect"), f"{label}.summary.effect", positive=True)
    interval = object_value(
        summary.get("confidence_interval"), f"{label}.summary.confidence_interval"
    )
    exact_fields(
        interval, {"lower", "upper", "confidence_level"}, f"{label}.summary.confidence_interval"
    )
    lower = float_value(interval.get("lower"), f"{label}.summary.confidence_interval.lower", True)
    upper = float_value(interval.get("upper"), f"{label}.summary.confidence_interval.upper", True)
    if (
        lower > upper
        or float_value(
            interval.get("confidence_level"),
            f"{label}.summary.confidence_interval.confidence_level",
        )
        != 0.95
    ):
        fail(f"{label}.summary.confidence_interval: invalid interval")
    bootstrap = object_value(summary.get("bootstrap"), f"{label}.summary.bootstrap")
    exact_fields(
        bootstrap, {"seed", "resample_count", "method", "prng"}, f"{label}.summary.bootstrap"
    )
    int_value(bootstrap.get("seed"), f"{label}.summary.bootstrap.seed")
    if (
        int_value(bootstrap.get("resample_count"), f"{label}.summary.bootstrap.resample_count")
        != 10000
    ):
        fail(f"{label}.summary.bootstrap.resample_count: expected 10000")
    if (
        bootstrap.get("method") != "percentile-block-bootstrap-type7-v1"
        or bootstrap.get("prng") != "splitmix64-rejection-v1"
    ):
        fail(f"{label}.summary.bootstrap: unsupported method")
    if summary.get("informational") is not True:
        fail(f"{label}.summary.informational: must be true")
    return item


def signed_int_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{label}: expected signed integer")
    return value


def validate_memory_environment(value: object, label: str) -> dict[str, object]:
    environment = object_value(value, label)
    exact_fields(
        environment,
        {
            "page_size_bytes",
            "kernel",
            "transparent_hugepage",
            "cgroup_memory_max",
            "cgroup_memory_high",
            "hosted_runner",
            "purge_policy",
            "allocator_runtime_options",
        },
        label,
    )
    page_size = int_value(environment.get("page_size_bytes"), f"{label}.page_size_bytes", 1)
    if page_size & (page_size - 1):
        fail(f"{label}.page_size_bytes: expected a power of two")
    for field in ("kernel", "transparent_hugepage", "cgroup_memory_max", "cgroup_memory_high"):
        string_value(environment.get(field), f"{label}.{field}")
    if not isinstance(environment.get("hosted_runner"), bool):
        fail(f"{label}.hosted_runner: expected boolean")
    if environment.get("purge_policy") != "natural-only":
        fail(f"{label}.purge_policy: allocator-specific purge is forbidden")
    options = object_value(
        environment.get("allocator_runtime_options"), f"{label}.allocator_runtime_options"
    )
    exact_fields(
        options, {"MIMALLOC_MEMORY_EVENTS", "MIMALLOC_PROF"}, f"{label}.allocator_runtime_options"
    )
    if options != {"MIMALLOC_MEMORY_EVENTS": "0", "MIMALLOC_PROF": "0"}:
        fail(f"{label}.allocator_runtime_options: profiler and memory events must be disabled")
    return environment


def validate_memory_sample(value: object, label: str) -> dict[str, object]:
    sample = object_value(value, label)
    exact_fields(sample, MEMORY_SAMPLE_FIELDS, label)
    if sample.get("metric_schema_version") != MEMORY_SCHEMA:
        fail(f"{label}.metric_schema_version: unsupported memory metric")
    allocator = string_value(sample.get("allocator_id"), f"{label}.allocator_id")
    if allocator not in ALLOCATOR_IDS:
        fail(f"{label}.allocator_id: unknown allocator")
    if not HEX_40.fullmatch(
        string_value(sample.get("allocator_source_sha"), f"{label}.allocator_source_sha")
    ):
        fail(f"{label}.allocator_source_sha: invalid digest")
    comparison_digest(sample.get("child_binary_sha256"), f"{label}.child_binary_sha256")
    cell = (
        string_value(sample.get("scenario_id"), f"{label}.scenario_id"),
        string_value(sample.get("thread_point"), f"{label}.thread_point"),
    )
    if cell not in MEMORY_CELLS:
        fail(f"{label}: undeclared memory scenario/thread cell")
    for field in (
        "block_id",
        "workload_seed",
        "thread_count",
        "baseline_ready_ns",
        "workload_active_ns",
        "workload_drained_ns",
        "post_drain_sample_100ms_ns",
        "post_drain_sample_1s_ns",
        "post_drain_sample_5s_ns",
        "sampler_pid",
        "sampled_pid",
        "baseline_rss_bytes",
        "baseline_hwm_bytes",
        "sampled_peak_rss_bytes",
        "kernel_peak_hwm_bytes",
        "peak_live_requested_bytes",
        "post_drain_rss_100ms_bytes",
        "post_drain_rss_1s_bytes",
        "post_drain_rss_5s_bytes",
        "hwm_tolerance_bytes",
    ):
        int_value(
            sample.get(field),
            f"{label}.{field}",
            0 if field in ("block_id", "workload_seed") else 1,
        )
    ordinal = int_value(sample.get("ordinal"), f"{label}.ordinal")
    if ordinal >= len(ALLOCATOR_IDS):
        fail(f"{label}.ordinal: expected 0..{len(ALLOCATOR_IDS) - 1}")
    if sample["sampler_pid"] == sample["sampled_pid"]:
        fail(f"{label}: sampler must target a distinct child PID")
    baseline_ready = cast(int, sample["baseline_ready_ns"])
    active = cast(int, sample["workload_active_ns"])
    drained = cast(int, sample["workload_drained_ns"])
    if not baseline_ready < active < drained:
        fail(f"{label}: control timestamps are out of order")
    for field, target in (
        ("post_drain_sample_100ms_ns", 100_000_000),
        ("post_drain_sample_1s_ns", 1_000_000_000),
        ("post_drain_sample_5s_ns", 5_000_000_000),
    ):
        observed = cast(int, sample[field]) - drained
        if observed < target or observed > target + 100_000_000:
            fail(f"{label}.{field}: outside declared post-drain window")
    baseline_rss = cast(int, sample["baseline_rss_bytes"])
    derived = {
        "sampled_peak_rss_delta_bytes": cast(int, sample["sampled_peak_rss_bytes"]) - baseline_rss,
        "post_drain_rss_delta_100ms_bytes": cast(int, sample["post_drain_rss_100ms_bytes"])
        - baseline_rss,
        "post_drain_rss_delta_1s_bytes": cast(int, sample["post_drain_rss_1s_bytes"])
        - baseline_rss,
        "post_drain_rss_delta_5s_bytes": cast(int, sample["post_drain_rss_5s_bytes"])
        - baseline_rss,
    }
    for field, expected in derived.items():
        if signed_int_value(sample.get(field), f"{label}.{field}") != expected:
            fail(f"{label}.{field}: inconsistent signed delta")
    if derived["sampled_peak_rss_delta_bytes"] <= 0:
        fail(
            f"{label}.sampled_peak_rss_delta_bytes: fragmentation denominator delta must be positive"
        )
    expected_ratio = derived["sampled_peak_rss_delta_bytes"] / cast(
        int, sample["peak_live_requested_bytes"]
    )
    ratio = float_value(sample.get("fragmentation_proxy"), f"{label}.fragmentation_proxy", True)
    if not math.isclose(ratio, expected_ratio, rel_tol=1e-12):
        fail(f"{label}.fragmentation_proxy: inconsistent ratio")
    if not isinstance(sample.get("hwm_discrepancy"), bool):
        fail(f"{label}.hwm_discrepancy: expected boolean")
    hwm_delta = cast(int, sample["kernel_peak_hwm_bytes"]) - cast(int, sample["baseline_hwm_bytes"])
    tolerance = max(
        8 * 1024 * 1024,
        max(derived["sampled_peak_rss_delta_bytes"], hwm_delta, 0) // 5,
    )
    discrepancy = abs(derived["sampled_peak_rss_delta_bytes"] - hwm_delta) > tolerance
    if sample["hwm_tolerance_bytes"] != tolerance or sample["hwm_discrepancy"] != discrepancy:
        fail(f"{label}: inconsistent VmHWM discrepancy flag or tolerance")
    sampling = object_value(sample.get("sampling"), f"{label}.sampling")
    exact_fields(
        sampling,
        {
            "target_interval_ns",
            "sample_count",
            "minimum_interval_ns",
            "median_interval_ns",
            "p95_interval_ns",
            "maximum_interval_ns",
        },
        f"{label}.sampling",
    )
    if (
        int_value(sampling.get("target_interval_ns"), f"{label}.sampling.target_interval_ns")
        != 5_000_000
    ):
        fail(f"{label}.sampling.target_interval_ns: expected 5 ms")
    timeline = list_value(sample.get("timeline"), f"{label}.timeline")
    if int_value(sampling.get("sample_count"), f"{label}.sampling.sample_count", 2) != len(
        timeline
    ):
        fail(f"{label}.sampling.sample_count: does not match timeline")
    timestamps: list[int] = []
    rss_values: list[int] = []
    for index, point_value in enumerate(timeline):
        point = object_value(point_value, f"{label}.timeline[{index}]")
        exact_fields(point, {"elapsed_ns", "rss_bytes"}, f"{label}.timeline[{index}]")
        timestamps.append(
            int_value(point.get("elapsed_ns"), f"{label}.timeline[{index}].elapsed_ns", 1)
        )
        rss_values.append(
            int_value(point.get("rss_bytes"), f"{label}.timeline[{index}].rss_bytes", 1)
        )
    intervals = [right - left for left, right in zip(timestamps, timestamps[1:])]
    if timestamps[0] < active or timestamps[-1] > drained or any(value <= 0 for value in intervals):
        fail(f"{label}.timeline: timestamps must be strictly inside the workload window")
    if timestamps[0] - active > 100_000_000 or drained - timestamps[-1] > 100_000_000:
        fail(f"{label}.timeline: sampler missed a workload edge")
    if max(intervals) > 100_000_000 or sampling.get("maximum_interval_ns") != max(intervals):
        fail(f"{label}.timeline: sampler interval bound/summary mismatch")
    ordered_intervals = sorted(intervals)

    def interval_quantile(numerator: int, denominator: int) -> int:
        return ordered_intervals[(len(ordered_intervals) - 1) * numerator // denominator]

    expected_sampling = {
        "target_interval_ns": 5_000_000,
        "sample_count": len(timeline),
        "minimum_interval_ns": ordered_intervals[0],
        "median_interval_ns": interval_quantile(1, 2),
        "p95_interval_ns": interval_quantile(95, 100),
        "maximum_interval_ns": ordered_intervals[-1],
    }
    if sampling != expected_sampling:
        fail(f"{label}.sampling: interval distribution does not match timeline")
    if max(rss_values) != sample["sampled_peak_rss_bytes"]:
        fail(f"{label}.sampled_peak_rss_bytes: does not match timeline")
    validate_memory_environment(sample.get("environment"), f"{label}.environment")
    child = object_value(sample.get("child_sample"), f"{label}.child_sample")
    exact_fields(child, CHILD_SAMPLE_FIELDS, f"{label}.child_sample")
    if (
        child.get("schema_version") != RAW_SCHEMA
        or child.get("suite_version") != SUITE_VERSION
        or child.get("scenario_version") != SUITE_VERSION
        or child.get("run_kind") != "headline"
        or child.get("execution_mode") != "normal"
        or int_value(child.get("run_seed"), f"{label}.child_sample.run_seed", 1) == 0
        or child.get("timed_out") is not False
        or child.get("crashed") is not False
        or child.get("exit_code") != 0
        or child.get("signal") is not None
    ):
        fail(f"{label}.child_sample: invalid protocol, run mode, or process result")
    for field in (
        "operation_count",
        "requested_transactions",
        "completed_transactions",
        "free_calls",
        "warmup_ns",
        "elapsed_ns",
        "checksum",
        "peak_live_requested_bytes",
    ):
        int_value(child.get(field), f"{label}.child_sample.{field}", 1)
    for field in (
        "allocation_calls",
        "calloc_calls",
        "aligned_allocation_calls",
        "realloc_calls",
        "setup_ns",
        "teardown_ns",
    ):
        int_value(child.get(field), f"{label}.child_sample.{field}")
    if child["completed_transactions"] != child["requested_transactions"]:
        fail(f"{label}.child_sample: incomplete transactions")
    float_value(
        child.get("throughput_operations_per_second"),
        f"{label}.child_sample.throughput_operations_per_second",
        True,
    )
    for field in (
        "allocator_version",
        "operation_unit",
        "reproduction_command",
    ):
        string_value(child.get(field), f"{label}.child_sample.{field}")
    for field, pattern in (
        ("allocator_source_sha", HEX_40),
        ("allocator_library_sha256", HEX_64),
        ("child_binary_sha256", HEX_64),
    ):
        if not pattern.fullmatch(string_value(child.get(field), f"{label}.child_sample.{field}")):
            fail(f"{label}.child_sample.{field}: invalid digest")
    child_runner = object_value(child.get("runner"), f"{label}.child_sample.runner")
    exact_fields(child_runner, CHILD_RUNNER_FIELDS, f"{label}.child_sample.runner")
    for field in ("os", "architecture"):
        string_value(child_runner.get(field), f"{label}.child_sample.runner.{field}")
    for field in ("physical_cores", "logical_cores"):
        int_value(child_runner.get(field), f"{label}.child_sample.runner.{field}", 1)
    child_toolchain = object_value(child.get("toolchain"), f"{label}.child_sample.toolchain")
    exact_fields(child_toolchain, CHILD_TOOLCHAIN_FIELDS, f"{label}.child_sample.toolchain")
    for field in CHILD_TOOLCHAIN_FIELDS:
        string_value(child_toolchain.get(field), f"{label}.child_sample.toolchain.{field}")
    if (
        child.get("allocator_id") != allocator
        or child.get("allocator_source_sha") != sample["allocator_source_sha"]
        or child.get("child_binary_sha256") != sample["child_binary_sha256"]
        or child.get("scenario_id") != cell[0]
        or child.get("thread_point") != cell[1]
        or child.get("thread_count") != sample["thread_count"]
        or child.get("block_id") != sample["block_id"]
        or child.get("ordinal") != sample["ordinal"]
        or child.get("workload_seed") != sample["workload_seed"]
        or child.get("peak_live_requested_bytes") != sample["peak_live_requested_bytes"]
    ):
        fail(f"{label}.child_sample: child workload oracle mismatch")
    return sample


def validate_memory_report(
    value: object, label: str, *, compact: bool = False
) -> dict[str, object]:
    report = object_value(value, label)
    exact_fields(report, MEMORY_HISTORY_FIELDS if compact else MEMORY_REPORT_FIELDS, label)
    if report.get("metric_schema_version") != MEMORY_SCHEMA or report.get("status") != "complete":
        fail(f"{label}: memory report must be complete {MEMORY_SCHEMA}")
    if not compact and report.get("invalid_reason") is not None:
        fail(f"{label}.invalid_reason: complete report requires null")
    comparison_digest(report.get("metric_comparison_key"), f"{label}.metric_comparison_key")
    validate_run(report.get("run"), f"{label}.run")
    runner: dict[str, object] | None = None
    runner_class = None
    if compact:
        comparison_digest(
            report.get("runner_fingerprint_sha256"), f"{label}.runner_fingerprint_sha256"
        )
    else:
        runner = validate_runner(report.get("runner"), f"{label}.runner")
        runner_class = runner["runner_class"]
    if report.get("sampling_target_interval_ns") != 5_000_000:
        fail(f"{label}.sampling_target_interval_ns: expected 5 ms")
    if report.get("purge_policy") != "natural-only":
        fail(f"{label}.purge_policy: explicit purge is forbidden")
    units = object_value(report.get("units"), f"{label}.units")
    exact_fields(units, set(MEMORY_METRICS), f"{label}.units")
    expected_units = {
        metric: "ratio" if metric == "fragmentation-proxy" else "bytes" for metric in MEMORY_METRICS
    }
    if (
        units != expected_units
        or report.get("direction") != "lower-is-better"
        or report.get("informational") is not True
    ):
        fail(f"{label}: invalid units, direction, or hosted-runner interpretation")
    methodology = object_value(report.get("methodology"), f"{label}.methodology")
    exact_fields(methodology, set(MEMORY_METHODOLOGY), f"{label}.methodology")
    if methodology != MEMORY_METHODOLOGY:
        fail(f"{label}.methodology: unsupported Linux process-memory contract")
    absolute = [
        validate_absolute(item, f"{label}.absolute_summaries[{index}]")
        for index, item in enumerate(
            list_value(report.get("absolute_summaries"), f"{label}.absolute_summaries")
        )
    ]
    paired = [
        validate_paired(item, f"{label}.paired_summaries[{index}]")
        for index, item in enumerate(
            list_value(report.get("paired_summaries"), f"{label}.paired_summaries")
        )
    ]
    expected_absolute = {
        (scenario, point, metric, allocator)
        for scenario, point in MEMORY_CELLS
        for metric in MEMORY_METRICS
        for allocator in ALLOCATOR_IDS
    }
    actual_absolute = {
        (item["scenario_id"], item["thread_point"], item["metric_id"], item["allocator_id"])
        for item in absolute
        if item.get("direction") == "lower-is-better"
    }
    expected_paired = {
        (scenario, point, metric, allocator)
        for scenario, point in MEMORY_CELLS
        for metric in MEMORY_METRICS
        for allocator in ALLOCATOR_IDS
        if allocator != "upstream-mimalloc"
    }
    actual_paired = {
        (
            item["scenario_id"],
            item["thread_point"],
            item["metric_id"],
            object_value(item["summary"], "memory paired summary")["candidate_id"],
        )
        for item in paired
        if object_value(item["summary"], "memory paired summary").get("direction")
        == "lower-is-better"
    }
    if (
        len(absolute) != len(expected_absolute)
        or len(paired) != len(expected_paired)
        or actual_absolute != expected_absolute
        or actual_paired != expected_paired
        or any(item.get("direction") != "lower-is-better" for item in absolute)
        or any(
            object_value(item["summary"], "memory paired summary").get("direction")
            != "lower-is-better"
            for item in paired
        )
    ):
        fail(f"{label}: incomplete or duplicate memory summary matrix")
    if compact:
        return report
    raw = [
        validate_memory_sample(item, f"{label}.raw_samples[{index}]")
        for index, item in enumerate(list_value(report.get("raw_samples"), f"{label}.raw_samples"))
    ]
    groups: dict[tuple[object, object, object], list[dict[str, object]]] = {}
    first_environment: dict[str, object] | None = None
    run_seeds: set[object] = set()
    allocator_identities: dict[object, tuple[object, ...]] = {}
    for sample in raw:
        groups.setdefault(
            (sample["scenario_id"], sample["thread_point"], sample["block_id"]), []
        ).append(sample)
        environment = object_value(sample["environment"], "memory environment")
        if first_environment is None:
            first_environment = environment
        elif environment != first_environment:
            fail(f"{label}: mixed memory environments are not comparable")
        if runner_class is not None and environment["hosted_runner"] != (
            runner_class == "github-hosted"
        ):
            fail(f"{label}: hosted-runner label disagrees with runner metadata")
        child = object_value(sample["child_sample"], "memory child sample")
        run_seeds.add(child["run_seed"])
        child_runner = object_value(child["runner"], "memory child runner")
        if runner is not None and child_runner != {
            "os": runner["os"],
            "architecture": runner["architecture"],
            "physical_cores": runner["physical_cores"],
            "logical_cores": runner["logical_cores"],
        }:
            fail(f"{label}: child runner contradicts memory runner metadata")
        identity = (
            child["allocator_version"],
            child["allocator_source_sha"],
            child["allocator_library_sha256"],
            child["child_binary_sha256"],
        )
        prior_identity = allocator_identities.setdefault(sample["allocator_id"], identity)
        if prior_identity != identity:
            fail(f"{label}: allocator identity changed within the memory run")
    if len(run_seeds) != 1 or set(allocator_identities) != set(ALLOCATOR_IDS):
        fail(f"{label}: memory run seed or allocator provenance is incomplete")
    blocks_by_cell: dict[tuple[object, object], set[object]] = {}
    for (scenario, point, block), samples in groups.items():
        ids = {sample["allocator_id"] for sample in samples}
        ordinals = {sample["ordinal"] for sample in samples}
        seeds = {sample["workload_seed"] for sample in samples}
        if (
            len(samples) != len(ALLOCATOR_IDS)
            or ids != set(ALLOCATOR_IDS)
            or ordinals != set(range(len(ALLOCATOR_IDS)))
            or len(seeds) != 1
        ):
            fail(f"{label}: incomplete memory pair for {scenario}/{point}/block-{block}")
        first_child = object_value(samples[0]["child_sample"], "memory paired child")
        for sample in samples[1:]:
            child = object_value(sample["child_sample"], "memory paired child")
            if any(child[field] != first_child[field] for field in CHILD_BLOCK_IDENTITY_FIELDS):
                fail(f"{label}: mismatched workload identity within a complete memory pair")
        blocks_by_cell.setdefault((scenario, point), set()).add(block)
    if set(blocks_by_cell) != set(MEMORY_CELLS) or any(
        len(blocks) < 15 for blocks in blocks_by_cell.values()
    ):
        fail(f"{label}: every memory cell requires at least 15 complete paired blocks")
    return report


def validate_latency_distribution(value: object, label: str) -> dict[str, object]:
    distribution = object_value(value, label)
    exact_fields(
        distribution,
        {
            "count",
            "p50_ns",
            "p95_ns",
            "p99_ns",
            "min_ns",
            "max_ns",
            "median_absolute_deviation_ns",
            "iqr_ns",
            "zero_count",
        },
        label,
    )
    count = int_value(distribution.get("count"), f"{label}.count", 1)
    p50 = float_value(distribution.get("p50_ns"), f"{label}.p50_ns", True)
    p95 = float_value(distribution.get("p95_ns"), f"{label}.p95_ns", True)
    p99 = float_value(distribution.get("p99_ns"), f"{label}.p99_ns", True)
    minimum = int_value(distribution.get("min_ns"), f"{label}.min_ns", 1)
    maximum = int_value(distribution.get("max_ns"), f"{label}.max_ns", 1)
    float_value(
        distribution.get("median_absolute_deviation_ns"),
        f"{label}.median_absolute_deviation_ns",
    )
    float_value(distribution.get("iqr_ns"), f"{label}.iqr_ns")
    if (
        distribution.get("zero_count") != 0
        or not minimum <= p50 <= p95 <= p99 <= maximum
        or count < 1
    ):
        fail(f"{label}: invalid latency order statistics or zero duration")
    return distribution


def latency_type7(values: Sequence[int | float], probability: float) -> float:
    if not values or not 0.0 <= probability <= 1.0:
        fail("latency Type-7 input is empty or has an invalid probability")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def summarize_latency_values(values: Sequence[int]) -> dict[str, object]:
    if not values or any(value <= 0 for value in values):
        fail("latency raw distribution is empty or contains a non-positive duration")
    p50 = latency_type7(values, 0.50)
    q1 = latency_type7(values, 0.25)
    q3 = latency_type7(values, 0.75)
    return {
        "count": len(values),
        "p50_ns": p50,
        "p95_ns": latency_type7(values, 0.95),
        "p99_ns": latency_type7(values, 0.99),
        "min_ns": min(values),
        "max_ns": max(values),
        "median_absolute_deviation_ns": latency_type7(
            [abs(float(value) - p50) for value in values], 0.50
        ),
        "iqr_ns": q3 - q1,
        "zero_count": 0,
    }


def validate_latency_scheduling(value: object, label: str) -> dict[str, object]:
    scheduling = object_value(value, label)
    exact_fields(
        scheduling,
        {
            "affinity_policy",
            "actual_cpu_ids",
            "thread_count",
            "physical_cores",
            "logical_cores",
            "context_switches",
            "runner_class",
            "clock",
        },
        label,
    )
    string_value(scheduling.get("affinity_policy"), f"{label}.affinity_policy")
    string_value(scheduling.get("runner_class"), f"{label}.runner_class")
    thread_count = int_value(scheduling.get("thread_count"), f"{label}.thread_count", 1)
    physical = int_value(scheduling.get("physical_cores"), f"{label}.physical_cores", 1)
    logical = int_value(scheduling.get("logical_cores"), f"{label}.logical_cores", 1)
    if physical > logical:
        fail(f"{label}: physical cores exceed logical cores")
    cpu_ids = list_value(scheduling.get("actual_cpu_ids"), f"{label}.actual_cpu_ids")
    if len(cpu_ids) != thread_count:
        fail(f"{label}.actual_cpu_ids: expected one observation per worker")
    for index, cpu in enumerate(cpu_ids):
        if cpu is not None:
            int_value(cpu, f"{label}.actual_cpu_ids[{index}]")
    switches = object_value(scheduling.get("context_switches"), f"{label}.context_switches")
    exact_fields(switches, {"voluntary", "involuntary"}, f"{label}.context_switches")
    int_value(switches.get("voluntary"), f"{label}.context_switches.voluntary")
    int_value(switches.get("involuntary"), f"{label}.context_switches.involuntary")
    clock = object_value(scheduling.get("clock"), f"{label}.clock")
    exact_fields(clock, {"source", "implementation", "resolution_ns"}, f"{label}.clock")
    if clock.get("source") != "monotonic":
        fail(f"{label}.clock.source: monotonic clock required")
    string_value(clock.get("implementation"), f"{label}.clock.implementation")
    int_value(clock.get("resolution_ns"), f"{label}.clock.resolution_ns", 1)
    return scheduling


def validate_latency_child(value: object, label: str, control: bool) -> dict[str, object]:
    child = object_value(value, label)
    exact_fields(
        child,
        {
            "protocol_version",
            "metric_schema_version",
            "control",
            "completed_transactions",
            "checksum",
            "observations",
            "scheduling",
        },
        label,
    )
    if (
        child.get("protocol_version") != LATENCY_CHILD_PROTOCOL
        or child.get("metric_schema_version") != LATENCY_SCHEMA
        or child.get("control") is not control
    ):
        fail(f"{label}: latency child identity/control mismatch")
    int_value(child.get("completed_transactions"), f"{label}.completed_transactions", 1)
    int_value(child.get("checksum"), f"{label}.checksum", 1)
    observations = list_value(child.get("observations"), f"{label}.observations")
    if not observations:
        fail(f"{label}.observations: empty")
    previous: tuple[int, int] | None = None
    for index, value in enumerate(observations):
        item = object_value(value, f"{label}.observations[{index}]")
        exact_fields(
            item,
            {"thread_index", "transaction_index", "duration_ns"},
            f"{label}.observations[{index}]",
        )
        key = (
            int_value(item.get("thread_index"), f"{label}.observations[{index}].thread_index"),
            int_value(
                item.get("transaction_index"),
                f"{label}.observations[{index}].transaction_index",
            ),
        )
        int_value(item.get("duration_ns"), f"{label}.observations[{index}].duration_ns", 1)
        if previous is not None and key <= previous:
            fail(f"{label}.observations: schedule must be strictly ordered")
        previous = key
    validate_latency_scheduling(child.get("scheduling"), f"{label}.scheduling")
    return child


def latency_schedule(child: Mapping[str, object], label: str) -> list[tuple[int, int]]:
    return [
        (
            cast(int, object_value(value, label)["thread_index"]),
            cast(int, object_value(value, label)["transaction_index"]),
        )
        for value in list_value(child["observations"], label)
    ]


def validate_latency_paired(value: object, label: str) -> dict[str, object]:
    item = object_value(value, label)
    exact_fields(item, {"scenario_id", "thread_point", "quantile", "summary"}, label)
    if (item.get("scenario_id"), item.get("thread_point")) not in LATENCY_CELLS:
        fail(f"{label}: undeclared latency cell")
    if item.get("quantile") not in LATENCY_QUANTILES:
        fail(f"{label}.quantile: unsupported")
    summary = object_value(item.get("summary"), f"{label}.summary")
    exact_fields(
        summary,
        {
            "candidate_id",
            "reference_id",
            "direction",
            "block_count",
            "effect",
            "confidence_interval",
            "bootstrap",
            "informational",
        },
        f"{label}.summary",
    )
    if (
        summary.get("candidate_id") not in set(ALLOCATOR_IDS) - {"upstream-mimalloc"}
        or summary.get("reference_id") != "upstream-mimalloc"
        or summary.get("direction") != "lower-is-better"
        or summary.get("informational") is not True
    ):
        fail(f"{label}.summary: invalid paired latency identity")
    int_value(summary.get("block_count"), f"{label}.summary.block_count", 15)
    float_value(summary.get("effect"), f"{label}.summary.effect", True)
    interval = object_value(summary.get("confidence_interval"), f"{label}.summary.interval")
    exact_fields(interval, {"lower", "upper", "confidence_level"}, f"{label}.summary.interval")
    lower = float_value(interval.get("lower"), f"{label}.summary.interval.lower", True)
    upper = float_value(interval.get("upper"), f"{label}.summary.interval.upper", True)
    if interval.get("confidence_level") != 0.95 or lower > upper:
        fail(f"{label}.summary.interval: invalid")
    bootstrap = object_value(summary.get("bootstrap"), f"{label}.summary.bootstrap")
    exact_fields(
        bootstrap, {"seed", "resample_count", "method", "prng"}, f"{label}.summary.bootstrap"
    )
    if (
        int_value(bootstrap.get("seed"), f"{label}.summary.bootstrap.seed") < 0
        or bootstrap.get("resample_count") != 10_000
        or bootstrap.get("method") != "percentile-whole-block-transaction-quantile-type7-v1"
        or bootstrap.get("prng") != "splitmix64-rejection-v1"
    ):
        fail(f"{label}.summary.bootstrap: unsupported block bootstrap")
    return item


def validate_latency_report(
    value: object, label: str, *, compact: bool = False
) -> dict[str, object]:
    report = object_value(value, label)
    required = LATENCY_REPORT_FIELDS - (
        {"invalid_reason", "runner", "raw_samples"} if compact else set()
    )
    if compact:
        required.add("runner_fingerprint_sha256")
    exact_fields(report, required, label)
    if (
        report.get("metric_schema_version") != LATENCY_SCHEMA
        or report.get("status") != "complete"
        or report.get("direction") != "lower-is-better"
        or report.get("informational") is not True
    ):
        fail(f"{label}: complete informational lower-is-better latency required")
    comparison_digest(report.get("metric_comparison_key"), f"{label}.metric_comparison_key")
    validate_run(report.get("run"), f"{label}.run")
    runner: dict[str, object] | None = None
    if compact:
        digest = string_value(
            report.get("runner_fingerprint_sha256"), f"{label}.runner_fingerprint_sha256"
        )
        if not HEX_64.fullmatch(digest):
            fail(f"{label}.runner_fingerprint_sha256: invalid")
    else:
        if report.get("invalid_reason") is not None:
            fail(f"{label}.invalid_reason: must be null")
        runner = validate_runner(report.get("runner"), f"{label}.runner")
    rates = object_value(report.get("sampling_denominators"), f"{label}.sampling_denominators")
    if set(rates) != {f"{scenario}/{point}" for scenario, point in LATENCY_CELLS}:
        fail(f"{label}.sampling_denominators: exact latency cell matrix required")
    for cell, rate in rates.items():
        denominator = int_value(rate, f"{label}.sampling_denominators.{cell}", 1)
        if denominator > 1024:
            fail(f"{label}.sampling_denominators.{cell}: exceeds protocol maximum")
    methodology = object_value(report.get("methodology"), f"{label}.methodology")
    exact_fields(
        methodology,
        {
            "transaction_boundaries",
            "quantile_method",
            "sampling_schedule",
            "overhead_control",
            "bootstrap",
            "storage_decision",
            "tail_policy",
        },
        f"{label}.methodology",
    )
    boundaries = object_value(
        methodology.get("transaction_boundaries"), f"{label}.methodology.transaction_boundaries"
    )
    exact_fields(
        boundaries,
        {"local", "cross-thread", "large-object"},
        f"{label}.methodology.transaction_boundaries",
    )
    for field in (
        "quantile_method",
        "sampling_schedule",
        "overhead_control",
        "bootstrap",
        "storage_decision",
        "tail_policy",
    ):
        string_value(methodology.get(field), f"{label}.methodology.{field}")
    for key in boundaries:
        definition = string_value(
            boundaries[key], f"{label}.methodology.transaction_boundaries.{key}"
        )
        if "free" not in definition or "allocation" not in definition:
            fail(f"{label}.methodology: transaction boundary must include allocation through free")

    absolute = list_value(report.get("absolute_summaries"), f"{label}.absolute_summaries")
    if len(absolute) != len(LATENCY_CELLS) * len(ALLOCATOR_IDS):
        fail(
            f"{label}.absolute_summaries: expected exact "
            f"{len(LATENCY_CELLS)}x{len(ALLOCATOR_IDS)} matrix"
        )
    absolute_keys: set[tuple[str, str, str]] = set()
    for index, value in enumerate(absolute):
        item = object_value(value, f"{label}.absolute_summaries[{index}]")
        exact_fields(
            item,
            {
                "allocator_id",
                "scenario_id",
                "thread_point",
                "transaction_definition",
                "measured",
                "control",
                "overhead_valid",
            },
            f"{label}.absolute_summaries[{index}]",
        )
        key = (
            cast(str, item.get("scenario_id")),
            cast(str, item.get("thread_point")),
            cast(str, item.get("allocator_id")),
        )
        if key[:2] not in LATENCY_CELLS or key[2] not in ALLOCATOR_IDS or key in absolute_keys:
            fail(f"{label}.absolute_summaries[{index}]: duplicate or undeclared cell")
        absolute_keys.add(key)
        definition = string_value(
            item.get("transaction_definition"),
            f"{label}.absolute_summaries[{index}].transaction_definition",
        )
        if (
            "allocation" not in definition
            or "free" not in definition
            or "allocator-call" in definition
        ):
            fail(f"{label}.absolute_summaries[{index}]: dishonest transaction label")
        measured = validate_latency_distribution(
            item.get("measured"), f"{label}.absolute_summaries[{index}].measured"
        )
        control = validate_latency_distribution(
            item.get("control"), f"{label}.absolute_summaries[{index}].control"
        )
        if (
            int_value(measured["count"], "latency measured count") < 10_000
            or int_value(control["count"], "latency control count") < 10_000
            or item.get("overhead_valid") is not True
            or cast(float, control["p50_ns"]) > cast(float, measured["p50_ns"]) * 0.05
            or cast(float, measured["p99_ns"]) <= cast(float, control["p99_ns"]) * 2.0
        ):
            fail(f"{label}.absolute_summaries[{index}]: control/sample threshold failed")

    paired = list_value(report.get("paired_summaries"), f"{label}.paired_summaries")
    if len(paired) != len(LATENCY_CELLS) * (len(ALLOCATOR_IDS) - 1) * len(LATENCY_QUANTILES):
        fail(f"{label}.paired_summaries: expected exact quantile matrix")
    paired_keys: set[tuple[str, str, str, str]] = set()
    for index, value in enumerate(paired):
        item = validate_latency_paired(value, f"{label}.paired_summaries[{index}]")
        summary = object_value(item["summary"], f"{label}.paired_summaries[{index}].summary")
        key = (
            cast(str, item["scenario_id"]),
            cast(str, item["thread_point"]),
            cast(str, item["quantile"]),
            cast(str, summary["candidate_id"]),
        )
        if key in paired_keys:
            fail(f"{label}.paired_summaries[{index}]: duplicate")
        paired_keys.add(key)

    blocks = list_value(report.get("block_summaries"), f"{label}.block_summaries")
    if len(blocks) < 300:
        fail(f"{label}.block_summaries: at least 15 blocks per allocator/cell required")
    block_keys: set[tuple[str, str, str, int]] = set()
    for index, value in enumerate(blocks):
        item = object_value(value, f"{label}.block_summaries[{index}]")
        exact_fields(
            item,
            {"block_id", "allocator_id", "scenario_id", "thread_point", "measured", "control"},
            f"{label}.block_summaries[{index}]",
        )
        key = (
            cast(str, item.get("scenario_id")),
            cast(str, item.get("thread_point")),
            cast(str, item.get("allocator_id")),
            int_value(item.get("block_id"), f"{label}.block_summaries[{index}].block_id"),
        )
        if key[:2] not in LATENCY_CELLS or key[2] not in ALLOCATOR_IDS or key in block_keys:
            fail(f"{label}.block_summaries[{index}]: duplicate or undeclared")
        block_keys.add(key)
        validate_latency_distribution(
            item.get("measured"), f"{label}.block_summaries[{index}].measured"
        )
        validate_latency_distribution(
            item.get("control"), f"{label}.block_summaries[{index}].control"
        )
    for scenario, point in LATENCY_CELLS:
        for allocator in ALLOCATOR_IDS:
            if sum(1 for key in block_keys if key[:3] == (scenario, point, allocator)) < 15:
                fail(f"{label}: incomplete block matrix for {scenario}/{point}/{allocator}")

    if compact:
        return report
    raw = list_value(report.get("raw_samples"), f"{label}.raw_samples")
    if len(raw) < 300:
        fail(f"{label}.raw_samples: incomplete")
    raw_groups: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    raw_blocks: dict[tuple[str, str, int], list[dict[str, object]]] = {}
    paired_schedules: dict[tuple[str, str, int], tuple[int, list[tuple[int, int]]]] = {}
    allocator_identities: dict[str, tuple[object, object]] = {}
    common_clock: object | None = None
    common_block_ids: set[int] | None = None
    for index, value in enumerate(raw):
        item = object_value(value, f"{label}.raw_samples[{index}]")
        exact_fields(
            item,
            {
                "metric_schema_version",
                "block_id",
                "ordinal",
                "workload_seed",
                "allocator_id",
                "allocator_source_sha",
                "child_binary_sha256",
                "scenario_id",
                "thread_point",
                "thread_count",
                "sample_denominator",
                "transaction_definition",
                "measured",
                "control",
            },
            f"{label}.raw_samples[{index}]",
        )
        if item.get("metric_schema_version") != LATENCY_SCHEMA:
            fail(f"{label}.raw_samples[{index}]: schema mismatch")
        scenario = string_value(
            item.get("scenario_id"), f"{label}.raw_samples[{index}].scenario_id"
        )
        point = string_value(item.get("thread_point"), f"{label}.raw_samples[{index}].thread_point")
        allocator = string_value(
            item.get("allocator_id"), f"{label}.raw_samples[{index}].allocator_id"
        )
        if (scenario, point) not in LATENCY_CELLS or allocator not in ALLOCATOR_IDS:
            fail(f"{label}.raw_samples[{index}]: undeclared cell or allocator")
        if not HEX_40.fullmatch(
            string_value(
                item.get("allocator_source_sha"),
                f"{label}.raw_samples[{index}].allocator_source_sha",
            )
        ):
            fail(f"{label}.raw_samples[{index}].allocator_source_sha: invalid")
        comparison_digest(
            item.get("child_binary_sha256"), f"{label}.raw_samples[{index}].child_binary_sha256"
        )
        block = int_value(item.get("block_id"), f"{label}.raw_samples[{index}].block_id")
        ordinal = int_value(item.get("ordinal"), f"{label}.raw_samples[{index}].ordinal")
        if ordinal >= len(ALLOCATOR_IDS):
            fail(f"{label}.raw_samples[{index}].ordinal: invalid")
        seed = int_value(
            item.get("workload_seed"), f"{label}.raw_samples[{index}].workload_seed", 1
        )
        denominator = int_value(
            item.get("sample_denominator"), f"{label}.raw_samples[{index}].sample_denominator", 1
        )
        if denominator != rates[f"{scenario}/{point}"]:
            fail(f"{label}.raw_samples[{index}]: sample rate differs from comparison key input")
        definition = string_value(
            item.get("transaction_definition"),
            f"{label}.raw_samples[{index}].transaction_definition",
        )
        if (
            "allocation" not in definition
            or "free" not in definition
            or "allocator-call" in definition
        ):
            fail(f"{label}.raw_samples[{index}]: invalid transaction definition")
        measured = validate_latency_child(
            item.get("measured"), f"{label}.raw_samples[{index}].measured", False
        )
        control = validate_latency_child(
            item.get("control"), f"{label}.raw_samples[{index}].control", True
        )
        measured_schedule = latency_schedule(measured, f"{label}.raw_samples[{index}].measured")
        if measured_schedule != latency_schedule(control, f"{label}.raw_samples[{index}].control"):
            fail(f"{label}.raw_samples[{index}]: measured/control schedules differ")
        measured_scheduling = object_value(measured["scheduling"], "latency measured scheduling")
        control_scheduling = object_value(control["scheduling"], "latency control scheduling")
        for field in (
            "affinity_policy",
            "thread_count",
            "physical_cores",
            "logical_cores",
            "clock",
        ):
            if measured_scheduling[field] != control_scheduling[field]:
                fail(f"{label}.raw_samples[{index}]: measured/control scheduling differs")
        thread_count = int_value(
            item.get("thread_count"), f"{label}.raw_samples[{index}].thread_count", 1
        )
        expected_threads = (
            1
            if point == "1"
            else cast(int, runner["physical_cores"])
            if runner is not None
            else thread_count
        )
        completed = int_value(
            measured.get("completed_transactions"),
            f"{label}.raw_samples[{index}].measured.completed_transactions",
            1,
        )
        if (
            control.get("completed_transactions") != completed
            or completed % thread_count != 0
            or thread_count != expected_threads
            or measured_scheduling["thread_count"] != thread_count
            or control.get("checksum") != 1
            or measured.get("checksum") == 1
            or any(
                worker >= thread_count or transaction >= completed // thread_count
                for worker, transaction in measured_schedule
            )
        ):
            fail(f"{label}.raw_samples[{index}]: transaction count/checksum/topology mismatch")
        if runner is not None:
            affinity = object_value(runner["affinity"], f"{label}.runner.affinity")
            if (
                measured_scheduling["physical_cores"] != runner["physical_cores"]
                or measured_scheduling["logical_cores"] != runner["logical_cores"]
                or measured_scheduling["runner_class"] != runner["runner_class"]
                or control_scheduling["runner_class"] != runner["runner_class"]
                or measured_scheduling["affinity_policy"] != affinity["policy"]
            ):
                fail(f"{label}.raw_samples[{index}]: scheduling contradicts report runner")
            if affinity["policy"] == "pinned":
                allowed = set(cast(list[int], affinity["logical_cpu_ids"]))
                observed = list_value(
                    measured_scheduling["actual_cpu_ids"], "latency measured CPU IDs"
                ) + list_value(control_scheduling["actual_cpu_ids"], "latency control CPU IDs")
                if any(cpu is None or cpu not in allowed for cpu in observed):
                    fail(f"{label}.raw_samples[{index}]: pinned CPU assignment was not observed")
        clock = measured_scheduling["clock"]
        if common_clock is None:
            common_clock = clock
        elif clock != common_clock:
            fail(f"{label}.raw_samples[{index}]: mixed monotonic clocks")
        identity = (item["allocator_source_sha"], item["child_binary_sha256"])
        prior_identity = allocator_identities.setdefault(allocator, identity)
        if identity != prior_identity:
            fail(f"{label}.raw_samples[{index}]: allocator provenance changed within run")
        pair_key = (scenario, point, block)
        if pair_key in paired_schedules:
            expected_seed, expected_schedule = paired_schedules[pair_key]
            if seed != expected_seed or measured_schedule != expected_schedule:
                fail(
                    f"{label}.raw_samples[{index}]: allocator sample schedule differs within block"
                )
        else:
            paired_schedules[pair_key] = (seed, measured_schedule)
        raw_groups.setdefault((scenario, point, allocator), []).append(item)
        raw_blocks.setdefault(pair_key, []).append(item)
    for scenario, point in LATENCY_CELLS:
        cell_block_ids: set[int] | None = None
        for allocator in ALLOCATOR_IDS:
            group = raw_groups.get((scenario, point, allocator), [])
            block_ids = {cast(int, item["block_id"]) for item in group}
            if len(group) < 15 or len(block_ids) != len(group):
                fail(f"{label}: incomplete/duplicate raw blocks for {scenario}/{point}/{allocator}")
            if cell_block_ids is None:
                cell_block_ids = block_ids
            elif block_ids != cell_block_ids:
                fail(f"{label}: allocators do not share an exact raw block set")
            for child_name in ("measured", "control"):
                count = sum(
                    len(
                        list_value(
                            object_value(item[child_name], "latency child")["observations"],
                            "latency observations",
                        )
                    )
                    for item in group
                )
                if count < 10_000:
                    fail(
                        f"{label}: {scenario}/{point}/{allocator}/{child_name} has fewer than 10000 samples"
                    )
        if cell_block_ids is None:
            fail(f"{label}: latency cell contains no blocks")
        if common_block_ids is None:
            common_block_ids = cell_block_ids
        elif cell_block_ids != common_block_ids:
            fail(f"{label}: latency cells do not share an exact raw block set")
    if common_block_ids is None or common_block_ids != set(range(len(common_block_ids))):
        fail(f"{label}: latency block IDs must be contiguous from zero")
    if len(raw) != len(LATENCY_CELLS) * len(ALLOCATOR_IDS) * len(common_block_ids):
        fail(f"{label}: raw latency matrix contains missing or extra records")
    for (scenario, point, block_id), group in raw_blocks.items():
        if (
            len(group) != len(ALLOCATOR_IDS)
            or {item["allocator_id"] for item in group} != set(ALLOCATOR_IDS)
            or {item["ordinal"] for item in group} != set(range(len(ALLOCATOR_IDS)))
            or len({item["workload_seed"] for item in group}) != 1
        ):
            fail(f"{label}: incomplete paired raw block {scenario}/{point}/{block_id}")

    raw_keys = {
        (
            cast(str, item["scenario_id"]),
            cast(str, item["thread_point"]),
            cast(str, item["allocator_id"]),
            cast(int, item["block_id"]),
        )
        for item in cast(list[dict[str, object]], raw)
    }
    if raw_keys != block_keys or len(blocks) != len(raw):
        fail(f"{label}: block summaries do not exactly cover raw latency records")
    block_by_key = {
        (
            cast(str, object_value(item, "latency block")["scenario_id"]),
            cast(str, object_value(item, "latency block")["thread_point"]),
            cast(str, object_value(item, "latency block")["allocator_id"]),
            cast(int, object_value(item, "latency block")["block_id"]),
        ): object_value(item, "latency block")
        for item in blocks
    }
    absolute_by_key = {
        (
            cast(str, object_value(item, "latency absolute")["scenario_id"]),
            cast(str, object_value(item, "latency absolute")["thread_point"]),
            cast(str, object_value(item, "latency absolute")["allocator_id"]),
        ): object_value(item, "latency absolute")
        for item in absolute
    }
    for group_key, group in raw_groups.items():
        measured_values: list[int] = []
        control_values: list[int] = []
        for item in group:
            measured = object_value(item["measured"], "latency measured")
            control = object_value(item["control"], "latency control")
            measured_block = [
                cast(int, object_value(value, "latency observation")["duration_ns"])
                for value in list_value(measured["observations"], "latency observations")
            ]
            control_block = [
                cast(int, object_value(value, "latency observation")["duration_ns"])
                for value in list_value(control["observations"], "latency observations")
            ]
            measured_values.extend(measured_block)
            control_values.extend(control_block)
            key = (*group_key, cast(int, item["block_id"]))
            block_summary = block_by_key[key]
            if block_summary["measured"] != summarize_latency_values(
                measured_block
            ) or block_summary["control"] != summarize_latency_values(control_block):
                fail(f"{label}: latency block summary differs from raw durations")
        absolute_summary = absolute_by_key[group_key]
        if absolute_summary["measured"] != summarize_latency_values(
            measured_values
        ) or absolute_summary["control"] != summarize_latency_values(control_values):
            fail(f"{label}: latency absolute summary differs from raw durations")
    return report


def validate_latest(latest: dict[str, object], label: str) -> None:
    exact_fields_with_optional(latest, TOP_LEVEL_FIELDS, OPTIONAL_TOP_LEVEL_FIELDS, label)
    versions = {
        "latest_schema_version": LATEST_SCHEMA,
        "raw_schema_version": RAW_SCHEMA,
        "statistics_version": STATISTICS_VERSION,
        "suite_version": SUITE_VERSION,
    }
    for field, expected in versions.items():
        if latest.get(field) != expected:
            fail(f"{label}.{field}: expected {expected!r}")
    report = object_value(latest.get("validation_report"), f"{label}.validation_report")
    exact_fields(
        report,
        {
            "validator_version",
            "status",
            "headline_eligible",
            "sample_count",
            "cell_count",
            "minimum_blocks_per_cell",
            "allocator_ids",
            "checks",
            "errors",
        },
        f"{label}.validation_report",
    )
    if (
        report.get("validator_version") != VALIDATOR_VERSION
        or report.get("status") != "valid"
        or report.get("headline_eligible") is not True
    ):
        fail(f"{label}.validation_report: Rust validator approval is required")
    samples = list_value(latest.get("raw_samples"), f"{label}.raw_samples")
    if int_value(
        report.get("sample_count"), f"{label}.validation_report.sample_count", 1800
    ) != len(samples):
        fail(f"{label}.validation_report.sample_count: does not match raw samples")
    if int_value(report.get("cell_count"), f"{label}.validation_report.cell_count") != 30:
        fail(f"{label}.validation_report.cell_count: expected 30")
    if (
        int_value(
            report.get("minimum_blocks_per_cell"),
            f"{label}.validation_report.minimum_blocks_per_cell",
            15,
        )
        != 15
    ):
        fail(f"{label}.validation_report.minimum_blocks_per_cell: expected 15")
    ids = tuple(
        string_value(value, f"{label}.validation_report.allocator_ids")
        for value in list_value(
            report.get("allocator_ids"), f"{label}.validation_report.allocator_ids"
        )
    )
    if ids != ALLOCATOR_IDS:
        fail(f"{label}.validation_report.allocator_ids: expected exact headline allocators")
    checks = [
        string_value(value, f"{label}.validation_report.checks")
        for value in list_value(report.get("checks"), f"{label}.validation_report.checks")
    ]
    if tuple(checks) != VALIDATION_CHECKS or list_value(
        report.get("errors"), f"{label}.validation_report.errors"
    ):
        fail(f"{label}.validation_report: exact checks and empty errors required")
    validate_run(latest.get("run"), f"{label}.run")
    validate_runner(latest.get("runner"), f"{label}.runner")
    allocators = list_value(latest.get("allocators"), f"{label}.allocators")
    if len(allocators) != len(ALLOCATOR_IDS):
        fail(f"{label}.allocators: expected exactly five")
    for index, allocator_id in enumerate(ALLOCATOR_IDS):
        validate_allocator_build(allocators[index], f"{label}.allocators[{index}]", allocator_id)
    calibrations = list_value(latest.get("calibrations"), f"{label}.calibrations")
    if len(calibrations) != 30 or len(samples) < 1800:
        fail(f"{label}: complete calibrations and raw samples are required")
    for index, value in enumerate(calibrations):
        calibration = object_value(value, f"{label}.calibrations[{index}]")
        exact_fields(
            calibration,
            {
                "scenario_id",
                "thread_point",
                "thread_count",
                "transactions_per_worker",
                "warmup_transactions_per_worker",
                "operation_count",
                "elapsed_ns",
            },
            f"{label}.calibrations[{index}]",
        )
        string_value(calibration.get("scenario_id"), f"{label}.calibrations[{index}].scenario_id")
        if calibration.get("thread_point") not in ("1", "2", "physical-core", "2x-logical"):
            fail(f"{label}.calibrations[{index}].thread_point: invalid")
        for field in (
            "thread_count",
            "transactions_per_worker",
            "warmup_transactions_per_worker",
            "operation_count",
        ):
            int_value(calibration.get(field), f"{label}.calibrations[{index}].{field}", 1)
        elapsed = int_value(
            calibration.get("elapsed_ns"), f"{label}.calibrations[{index}].elapsed_ns", 500_000_000
        )
        if elapsed > 2_000_000_000:
            fail(f"{label}.calibrations[{index}].elapsed_ns: exceeds protocol maximum")
    for index, value in enumerate(samples):
        object_value(value, f"{label}.raw_samples[{index}]")
    absolute = list_value(latest.get("absolute_summaries"), f"{label}.absolute_summaries")
    paired = list_value(latest.get("paired_summaries"), f"{label}.paired_summaries")
    if len(absolute) < 120 or len(paired) < 90:
        fail(f"{label}: aggregate-only or incomplete summary input refused")
    for index, value in enumerate(absolute):
        validate_absolute(value, f"{label}.absolute_summaries[{index}]")
    for index, value in enumerate(paired):
        validate_paired(value, f"{label}.paired_summaries[{index}]")
    comparison_digest(latest.get("comparison_key"), f"{label}.comparison_key")
    methodology = object_value(latest.get("methodology"), f"{label}.methodology")
    exact_fields(
        methodology,
        {
            "absolute_summary",
            "paired_effect",
            "confidence_interval",
            "quantile_method",
            "noise_threshold_relative_iqr",
            "informational",
        },
        f"{label}.methodology",
    )
    for field in ("absolute_summary", "paired_effect", "confidence_interval", "quantile_method"):
        string_value(methodology.get(field), f"{label}.methodology.{field}")
    if (
        float_value(
            methodology.get("noise_threshold_relative_iqr"),
            f"{label}.methodology.noise_threshold_relative_iqr",
        )
        != 0.1
        or methodology.get("informational") is not True
    ):
        fail(f"{label}.methodology: unsupported methodology contract")
    memory = latest.get("memory")
    if memory is not None:
        memory_report = validate_memory_report(memory, f"{label}.memory")
        expected_sources = {
            object_value(value, f"{label}.allocators")["allocator_id"]: object_value(
                value, f"{label}.allocators"
            )["source_sha"]
            for value in allocators
        }
        observed_sources = {
            object_value(value, f"{label}.memory.raw_samples")["allocator_id"]: object_value(
                value, f"{label}.memory.raw_samples"
            )["allocator_source_sha"]
            for value in list_value(memory_report["raw_samples"], f"{label}.memory.raw_samples")
        }
        if observed_sources != expected_sources:
            fail(f"{label}.memory: allocator source pins differ from the core run")
    latency = latest.get("latency")
    if latency is not None:
        latency_report = validate_latency_report(latency, f"{label}.latency")
        expected_sources = {
            object_value(value, f"{label}.allocators")["allocator_id"]: object_value(
                value, f"{label}.allocators"
            )["source_sha"]
            for value in allocators
        }
        observed_sources = {
            object_value(value, f"{label}.latency.raw_samples")["allocator_id"]: object_value(
                value, f"{label}.latency.raw_samples"
            )["allocator_source_sha"]
            for value in list_value(latency_report["raw_samples"], f"{label}.latency.raw_samples")
        }
        if observed_sources != expected_sources:
            fail(f"{label}.latency: allocator source pins differ from the core run")
    scaling = latest.get("scaling")
    if scaling is not None:
        scaling_report = validate_scaling_report(scaling, f"{label}.scaling")
        # Compare the set of (allocator, source) pairs rather than a mapping:
        # a mapping keeps only the last sample per allocator, which would let a
        # single mispinned sample through.
        #
        # Only the lock-pinned competitors must match the core run. The sweep
        # runs weekly and overlays onto whichever daily core envelope is
        # published, so the fork's own commit is normally newer; requiring
        # equality there would make the overlay permanently unpublishable.
        expected_pairs = {
            (
                str(object_value(value, f"{label}.allocators")["allocator_id"]),
                str(object_value(value, f"{label}.allocators")["source_sha"]),
            )
            for value in allocators
            if str(object_value(value, f"{label}.allocators")["allocator_id"])
            in LOCK_PINNED_ALLOCATORS
        }
        scaling_samples = [
            object_value(value, f"{label}.scaling.raw_samples")
            for value in list_value(scaling_report["raw_samples"], f"{label}.scaling.raw_samples")
        ]
        observed_pairs = {
            (str(value["allocator_id"]), str(value["allocator_source_sha"]))
            for value in scaling_samples
            if str(value["allocator_id"]) in LOCK_PINNED_ALLOCATORS
        }
        if observed_pairs != expected_pairs:
            fail(f"{label}.scaling: allocator source pins differ from the core run")
        fork_sources = {
            str(value["allocator_source_sha"])
            for value in scaling_samples
            if str(value["allocator_id"]) == "mimalloc-pprof"
        }
        if len(fork_sources) != 1:
            fail(f"{label}.scaling: exactly one mimalloc-pprof build must be measured")
    pending = list_value(latest.get("pending_metrics"), f"{label}.pending_metrics")
    expected_pending = tuple(
        metric
        for metric, complete in (
            ("memory", memory is not None),
            ("latency", latency is not None),
            ("scaling", scaling is not None),
            ("pprof-tax", False),
        )
        if not complete
    )
    if len(pending) != len(expected_pending):
        fail(f"{label}.pending_metrics: does not match collected optional metrics")
    for index, value in enumerate(pending):
        item = object_value(value, f"{label}.pending_metrics[{index}]")
        exact_fields(
            item,
            {"metric_id", "status", "reason", "phase_issue_url"},
            f"{label}.pending_metrics[{index}]",
        )
        if item.get("status") != "pending":
            fail(f"{label}.pending_metrics[{index}]: status must be pending")
        for field in ("metric_id", "reason", "phase_issue_url"):
            string_value(item.get(field), f"{label}.pending_metrics[{index}].{field}")
        https_url_value(
            item.get("phase_issue_url"), f"{label}.pending_metrics[{index}].phase_issue_url"
        )
        if any(isinstance(item.get(field), (int, float)) for field in item):
            fail(f"{label}.pending_metrics[{index}]: pending metrics cannot contain numbers")
    if tuple(object_value(item, "pending")["metric_id"] for item in pending) != expected_pending:
        fail(f"{label}.pending_metrics: unexpected metric IDs or order")
    urls = object_value(latest.get("canonical_urls"), f"{label}.canonical_urls")
    exact_fields(
        urls, {"pages", "stats_branch", "latest_json", "methodology"}, f"{label}.canonical_urls"
    )
    for field in urls:
        https_url_value(urls[field], f"{label}.canonical_urls.{field}")
    string_value(latest.get("reproduction_command"), f"{label}.reproduction_command")
    https_url_value(latest.get("actions_run_url"), f"{label}.actions_run_url")


def parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        fail(f"{label}: invalid RFC3339 timestamp: {error}")
    if parsed.tzinfo is None:
        fail(f"{label}: timestamp must include a timezone")
    return parsed


def compact_json(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def history_row(
    latest: dict[str, object], *, include_optional_metrics: bool = True
) -> dict[str, object]:
    allocators = [
        object_value(value, "latest.allocators")
        for value in list_value(latest["allocators"], "latest.allocators")
    ]
    row: dict[str, object] = {
        "history_schema_version": HISTORY_SCHEMA,
        "statistics_version": STATISTICS_VERSION,
        "suite_version": SUITE_VERSION,
        "run": latest["run"],
        "comparison_key": comparison_digest(latest["comparison_key"], "latest.comparison_key"),
        "runner": latest["runner"],
        "allocator_identities": [
            {
                "allocator_id": allocator["allocator_id"],
                "allocator_version": allocator["allocator_version"],
                "source_sha": allocator["source_sha"],
                "library_sha256": allocator["static_library_sha256"],
                "child_binary_sha256": allocator["child_binary_sha256"],
            }
            for allocator in allocators
        ],
        "absolute_summaries": latest["absolute_summaries"],
        "paired_summaries": latest["paired_summaries"],
    }
    if include_optional_metrics and "memory" in latest:
        memory = validate_memory_report(latest["memory"], "latest.memory")
        runner = object_value(memory["runner"], "latest.memory.runner")
        row["memory"] = {
            key: value
            for key, value in memory.items()
            if key not in {"invalid_reason", "runner", "raw_samples"}
        } | {"runner_fingerprint_sha256": runner["fingerprint_sha256"]}
    if include_optional_metrics and "latency" in latest:
        latency = validate_latency_report(latest["latency"], "latest.latency")
        runner = object_value(latency["runner"], "latest.latency.runner")
        row["latency"] = {
            key: value
            for key, value in latency.items()
            if key not in {"invalid_reason", "runner", "raw_samples"}
        } | {"runner_fingerprint_sha256": runner["fingerprint_sha256"]}
    if include_optional_metrics and "scaling" in latest:
        scaling = validate_scaling_report(latest["scaling"], "latest.scaling")
        runner = object_value(scaling["runner"], "latest.scaling.runner")
        row["scaling"] = {
            key: value
            for key, value in scaling.items()
            if key not in {"invalid_reason", "runner", "topology", "patterns", "raw_samples"}
        } | {"runner_fingerprint_sha256": runner["fingerprint_sha256"]}
    return row


def validate_history_row(value: object, label: str) -> dict[str, object]:
    row = object_value(value, label)
    exact_fields_with_optional(row, HISTORY_FIELDS, OPTIONAL_HISTORY_FIELDS, label)
    if row.get("history_schema_version") != HISTORY_SCHEMA:
        fail(f"{label}.history_schema_version: incompatible schema")
    if (
        row.get("statistics_version") != STATISTICS_VERSION
        or row.get("suite_version") != SUITE_VERSION
    ):
        fail(f"{label}: incompatible statistics or suite version")
    validate_run(row.get("run"), f"{label}.run")
    comparison_digest(row.get("comparison_key"), f"{label}.comparison_key")
    validate_runner(row.get("runner"), f"{label}.runner")
    allocators = list_value(row.get("allocator_identities"), f"{label}.allocator_identities")
    if len(allocators) != len(ALLOCATOR_IDS):
        fail(f"{label}.allocator_identities: expected five")
    for index, value in enumerate(allocators):
        item = object_value(value, f"{label}.allocator_identities[{index}]")
        exact_fields(
            item,
            {
                "allocator_id",
                "allocator_version",
                "source_sha",
                "library_sha256",
                "child_binary_sha256",
            },
            f"{label}.allocator_identities[{index}]",
        )
        if item.get("allocator_id") != ALLOCATOR_IDS[index]:
            fail(f"{label}.allocator_identities[{index}].allocator_id: unexpected allocator")
        string_value(
            item.get("allocator_version"),
            f"{label}.allocator_identities[{index}].allocator_version",
        )
        if not HEX_40.fullmatch(
            string_value(
                item.get("source_sha"), f"{label}.allocator_identities[{index}].source_sha"
            )
        ):
            fail(f"{label}.allocator_identities[{index}].source_sha: invalid")
        for field in ("library_sha256", "child_binary_sha256"):
            if not HEX_64.fullmatch(
                string_value(item.get(field), f"{label}.allocator_identities[{index}].{field}")
            ):
                fail(f"{label}.allocator_identities[{index}].{field}: invalid")
    absolute = list_value(row.get("absolute_summaries"), f"{label}.absolute_summaries")
    paired = list_value(row.get("paired_summaries"), f"{label}.paired_summaries")
    if len(absolute) < 120 or len(paired) < 90:
        fail(f"{label}: compact history summaries are incomplete")
    for index, item in enumerate(absolute):
        validate_absolute(item, f"{label}.absolute_summaries[{index}]")
    for index, item in enumerate(paired):
        validate_paired(item, f"{label}.paired_summaries[{index}]")
    if "memory" in row:
        validate_memory_report(row["memory"], f"{label}.memory", compact=True)
    if "latency" in row:
        validate_latency_report(row["latency"], f"{label}.latency", compact=True)
    if "scaling" in row:
        validate_scaling_report(row["scaling"], f"{label}.scaling", compact=True)
    return row


def equivalent_payload(left: object, right: object, tolerance: float = 1e-12) -> bool:
    """Structural equality that tolerates float round-trip noise.

    A published `latest.json` that passes back through the Rust overlay
    binaries can return with a float shifted by one unit in the last place:
    serde_json's parse-and-re-emit is not bit-identical to Python's json for
    every value (observed on `relative_iqr`, ...413 in, ...412 out). The
    history merge must not read that as a rewritten historical row, but must
    still reject a real change. One ULP is around 1e-16 relative, four orders
    of magnitude below this tolerance, so genuine edits are still caught.
    """

    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, float) or isinstance(right, float):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        if math.isnan(left) or math.isnan(right):
            return False
        return left == right or math.isclose(left, right, rel_tol=tolerance, abs_tol=0.0)
    if isinstance(left, dict) and isinstance(right, dict):
        left_map = cast(dict[str, object], left)
        right_map = cast(dict[str, object], right)
        return set(left_map) == set(right_map) and all(
            equivalent_payload(left_map[key], right_map[key], tolerance) for key in left_map
        )
    if isinstance(left, list) and isinstance(right, list):
        left_list = cast(list[object], left)
        right_list = cast(list[object], right)
        return len(left_list) == len(right_list) and all(
            equivalent_payload(one, other, tolerance) for one, other in zip(left_list, right_list)
        )
    return left == right


def read_history(path: Path, initialize: bool) -> list[dict[str, object]]:
    if not path.exists():
        if not initialize:
            fail(f"{path}: history is absent; pass --initialize-history explicitly")
        return []
    if path.is_symlink() or not path.is_file():
        fail(f"{path}: history input must be a regular file")
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        fail(f"{path}: compact history requires a final newline")
    rows: list[dict[str, object]] = []
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            fail(f"{path}:{number}: blank history lines are forbidden")
        rows.append(
            validate_history_row(parse_json_bytes(line, f"{path}:{number}"), f"{path}:{number}")
        )
    reject_duplicate_history(rows, str(path))
    return rows


def reject_duplicate_history(rows: Sequence[Mapping[str, object]], label: str) -> None:
    seen: set[tuple[str, int]] = set()
    for row in rows:
        run = object_value(row["run"], f"{label}.run")
        key = (cast(str, run["run_id"]), cast(int, run["run_attempt"]))
        if key in seen:
            fail(f"{label}: duplicate run ID/attempt {key[0]}/{key[1]}")
        seen.add(key)


def history_sort_key(row: Mapping[str, object]) -> tuple[datetime, str, int, str]:
    run = object_value(row["run"], "history.run")
    return (
        parse_timestamp(cast(str, run["generated_at_utc"]), "history timestamp"),
        cast(str, run["run_id"]),
        cast(int, run["run_attempt"]),
        cast(str, row["comparison_key"]),
    )


def merge_history(
    rows: list[dict[str, object]], current: dict[str, object]
) -> list[dict[str, object]]:
    current_run = object_value(current["run"], "current history run")
    current_identity = (current_run["run_id"], current_run["run_attempt"])
    combined = list(rows)
    for index, row in enumerate(combined):
        run = object_value(row["run"], "history run")
        if (run["run_id"], run["run_attempt"]) != current_identity:
            continue
        optional = {"memory", "latency", "scaling"}
        previous_base = {key: value for key, value in row.items() if key not in optional}
        current_base = {key: value for key, value in current.items() if key not in optional}
        gained = (set(current) & optional) - (set(row) & optional)
        if not equivalent_payload(previous_base, current_base) or not gained:
            fail("history append: duplicate run may only gain a validated optional metric")
        # Keep the row exactly as it was first published and add only the newly
        # collected metric. Rewriting it from the round-tripped envelope would
        # silently move already-published numbers by the same round-trip noise
        # this comparison tolerates.
        combined[index] = dict(row) | {key: current[key] for key in sorted(gained)}
        break
    else:
        combined.append(current)
    reject_duplicate_history(combined, "history append")
    combined.sort(key=history_sort_key)
    return combined[-1000:]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, pixels: bytearray, title: str) -> bytes:
    rows = b"".join(
        b"\x00" + bytes(pixels[y * width * 3 : (y + 1) * width * 3]) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"tEXt", b"Title\x00" + title.encode("latin-1", "replace"))
        + png_chunk(b"IDAT", zlib.compress(rows, 9))
        + png_chunk(b"IEND", b"")
    )


class Canvas:
    def __init__(self, width: int, height: int, color: tuple[int, int, int]) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def rectangle(
        self, x: int, y: int, width: int, height: int, color: tuple[int, int, int]
    ) -> None:
        for row in range(max(0, y), min(self.height, y + height)):
            start = (row * self.width + max(0, x)) * 3
            end = (row * self.width + min(self.width, x + width)) * 3
            self.pixels[start:end] = bytes(color) * ((end - start) // 3)

    def line(
        self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], thickness: int = 3
    ) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for step in range(steps + 1):
            x = x0 + (x1 - x0) * step // steps
            y = y0 + (y1 - y0) * step // steps
            self.rectangle(x - thickness // 2, y - thickness // 2, thickness, thickness, color)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int],
        scale: int = 2,
    ) -> None:
        """Draw a label with the built-in 3x5 bitmap font.

        The rasterized panels carry no vector text, so axis labels, legends,
        and annotations all go through this. Unknown characters advance the
        cursor and draw nothing; letters render in uppercase.
        """

        cursor = x
        for character in value.upper():
            glyph = FONT_3X5.get(character)
            if glyph is None:
                cursor += FONT_ADVANCE * scale
                continue
            for row, bits in enumerate(glyph):
                for column, pixel in enumerate(bits):
                    if pixel == "#":
                        self.rectangle(
                            cursor + column * scale,
                            y + row * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += FONT_ADVANCE * scale


def text_width(value: str, scale: int = 2) -> int:
    """Pixel width of a label; the trailing inter-glyph gap is not counted."""

    return len(value) * FONT_ADVANCE * scale - scale


# Minimal 3x5 bitmap font. Only the glyphs the panels use are included, so the
# dict doubles as documentation of the rendered alphabet.
FONT_3X5: dict[str, tuple[str, str, str, str, str]] = {
    "0": (".#.", "#.#", "#.#", "#.#", ".#."),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("##.", "..#", ".#.", "#..", "###"),
    "3": ("##.", "..#", ".#.", "..#", "##."),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "##.", "..#", "##."),
    "6": (".##", "#..", "##.", "#.#", ".#."),
    "7": ("###", "..#", ".#.", ".#.", ".#."),
    "8": (".#.", "#.#", ".#.", "#.#", ".#."),
    "9": (".#.", "#.#", ".##", "..#", "##."),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": (".##", "#..", "#..", "#..", ".##"),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"),
    "F": ("###", "#..", "##.", "#..", "#.."),
    "G": (".##", "#..", "#.#", "#.#", ".##"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "J": ("..#", "..#", "..#", "#.#", ".#."),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    "M": ("#.#", "###", "###", "#.#", "#.#"),
    "N": ("##.", "#.#", "#.#", "#.#", "#.#"),
    "O": (".#.", "#.#", "#.#", "#.#", ".#."),
    "P": ("##.", "#.#", "##.", "#..", "#.."),
    "Q": (".#.", "#.#", "#.#", "##.", ".##"),
    "R": ("##.", "#.#", "##.", "#.#", "#.#"),
    "S": (".##", "#..", ".#.", "..#", "##."),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", "###"),
    "V": ("#.#", "#.#", "#.#", "#.#", ".#."),
    "W": ("#.#", "#.#", "###", "###", "#.#"),
    "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", ".#.", ".#.", ".#."),
    "Z": ("###", "..#", ".#.", "#..", "###"),
    " ": ("...", "...", "...", "...", "..."),
    ".": ("...", "...", "...", "...", ".#."),
    ",": ("...", "...", "...", "..#", ".#."),
    "%": ("#.#", "..#", ".#.", "#..", "#.#"),
    "/": ("..#", "..#", ".#.", "#..", "#.."),
    "-": ("...", "...", "###", "...", "..."),
    "+": ("...", ".#.", "###", ".#.", "..."),
    "(": ("..#", ".#.", "#..", ".#.", "..#"),
    ")": ("#..", ".#.", "..#", ".#.", "#.."),
    "=": ("...", "###", "...", "###", "..."),
    "<": ("..#", ".#.", "#..", ".#.", "..#"),
    ">": ("#..", ".#.", "..#", ".#.", "#.."),
    "^": (".#.", "#.#", "...", "...", "..."),
}
FONT_GLYPH_WIDTH = 3
FONT_GLYPH_HEIGHT = 5
FONT_ADVANCE = 4  # glyph width plus one blank column


COLORS = [
    (53, 132, 228),
    (239, 108, 0),
    (15, 157, 88),
    (0, 150, 160),
    (171, 71, 188),
]


def throughput_png(latest: Mapping[str, object]) -> bytes:
    canvas = Canvas(1280, 720, (248, 250, 252))
    canvas.rectangle(0, 0, 1280, 72, (24, 35, 52))
    records = [
        validate_absolute(value, "absolute summary")
        for value in list_value(latest["absolute_summaries"], "absolute summaries")
    ]
    cells: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for record in records:
        key = (
            cast(str, record["scenario_id"]),
            cast(str, record["thread_point"]),
            cast(str, record["metric_id"]),
        )
        cells.setdefault(key, []).append(record)
    for ordinal, (_key, values) in enumerate(sorted(cells.items())[:12]):
        column, row = ordinal % 3, ordinal // 3
        left, top = 55 + column * 410, 100 + row * 145
        canvas.rectangle(left, top, 370, 118, (235, 240, 246))
        medians = [
            float_value(object_value(value["summary"], "summary")["median"], "median", True)
            for value in values
        ]
        maximum = max(medians)
        for index, median in enumerate(medians[: len(ALLOCATOR_IDS)]):
            canvas.rectangle(
                left + 18, top + 18 + index * 19, int(325 * median / maximum), 12, COLORS[index]
            )
    return encode_png(1280, 720, canvas.pixels, "Validated paired benchmark throughput")


def history_png(rows: Sequence[Mapping[str, object]], key: str) -> bytes:
    canvas = Canvas(1280, 720, (248, 250, 252))
    canvas.rectangle(0, 0, 1280, 72, (24, 35, 52))
    selected = [row for row in rows if row.get("comparison_key") == key]
    values: list[float] = []
    for row in selected:
        absolute = list_value(row["absolute_summaries"], "history absolute summaries")
        if absolute:
            summary = object_value(
                object_value(absolute[0], "history summary")["summary"], "history summary"
            )
            values.append(float_value(summary["median"], "history median", True))
    canvas.line(80, 640, 1220, 640, (90, 102, 115))
    canvas.line(80, 110, 80, 640, (90, 102, 115))
    if values:
        low, high = min(values), max(values)
        span = max(high - low, high * 0.01)
        points: list[tuple[int, int]] = []
        for index, value in enumerate(values):
            x = 100 + (1100 * index // max(1, len(values) - 1))
            y = 610 - int(460 * (value - low) / span)
            points.append((x, y))
            canvas.rectangle(x - 5, y - 5, 11, 11, COLORS[2])
        for first, second in zip(points, points[1:]):
            canvas.line(first[0], first[1], second[0], second[1], COLORS[2])
    return encode_png(1280, 720, canvas.pixels, "History for one identical comparison key")


def pending_png(metric: str, reason: str, width: int = 960, height: int = 540) -> bytes:
    canvas = Canvas(width, height, (248, 250, 252))
    left = max(30, (width - 850) // 2)
    top = max(30, (height - 430) // 2)
    canvas.rectangle(left, top, 850, 430, (231, 236, 242))
    canvas.rectangle(left, top, 850, 18, (117, 126, 140))
    canvas.rectangle(left + 115, top + 150, 620, 95, (255, 193, 7))
    canvas.line(left + 150, top + 290, left + 700, top + 290, (117, 126, 140), 5)
    return encode_png(width, height, canvas.pixels, f"PENDING {metric}: {reason}")


MEMORY_PANEL_WIDTH = 960
MEMORY_PANEL_HEIGHT = 540
MEMORY_BAR_MAX_WIDTH = 380


def draw_allocator_legend(canvas: Canvas, x: int, y: int, swatch: int = 18, scale: int = 2) -> None:
    """Header legend with one fixed swatch per allocator, in allocator order."""

    for allocator in ALLOCATOR_IDS:
        canvas.rectangle(x, y, swatch, swatch, COLORS[ALLOCATOR_IDS.index(allocator)])
        canvas.text(x + swatch + 8, y, allocator, (232, 238, 247), scale)
        x += swatch + 8 + text_width(allocator, scale) + 30


def memory_bar_cells(
    memory: Mapping[str, object], metric_id: str
) -> dict[tuple[str, str], dict[str, float]]:
    """Per-cell per-allocator medians for one memory metric, keyed in
    allocator order so bar colors are always the legend colors."""

    records = [
        validate_absolute(value, "memory bar summary")
        for value in list_value(memory["absolute_summaries"], "memory bar summaries")
        if object_value(value, "memory bar summary").get("metric_id") == metric_id
    ]
    cells: dict[tuple[str, str], dict[str, float]] = {}
    for record in records:
        key = (cast(str, record["scenario_id"]), cast(str, record["thread_point"]))
        allocator = cast(str, record["allocator_id"])
        median = float_value(
            object_value(record["summary"], "memory bar summary")["median"],
            "memory bar median",
            True,
        )
        cells.setdefault(key, {})[allocator] = median
    for key, values in cells.items():
        cells[key] = {
            allocator: values[allocator] for allocator in ALLOCATOR_IDS if allocator in values
        }
    return cells


def memory_bar_length(value: float, maximum: float) -> int:
    """Bar length in pixels for a value on a per-cell scale; the largest
    value fills the bar zone exactly."""

    if maximum <= 0:
        fail("memory bar cell maximum must be positive")
    return max(1, int(MEMORY_BAR_MAX_WIDTH * value / maximum))


def draw_ratio_bar_grid(
    canvas: Canvas, cells: Mapping[tuple[str, str], Mapping[str, float]]
) -> None:
    """Shared two-column bar grid for ratio panels: bars scale to each cell's
    largest value, every cell carries a 1.0 reference line, and cells are
    labeled. The 1.0 line is the honest reading for both panels: upstream
    parity for the normalized memory bars, RSS-equals-live-bytes for the
    fragmentation proxy."""

    canvas.rectangle(0, 0, MEMORY_PANEL_WIDTH, 62, (24, 35, 52))
    draw_allocator_legend(canvas, 30, 22)
    if not cells:
        empty = "no ratio cells in this envelope"
        canvas.text(
            (MEMORY_PANEL_WIDTH - text_width(empty, 2)) // 2,
            240,
            empty,
            (117, 126, 140),
            2,
        )
        return
    for ordinal, (key, values) in enumerate(sorted(cells.items())):
        column, row = ordinal % 2, ordinal // 2
        left, top = 45 + column * 460, 85 + row * 112
        canvas.rectangle(left, top, 420, 92, (235, 240, 246))
        canvas.text(left + 8, top - 14, f"{key[0]}/{key[1]}", (90, 102, 115), 1)
        maximum = max(values.values())
        bar_left = left + 14
        for index, allocator in enumerate(ALLOCATOR_IDS):
            if allocator not in values:
                continue
            value = values[allocator]
            bar_length = memory_bar_length(value, maximum)
            canvas.rectangle(
                bar_left,
                top + 10 + index * 15,
                bar_length,
                9,
                COLORS[ALLOCATOR_IDS.index(allocator)],
            )
            label = f"{value:.2f}x"
            canvas.text(
                bar_left + bar_length + 6,
                top + 9 + index * 15,
                label,
                COLORS[ALLOCATOR_IDS.index(allocator)],
                1,
            )
        # The 1.0 reference line is drawn on top of the bars so it stays
        # visible even when an allocator's bar extends past it.
        baseline_x = bar_left + min(MEMORY_BAR_MAX_WIDTH, memory_bar_length(1.0, maximum))
        canvas.line(baseline_x, top + 4, baseline_x, top + 88, (90, 102, 115), 2)
        canvas.text(
            baseline_x - text_width("1.0x", 1) // 2,
            top + 80,
            "1.0x",
            (90, 102, 115),
            1,
        )


def memory_png(memory: Mapping[str, object]) -> bytes:
    """Sampled-peak RSS bars normalized to upstream-mimalloc = 1.0, matching
    the throughput panel's normalization so '+X% memory' reads at a glance."""

    raw = memory_bar_cells(memory, "sampled-peak-rss-bytes")
    normalized: dict[tuple[str, str], dict[str, float]] = {}
    for key, values in raw.items():
        reference = values.get("upstream-mimalloc")
        if reference is None or reference <= 0:
            fail(f"memory cell {key}: missing upstream-mimalloc reference")
        normalized[key] = {allocator: median / reference for allocator, median in values.items()}
    canvas = Canvas(MEMORY_PANEL_WIDTH, MEMORY_PANEL_HEIGHT, (248, 250, 252))
    draw_ratio_bar_grid(canvas, normalized)
    return encode_png(
        MEMORY_PANEL_WIDTH,
        MEMORY_PANEL_HEIGHT,
        canvas.pixels,
        "Sampled peak RSS relative to upstream-mimalloc; 1.0 = upstream; lower is better",
    )


def fragmentation_png(memory: Mapping[str, object]) -> bytes:
    """Fragmentation proxy (sampled peak RSS delta / peak live bytes) as its
    own lower-is-better panel with a 1.0 reference line."""

    cells = memory_bar_cells(memory, "fragmentation-proxy")
    canvas = Canvas(MEMORY_PANEL_WIDTH, MEMORY_PANEL_HEIGHT, (248, 250, 252))
    draw_ratio_bar_grid(canvas, cells)
    return encode_png(
        MEMORY_PANEL_WIDTH,
        MEMORY_PANEL_HEIGHT,
        canvas.pixels,
        "Fragmentation proxy (sampled peak RSS delta / peak live bytes); lower is better",
    )


def latency_png(latency: Mapping[str, object]) -> bytes:
    canvas = Canvas(960, 540, (248, 250, 252))
    canvas.rectangle(0, 0, 960, 62, (24, 35, 52))
    records = [
        object_value(value, "latency absolute summary")
        for value in list_value(latency["absolute_summaries"], "latency absolute summaries")
    ]
    cells: dict[tuple[str, str], list[dict[str, object]]] = {}
    for record in records:
        key = (cast(str, record["scenario_id"]), cast(str, record["thread_point"]))
        cells.setdefault(key, []).append(record)
    for ordinal, (_key, values) in enumerate(sorted(cells.items())):
        column, row = ordinal % 2, ordinal // 2
        left, top = 45 + column * 460, 85 + row * 145
        canvas.rectangle(left, top, 420, 120, (235, 240, 246))
        values.sort(key=lambda value: ALLOCATOR_IDS.index(cast(str, value["allocator_id"])))
        p99_values = [
            float_value(
                object_value(value["measured"], "latency measured")["p99_ns"],
                "latency p99",
                True,
            )
            for value in values
        ]
        maximum = max(p99_values)
        for index, (value, duration) in enumerate(zip(values, p99_values)):
            canvas.rectangle(
                left + 14,
                top + 13 + index * 20,
                max(1, int(380 * duration / maximum)),
                12,
                COLORS[ALLOCATOR_IDS.index(cast(str, value["allocator_id"]))],
            )
    return encode_png(
        960,
        540,
        canvas.pixels,
        "Transaction latency p99; allocation plus touch/checksum through free; lower is better",
    )


PARETO_WIDTH = 960
PARETO_HEIGHT = 540
# Plot margins in pixels: left, top, right, bottom.
PARETO_MARGINS = (118, 96, 30, 56)


def pareto_points(
    memory: Mapping[str, object], latest: Mapping[str, object]
) -> list[tuple[str, float, float, str, str]]:
    """Pair each memory fragmentation median with the core throughput median
    on the matching scenario/thread/allocator cell.

    Every returned item is (allocator_id, fragmentation_proxy,
    ops_per_second, scenario_id, thread_point). Cells missing either half are
    skipped: the chart renders exactly what genuinely matches, and nothing
    is fabricated for cells that do not.
    """

    def cell_medians(
        records: Sequence[dict[str, object]], metric_id: str
    ) -> dict[tuple[str, str, str], float]:
        medians: dict[tuple[str, str, str], float] = {}
        for record in records:
            if cast(str, record["metric_id"]) != metric_id:
                continue
            key = (
                cast(str, record["scenario_id"]),
                cast(str, record["thread_point"]),
                cast(str, record["allocator_id"]),
            )
            medians[key] = float_value(
                object_value(record["summary"], "pareto summary")["median"],
                "pareto median",
                True,
            )
        return medians

    memory_records = [
        validate_absolute(value, "memory pareto record")
        for value in list_value(memory["absolute_summaries"], "memory pareto summaries")
    ]
    core_records = [
        validate_absolute(value, "core pareto record")
        for value in list_value(latest["absolute_summaries"], "core pareto summaries")
    ]
    fragmentation = cell_medians(memory_records, "fragmentation-proxy")
    throughput = cell_medians(core_records, "throughput-operations-per-second")
    return [
        (key[2], fragmentation[key], throughput[key], key[0], key[1])
        for key in sorted(set(fragmentation) & set(throughput))
    ]


def pareto_scale(points: Sequence[tuple[str, float, float, str, str]]) -> tuple[float, float]:
    """Axis ceilings with 15% headroom over the largest observed value."""

    x_max = (
        max(
            (fragmentation for _allocator, fragmentation, _ops, _scenario, _point in points),
            default=0.0,
        )
        * 1.15
    )
    y_max = (
        max(
            (ops for _allocator, _fragmentation, ops, _scenario, _point in points),
            default=0.0,
        )
        * 1.15
    )
    return max(x_max, 1.0), max(y_max, 1.0)


def pareto_x(fragmentation: float, x_max: float) -> float:
    left, _top, right, _bottom = PARETO_MARGINS
    return left + (PARETO_WIDTH - left - right) * fragmentation / x_max


def pareto_y(ops: float, y_max: float) -> float:
    _left, top, _right, bottom = PARETO_MARGINS
    return top + (PARETO_HEIGHT - top - bottom) * (1.0 - ops / y_max)


def draw_pareto(canvas: Canvas, points: Sequence[tuple[str, float, float, str, str]]) -> None:
    """Fragmentation (x, lower is better) vs throughput (y, higher is better)
    with one colored marker per matched cell. The upper-left corner is the
    good corner and is marked explicitly."""

    left, top, right, bottom = PARETO_MARGINS
    plot_width = PARETO_WIDTH - left - right
    plot_height = PARETO_HEIGHT - top - bottom
    axis_color = (90, 102, 115)
    grid_color = (223, 229, 236)
    label_color = (90, 102, 115)
    canvas.rectangle(0, 0, PARETO_WIDTH, 62, (24, 35, 52))
    draw_allocator_legend(canvas, 30, 22)
    x_max, y_max = pareto_scale(points)
    unit = axis_unit(y_max)
    for step in range(5):
        fraction = step / 4
        x = round(left + plot_width * fraction)
        y = round(top + plot_height * (1 - fraction))
        canvas.line(x, top, x, PARETO_HEIGHT - bottom, grid_color)
        canvas.line(left, y, PARETO_WIDTH - right, y, grid_color)
        x_label = f"{x_max * fraction:.2f}"
        canvas.text(
            x - text_width(x_label) // 2,
            PARETO_HEIGHT - bottom + 12,
            x_label,
            label_color,
        )
        y_label = format_throughput(y_max * fraction, unit)
        canvas.text(left - 10 - text_width(y_label), y - 5, y_label, label_color)
    canvas.line(left, top, left, PARETO_HEIGHT - bottom, axis_color, 2)
    canvas.line(
        left, PARETO_HEIGHT - bottom, PARETO_WIDTH - right, PARETO_HEIGHT - bottom, axis_color, 2
    )
    canvas.text(left, top - 30, "median throughput (ops/s)", label_color, 1)
    x_caption = "fragmentation proxy (lower is better)"
    canvas.text(
        left + (plot_width - text_width(x_caption, 1)) // 2,
        PARETO_HEIGHT - bottom + 27,
        x_caption,
        label_color,
        1,
    )
    if not points:
        empty = "no memory cells match a core throughput cell"
        canvas.text(
            left + (plot_width - text_width(empty, 2)) // 2,
            top + plot_height // 2 - 10,
            empty,
            (117, 126, 140),
            2,
        )
        return
    # Mark the good corner with a green L-bracket and label.
    better = (15, 157, 88)
    canvas.line(left + 4, top + 6, left + 28, top + 6, better, 4)
    canvas.line(left + 6, top + 4, left + 6, top + 26, better, 4)
    canvas.text(left + 14, top + 34, "better", better, 2)
    for allocator, fragmentation, ops, _scenario, _point in points:
        x = round(pareto_x(fragmentation, x_max))
        y = round(pareto_y(ops, y_max))
        canvas.rectangle(x - 4, y - 4, 8, 8, COLORS[ALLOCATOR_IDS.index(allocator)])


def pareto_png(latest: Mapping[str, object]) -> bytes:
    # Envelope-level validation belongs to validate_latest in render(); like
    # the sibling draw functions this validates only the records it consumes.
    memory = object_value(latest["memory"], "latest.memory")
    points = pareto_points(memory, latest)
    canvas = Canvas(PARETO_WIDTH, PARETO_HEIGHT, (248, 250, 252))
    draw_pareto(canvas, points)
    return encode_png(
        PARETO_WIDTH,
        PARETO_HEIGHT,
        canvas.pixels,
        "Speed-memory Pareto scatter; fragmentation proxy vs median throughput; upper-left is better",
    )


TIMELINE_WIDTH = 1280
TIMELINE_HEIGHT = 720
TIMELINE_COLS = 4
TIMELINE_ROWS = 2
# Per-slot plot margins in pixels: left, top, right, bottom. The bottom margin
# fits two label rows: the time tick labels plus the post-drain offset diamonds.
TIMELINE_SLOT_MARGINS = (54, 26, 6, 34)
TIMELINE_DRAIN_COLOR = (90, 102, 115)
TIMELINE_GRID_COLOR = (223, 229, 236)


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _format_mib(value: int) -> str:
    return f"{value // (1024 * 1024)}M"


def _timeline_series(group: Sequence[Mapping[str, object]], label: str) -> dict[str, object]:
    """Per-allocator series for one memory cell: the raw (elapsed, rss) points
    plus the median drained timestamp and the three post-drain medians."""

    allocators: dict[str, list[Mapping[str, object]]] = {}
    for sample in group:
        allocator = string_value(sample.get("allocator_id"), f"{label}.allocator_id")
        if allocator not in ALLOCATOR_IDS:
            fail(f"{label}: unknown allocator {allocator}")
        allocators.setdefault(allocator, []).append(sample)
    series: dict[str, dict[str, object]] = {}
    for allocator in ALLOCATOR_IDS:
        samples = allocators[allocator]
        points: list[tuple[int, int]] = []
        for sample in samples:
            timeline = list_value(sample.get("timeline"), f"{label}.timeline")
            for index, point_value in enumerate(timeline):
                point = object_value(point_value, f"{label}.timeline[{index}]")
                points.append(
                    (
                        int_value(
                            point.get("elapsed_ns"), f"{label}.timeline[{index}].elapsed_ns", 1
                        ),
                        int_value(
                            point.get("rss_bytes"), f"{label}.timeline[{index}].rss_bytes", 1
                        ),
                    )
                )
        series[allocator] = {
            "points": sorted(points),
            "drained_ns": _median_int(
                [
                    int_value(sample.get("workload_drained_ns"), f"{label}.workload_drained_ns", 1)
                    for sample in samples
                ]
            ),
            "active_ns": _median_int(
                [
                    int_value(sample.get("workload_active_ns"), f"{label}.workload_active_ns", 1)
                    for sample in samples
                ]
            ),
            "decay": [
                (
                    _median_int(
                        [
                            int_value(
                                sample.get(f"post_drain_sample_{delay}_ns"),
                                f"{label}.post_drain_sample_{delay}_ns",
                                1,
                            )
                            for sample in samples
                        ]
                    ),
                    _median_int(
                        [
                            int_value(
                                sample.get(f"post_drain_rss_{delay}_bytes"),
                                f"{label}.post_drain_rss_{delay}_bytes",
                                1,
                            )
                            for sample in samples
                        ]
                    ),
                )
                for delay in ("100ms", "1s", "5s")
            ],
        }
    return {
        "series": series,
        "drained_ns": _median_int(
            [cast(int, series[allocator]["drained_ns"]) for allocator in ALLOCATOR_IDS]
        ),
        "active_ns": _median_int(
            [cast(int, series[allocator]["active_ns"]) for allocator in ALLOCATOR_IDS]
        ),
    }


def timeline_cells(memory: Mapping[str, object]) -> list[dict[str, object]]:
    """Group the memory raw samples by declared cell, in cell order."""

    samples = [
        object_value(value, "timeline sample")
        for value in list_value(memory["raw_samples"], "timeline raw samples")
    ]
    grouped: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for sample in samples:
        key = (
            string_value(sample.get("scenario_id"), "timeline scenario"),
            string_value(sample.get("thread_point"), "timeline thread point"),
        )
        if key not in MEMORY_CELLS:
            fail(f"timeline sample: undeclared memory cell {key}")
        grouped.setdefault(key, []).append(sample)
    cells: list[dict[str, object]] = []
    for key in MEMORY_CELLS:
        group = grouped.get(key, [])
        if not group:
            continue
        series = _timeline_series(group, f"memory cell {key[0]}/{key[1]}")
        cells.append(
            {
                "scenario": key[0],
                "point": key[1],
                "series": series["series"],
                "active_ns": series["active_ns"],
                "drained_ns": series["drained_ns"],
            }
        )
    return cells


def timeline_domain(cells: Sequence[Mapping[str, object]]) -> tuple[int, int, int, int]:
    """Global time/RSS domain across every cell, so all seven mini charts
    share one scale and can be compared at a glance."""

    active_values: list[int] = []
    t_max_values: list[int] = []
    rss_values: list[int] = []
    for cell in cells:
        for allocator in ALLOCATOR_IDS:
            series = object_value(cell["series"], "timeline series")[allocator]
            series = object_value(series, "timeline allocator series")
            active = int(cast(int, series["active_ns"]))
            active_values.append(active)
            drained = int(cast(int, series["drained_ns"]))
            t_max_values.append(drained + 5_000_000_000)
            for _elapsed, rss in cast(list[tuple[int, int]], series["points"]):
                rss_values.append(rss)
            for elapsed, rss in cast(list[tuple[int, int]], series["decay"]):
                rss_values.append(rss)
                t_max_values.append(elapsed)
    if not active_values or not t_max_values or not rss_values:
        fail("timeline cells contain no series data")
    # The x domain runs to the latest 5 s post-drain sample so return-to-OS is
    # visible; per-sample windows may legally overshoot by up to 100 ms.
    active_min = min(active_values)
    t_max = max(t_max_values)
    rss_min = min(rss_values)
    rss_max = max(rss_values)
    rss_span = rss_max - rss_min
    if rss_span == 0:
        rss_span = max(rss_max // 10, 1)
    return active_min, t_max, rss_min - rss_span // 10, rss_max + rss_span // 10


def timeline_x(elapsed_ns: int, t_min: int, t_max: int, left: int, width: int) -> float:
    span = max(t_max - t_min, 1)
    return left + width * (elapsed_ns - t_min) / span


def timeline_y(rss_bytes: int, rss_min: int, rss_max: int, top: int, height: int) -> float:
    span = max(rss_max - rss_min, 1)
    return top + height * (1 - (rss_bytes - rss_min) / span)


def _draw_diamond(canvas: Canvas, x: int, y: int, radius: int, color: tuple[int, int, int]) -> None:
    canvas.line(x - radius, y, x, y - radius, color, 2)
    canvas.line(x, y - radius, x + radius, y, color, 2)
    canvas.line(x + radius, y, x, y + radius, color, 2)
    canvas.line(x, y + radius, x - radius, y, color, 2)


def draw_rss_timeline(canvas: Canvas, cells: Sequence[Mapping[str, object]]) -> None:
    """One mini chart per memory cell: external RSS over time for all five
    allocators, with the workload-drained marker and the three post-drain
    return-to-OS points annotated on the axis."""

    slot_width = TIMELINE_WIDTH // TIMELINE_COLS
    slot_height = TIMELINE_HEIGHT // TIMELINE_ROWS
    slot_left, slot_top, slot_right, slot_bottom = TIMELINE_SLOT_MARGINS
    plot_width = slot_width - slot_left - slot_right
    plot_height = slot_height - slot_top - slot_bottom
    t_min, t_max, rss_min, rss_max = timeline_domain(cells)
    label_color = (90, 102, 115)
    for index, cell in enumerate(cells):
        slot_x = (index % TIMELINE_COLS) * slot_width
        slot_y = (index // TIMELINE_COLS) * slot_height
        left = slot_x + slot_left
        top = slot_y + slot_top
        bottom = slot_y + slot_height - slot_bottom
        canvas.text(
            slot_x + 6,
            slot_y + 8,
            f"{cell['scenario']}/{cell['point']}",
            label_color,
            1,
        )
        for step in range(3):
            fraction = step / 2
            y = top + plot_height * (1 - fraction)
            canvas.line(left, round(y), left + plot_width, round(y), TIMELINE_GRID_COLOR)
            rss = rss_min + int((rss_max - rss_min) * fraction)
            label = _format_mib(rss)
            canvas.text(left - 4 - text_width(label, 1), round(y) - 3, label, label_color, 1)
        for step in range(3):
            fraction = step / 2
            x = left + plot_width * fraction
            elapsed = t_min + int((t_max - t_min) * fraction)
            label = f"{(elapsed - t_min) / 1_000_000_000:.1f}s"
            canvas.text(round(x) - text_width(label, 1) // 2, bottom + 2, label, label_color, 1)
        # Axis ticks marking the three post-drain sample offsets.
        for delay_label, delay_ns in (
            ("100ms", 100_000_000),
            ("1s", 1_000_000_000),
            ("5s", 5_000_000_000),
        ):
            x = round(
                timeline_x(cast(int, cell["drained_ns"]) + delay_ns, t_min, t_max, left, plot_width)
            )
            _draw_diamond(canvas, x, bottom + 10, 3, label_color)
            canvas.text(
                x - text_width(delay_label, 1) // 2, bottom + 17, delay_label, label_color, 1
            )
        canvas.line(left, top, left, bottom, label_color, 2)
        canvas.line(left, bottom, left + plot_width, bottom, label_color, 2)
        drained_x = round(timeline_x(cast(int, cell["drained_ns"]), t_min, t_max, left, plot_width))
        # Dashed workload-drained marker, annotated with a D at the top.
        dash_y = top
        on = True
        while dash_y < bottom:
            if on:
                canvas.line(
                    drained_x, dash_y, drained_x, min(dash_y + 5, bottom), TIMELINE_DRAIN_COLOR, 2
                )
            dash_y += 5 if on else 4
            on = not on
        canvas.text(drained_x + 4, top + 2, "D", TIMELINE_DRAIN_COLOR, 1)
        # Lines first, markers second: a steep line from another allocator may
        # pass through this allocator's peak, and its marker must stay visible.
        strokes: list[
            tuple[tuple[int, int, int], list[tuple[int, int]], list[tuple[int, int]]]
        ] = []
        for allocator in ALLOCATOR_IDS:
            series = object_value(
                object_value(cell["series"], "timeline series")[allocator],
                "timeline allocator series",
            )
            points = cast(list[tuple[int, int]], series["points"])
            if not points:
                continue
            color = COLORS[ALLOCATOR_IDS.index(allocator)]
            plotted = [
                (
                    round(timeline_x(elapsed, t_min, t_max, left, plot_width)),
                    round(timeline_y(rss, rss_min, rss_max, top, plot_height)),
                )
                for elapsed, rss in points
            ]
            decay = [
                (
                    round(timeline_x(elapsed, t_min, t_max, left, plot_width)),
                    round(timeline_y(rss, rss_min, rss_max, top, plot_height)),
                )
                for elapsed, rss in cast(list[tuple[int, int]], series["decay"])
            ]
            strokes.append((color, plotted, decay))
        for color, plotted, _decay in strokes:
            for first, second in zip(plotted, plotted[1:]):
                canvas.line(first[0], first[1], second[0], second[1], color, 2)
        for color, plotted, decay in strokes:
            for x, y in plotted:
                canvas.rectangle(x - 1, y - 1, 3, 3, color)
            for x, y in decay:
                _draw_diamond(canvas, x, y, 5, color)
    # The final slot is the legend instead of an eighth chart.
    legend_slot = TIMELINE_COLS * TIMELINE_ROWS - 1
    legend_x = (legend_slot % TIMELINE_COLS) * slot_width + 16
    legend_y = (legend_slot // TIMELINE_COLS) * slot_height + 18
    canvas.text(legend_x, legend_y, "LEGEND", label_color, 2)
    for row, allocator in enumerate(ALLOCATOR_IDS):
        swatch_y = legend_y + 22 + row * 20
        canvas.rectangle(legend_x, swatch_y, 14, 14, COLORS[ALLOCATOR_IDS.index(allocator)])
        canvas.text(legend_x + 20, swatch_y + 2, allocator, label_color, 1)
    notes = [
        "line: external RSS over time,",
        "5 ms smaps_rollup sampling,",
        "all paired blocks",
        "",
        "D dashed: workload drained",
        "",
        "axis diamonds: post-drain",
        "sample offsets 100ms/1s/5s",
        "",
        "colored diamonds: median",
        "post-drain RSS per allocator",
        "(return-to-OS)",
        "",
        "natural purge only;",
        "lower is better",
    ]
    note_y = legend_y + 22 + len(ALLOCATOR_IDS) * 20 + 10
    for note in notes:
        canvas.text(legend_x, note_y, note, label_color, 1)
        note_y += 12


def rss_timeline_png(memory: Mapping[str, object]) -> bytes:
    # Envelope-level validation belongs to validate_latest in render(); this
    # validates only the fields it consumes, like the sibling draw functions.
    cells = timeline_cells(memory)
    canvas = Canvas(TIMELINE_WIDTH, TIMELINE_HEIGHT, (248, 250, 252))
    draw_rss_timeline(canvas, cells)
    return encode_png(
        TIMELINE_WIDTH,
        TIMELINE_HEIGHT,
        canvas.pixels,
        "Linux process RSS over time with post-drain return-to-OS points; lower is better",
    )


SCALING_REPORT_FIELDS = {
    "metric_schema_version",
    "status",
    "invalid_reason",
    "metric_comparison_key",
    "run",
    "runner",
    "topology",
    "direction",
    "informational",
    "rigor_label",
    "thread_points",
    "patterns",
    "methodology",
    "cell_summaries",
    "raw_samples",
}
SCALING_METHODOLOGY_FIELDS = {
    "rigor",
    "blocks_per_cell",
    "aggregation",
    "operation_stream",
    "seed_chain",
    "pairing",
    "work_normalization",
    "oversubscription",
    "cross_thread_backpressure",
    "statistics_omitted",
}
SCALING_SUMMARY_FIELDS = {
    "pattern",
    "thread_count",
    "oversubscription_factor",
    "oversubscribed",
    "allocator_id",
    "block_count",
    "median_throughput",
    "min_throughput",
    "max_throughput",
    "speedup_vs_single_worker",
}


def validate_scaling_rss(
    value: object, label: str, thread_points: tuple[int, ...]
) -> dict[str, object]:
    """The RSS side-car is an optional object with its own schema version, so
    rows published before it existed stay valid history.

    `thread_points` is the sweep shape its parent report declares, not the
    current contract: a side-car must cover exactly the cells its own report
    measured, whichever lineage that report belongs to.
    """

    rss = object_value(value, label)
    exact_fields(rss, {"metric_schema_version", "sampling", "cell_summaries"}, label)
    if rss.get("metric_schema_version") != SCALING_RSS_SCHEMA:
        fail(f"{label}.metric_schema_version: unsupported scaling RSS schema")
    sampling = object_value(rss.get("sampling"), f"{label}.sampling")
    exact_fields(sampling, {"source", "method", "poll_interval_ns"}, f"{label}.sampling")
    string_value(sampling.get("source"), f"{label}.sampling.source")
    string_value(sampling.get("method"), f"{label}.sampling.method")
    int_value(sampling.get("poll_interval_ns"), f"{label}.sampling.poll_interval_ns", 1)
    summaries = [
        object_value(item, f"{label}.cell_summaries[{index}]")
        for index, item in enumerate(
            list_value(rss.get("cell_summaries"), f"{label}.cell_summaries")
        )
    ]
    expected_cells = len(SCALING_PATTERN_IDS) * len(thread_points) * len(ALLOCATOR_IDS)
    if len(summaries) != expected_cells:
        fail(f"{label}.cell_summaries: expected exactly {expected_cells} cells")
    seen: set[tuple[str, int, str]] = set()
    for index, summary in enumerate(summaries):
        exact_fields(
            summary,
            {
                "pattern",
                "thread_count",
                "allocator_id",
                "block_count",
                "median_peak_rss_bytes",
                "min_peak_rss_bytes",
                "max_peak_rss_bytes",
            },
            f"{label}.cell_summaries[{index}]",
        )
        pattern = string_value(summary.get("pattern"), f"{label}.cell_summaries[{index}].pattern")
        allocator = string_value(
            summary.get("allocator_id"), f"{label}.cell_summaries[{index}].allocator_id"
        )
        threads = int_value(
            summary.get("thread_count"), f"{label}.cell_summaries[{index}].thread_count", 1
        )
        key = (pattern, threads, allocator)
        if (
            pattern not in SCALING_PATTERN_IDS
            or allocator not in ALLOCATOR_IDS
            or threads not in thread_points
            or key in seen
        ):
            fail(f"{label}.cell_summaries[{index}]: undeclared or duplicate RSS cell")
        seen.add(key)
        if (
            int_value(summary.get("block_count"), f"{label}.cell_summaries[{index}].block_count", 1)
            != SCALING_BLOCKS
        ):
            fail(f"{label}.cell_summaries[{index}].block_count: {SCALING_BLOCKS} blocks required")
        minimum = int_value(
            summary.get("min_peak_rss_bytes"), f"{label}.cell_summaries[{index}].min", 1
        )
        median = int_value(
            summary.get("median_peak_rss_bytes"), f"{label}.cell_summaries[{index}].median", 1
        )
        maximum = int_value(
            summary.get("max_peak_rss_bytes"), f"{label}.cell_summaries[{index}].max", 1
        )
        if minimum > median or maximum < median:
            fail(f"{label}.cell_summaries[{index}]: min/median/max are inconsistent")
    return rss


def validate_scaling_report(
    value: object, label: str, *, compact: bool = False
) -> dict[str, object]:
    report = object_value(value, label)
    required = SCALING_REPORT_FIELDS - (
        {"invalid_reason", "runner", "topology", "patterns", "raw_samples"} if compact else set()
    )
    if compact:
        required.add("runner_fingerprint_sha256")
    exact_fields_with_optional(report, required, {"rss"}, label)
    if (
        report.get("metric_schema_version") != SCALING_SCHEMA
        or report.get("status") != "complete"
        or report.get("direction") != "higher-is-better"
        or report.get("informational") is not True
    ):
        fail(f"{label}: complete informational higher-is-better scaling required")
    if report.get("rigor_label") != SCALING_RIGOR_LABEL:
        fail(f"{label}.rigor_label: coverage-mode labeling is mandatory")
    points = [
        int_value(item, f"{label}.thread_points[{index}]", 1)
        for index, item in enumerate(
            list_value(report.get("thread_points"), f"{label}.thread_points")
        )
    ]
    thread_points = tuple(points)
    if thread_points not in SCALING_THREAD_POINT_LINEAGES:
        fail(
            f"{label}.thread_points: {list(thread_points)} is not a known sweep lineage; "
            f"expected one of {[list(shape) for shape in SCALING_THREAD_POINT_LINEAGES]}"
        )
    # After the shape is known, so the side-car is checked against the sweep
    # its own report declares rather than against the current contract.
    if "rss" in report:
        validate_scaling_rss(report.get("rss"), f"{label}.rss", thread_points)
    comparison_digest(report.get("metric_comparison_key"), f"{label}.metric_comparison_key")
    validate_run(report.get("run"), f"{label}.run")
    if compact:
        digest = string_value(
            report.get("runner_fingerprint_sha256"), f"{label}.runner_fingerprint_sha256"
        )
        if not HEX_64.fullmatch(digest):
            fail(f"{label}.runner_fingerprint_sha256: invalid")
    else:
        if report.get("invalid_reason") is not None:
            fail(f"{label}.invalid_reason: must be null")
        validate_runner(report.get("runner"), f"{label}.runner")
        topology = object_value(report.get("topology"), f"{label}.topology")
        exact_fields(
            topology,
            {"physical_cores", "logical_cores", "allowed_logical_cpus", "affinity_policy"},
            f"{label}.topology",
        )
        int_value(topology.get("allowed_logical_cpus"), f"{label}.topology.allowed_logical_cpus", 1)
    methodology = object_value(report.get("methodology"), f"{label}.methodology")
    exact_fields(methodology, SCALING_METHODOLOGY_FIELDS, f"{label}.methodology")
    if methodology.get("rigor") != SCALING_RIGOR_LABEL:
        fail(f"{label}.methodology.rigor: coverage-mode labeling is mandatory")
    if int_value(methodology.get("blocks_per_cell"), f"{label}.methodology.blocks_per_cell", 1) != (
        SCALING_BLOCKS
    ):
        fail(f"{label}.methodology.blocks_per_cell: protocol fixes {SCALING_BLOCKS} blocks")
    summaries = list_value(report.get("cell_summaries"), f"{label}.cell_summaries")
    expected_cells = len(SCALING_PATTERN_IDS) * len(thread_points) * len(ALLOCATOR_IDS)
    if len(summaries) != expected_cells:
        fail(f"{label}.cell_summaries: expected exactly {expected_cells} cells")
    seen: set[tuple[str, int, str]] = set()
    for index, item in enumerate(summaries):
        summary = object_value(item, f"{label}.cell_summaries[{index}]")
        exact_fields(summary, SCALING_SUMMARY_FIELDS, f"{label}.cell_summaries[{index}]")
        pattern = string_value(summary.get("pattern"), f"{label}.cell_summaries[{index}].pattern")
        allocator = string_value(
            summary.get("allocator_id"), f"{label}.cell_summaries[{index}].allocator_id"
        )
        threads = int_value(
            summary.get("thread_count"), f"{label}.cell_summaries[{index}].thread_count", 1
        )
        if (
            pattern not in SCALING_PATTERN_IDS
            or allocator not in ALLOCATOR_IDS
            or threads not in thread_points
        ):
            fail(f"{label}.cell_summaries[{index}]: undeclared pattern, allocator, or thread point")
        key = (pattern, threads, allocator)
        if key in seen:
            fail(f"{label}.cell_summaries[{index}]: duplicate cell {key}")
        seen.add(key)
        if (
            int_value(summary.get("block_count"), f"{label}.cell_summaries[{index}].block_count", 1)
            != SCALING_BLOCKS
        ):
            fail(f"{label}.cell_summaries[{index}].block_count: {SCALING_BLOCKS} blocks required")
        median = float_value(
            summary.get("median_throughput"), f"{label}.cell_summaries[{index}].median", True
        )
        low = float_value(
            summary.get("min_throughput"), f"{label}.cell_summaries[{index}].min", True
        )
        high = float_value(
            summary.get("max_throughput"), f"{label}.cell_summaries[{index}].max", True
        )
        float_value(
            summary.get("speedup_vs_single_worker"),
            f"{label}.cell_summaries[{index}].speedup",
            True,
        )
        factor = float_value(
            summary.get("oversubscription_factor"),
            f"{label}.cell_summaries[{index}].oversubscription_factor",
            True,
        )
        if low > median or high < median:
            fail(f"{label}.cell_summaries[{index}]: min/median/max are inconsistent")
        if summary.get("oversubscribed") is not (factor > 1.0):
            fail(f"{label}.cell_summaries[{index}].oversubscribed: disagrees with its factor")
    if len(seen) != expected_cells:
        fail(f"{label}.cell_summaries: matrix is incomplete")
    if not compact:
        raw = list_value(report.get("raw_samples"), f"{label}.raw_samples")
        if len(raw) != expected_cells * SCALING_BLOCKS:
            fail(f"{label}.raw_samples: expected {expected_cells * SCALING_BLOCKS} samples")
    return report


# Dark panel palette. Defined once so every scaling facet reads as one system.
SCALING_INK = {
    "background": "#0d1117",
    "plot": "#111823",
    "grid": "#1f2937",
    "axis": "#8b98ad",
    "title": "#e8eef7",
    "muted": "#7d8da5",
    "badge": "#f0b429",
    "oversubscribed": "#1a2230",
}
# One fixed color per allocator, reused identically on every panel.
SCALING_SERIES = {
    "mimalloc-pprof": "#58a6ff",
    "upstream-mimalloc": "#3fb950",
    "bun-mimalloc": "#ff9d5c",
    "tcmalloc": "#e3b341",
    "jemalloc": "#bc8cff",
}


def svg_text(
    x: float,
    y: float,
    value: str,
    *,
    fill: str,
    size: float = 13,
    weight: str = "normal",
    anchor: str = "start",
    family: str = "system-ui,-apple-system,Segoe UI,Roboto,sans-serif",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size:.0f}" '
        f'font-family="{family}" font-weight="{weight}" text-anchor="{anchor}">'
        f"{escaped(value)}</text>"
    )


def axis_unit(ceiling: float) -> tuple[float, str, int]:
    """Pick one unit for the whole y axis. Mixing units between ticks (2k
    above 822) makes a chart unreadable, so the scale is chosen once."""

    if ceiling >= 1e9:
        return 1e9, "G", 2
    if ceiling >= 1e6:
        return 1e6, "M", 1
    if ceiling >= 1e3:
        return 1e3, "k", 1
    return 1.0, "", 0


def format_throughput(value: float, unit: tuple[float, str, int] | None = None) -> str:
    if value == 0:
        return "0"
    divisor, suffix, decimals = unit if unit is not None else axis_unit(value)
    return f"{value / divisor:.{decimals}f}{suffix}"


SCALING_WIDTH = 1000
SCALING_DUAL_WIDTH = 1400
SCALING_HEIGHT = 590
SCALING_TOP = 118
SCALING_BOTTOM = 110
SCALING_LEFT = 92
SCALING_RIGHT = 40
SCALING_PLOT_WIDTH = SCALING_WIDTH - SCALING_LEFT - SCALING_RIGHT
SCALING_DUAL_PLOT_WIDTH = 560
SCALING_DUAL_RSS_LEFT = 712


def scaling_x_of(
    threads: int, left: int, plot_width: int, thread_points: Sequence[int] = SCALING_THREAD_POINTS
) -> float:
    """Log2 spacing: every doubling is one step, so the dense sweep 1/2/3/4/6/8
    places 2/4/8 evenly and the odd points between their neighbors.

    The axis spans the sweep the report being drawn actually declares, so a
    panel rendered from an older lineage is not squeezed against a scale it
    never measured.
    """

    span = math.log2(max(thread_points))
    return left + plot_width * (math.log2(threads) / span if span else 0.0)


def scaling_y_of(value: float, ceiling: float, top: int, plot_height: int) -> float:
    return top + plot_height * (1.0 - value / ceiling)


def _scaling_plot_parts(
    parts: list[str],
    left: int,
    top: int,
    plot_width: int,
    plot_height: int,
    series: Mapping[str, list[tuple[int, float]]],
    ceiling: float,
    allowed: int,
    title: str,
    subtitle: str,
    unit: tuple[float, str, int],
    thread_points: Sequence[int] = SCALING_THREAD_POINTS,
) -> None:
    """Append one dark line plot: x is worker count (log2), y one series per
    allocator, with the oversubscribed band shaded."""

    parts.append(
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        f'fill="{SCALING_INK["plot"]}" rx="6"/>'
    )
    # Shade the oversubscribed region so contention is never read as core scaling.
    oversubscribed = [point for point in thread_points if point > allowed]
    in_budget = [point for point in thread_points if point <= allowed]
    if oversubscribed:
        first_over = scaling_x_of(oversubscribed[0], left, plot_width, thread_points)
        # The first oversubscribed point is usually the last point on the axis,
        # so anchoring the band at it would give it zero width. Start it halfway
        # back to the last in-budget point instead.
        band_start = (
            (scaling_x_of(in_budget[-1], left, plot_width, thread_points) + first_over) / 2
            if in_budget
            else float(left)
        )
        band_width = left + plot_width - band_start
        parts.append(
            f'<rect x="{band_start:.1f}" y="{top}" width="{band_width:.1f}" '
            f'height="{plot_height}" fill="{SCALING_INK["oversubscribed"]}" rx="6"/>'
        )
        parts.append(
            svg_text(
                left + plot_width - 8,
                top + 20,
                f"oversubscribed (> {allowed} vCPU)",
                fill=SCALING_INK["muted"],
                size=12,
                anchor="end",
            )
        )
    for step in range(5):
        value = ceiling * step / 4
        y = scaling_y_of(value, ceiling, top, plot_height)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" '
            f'stroke="{SCALING_INK["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            svg_text(
                left - 12,
                y + 4,
                format_throughput(value, unit),
                fill=SCALING_INK["axis"],
                size=12,
                anchor="end",
            )
        )
    for threads in thread_points:
        x = scaling_x_of(threads, left, plot_width, thread_points)
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" '
            f'stroke="{SCALING_INK["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            svg_text(
                x,
                top + plot_height + 26,
                str(threads),
                fill=SCALING_INK["title"],
                size=13,
                weight="600",
                anchor="middle",
            )
        )
        if threads > allowed:
            parts.append(
                svg_text(
                    x,
                    top + plot_height + 44,
                    f"{threads / allowed:g}x",
                    fill=SCALING_INK["muted"],
                    size=11,
                    anchor="middle",
                )
            )
    for allocator in ALLOCATOR_IDS:
        points = series.get(allocator, [])
        if not points:
            continue
        color = SCALING_SERIES[allocator]
        path = " ".join(
            f"{'M' if index == 0 else 'L'}"
            f"{scaling_x_of(threads, left, plot_width, thread_points):.1f} "
            f"{scaling_y_of(value, ceiling, top, plot_height):.1f}"
            for index, (threads, value) in enumerate(points)
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for threads, value in points:
            parts.append(
                f'<circle cx="{scaling_x_of(threads, left, plot_width, thread_points):.1f}" '
                f'cy="{scaling_y_of(value, ceiling, top, plot_height):.1f}" r="4.5" '
                f'fill="{SCALING_INK["background"]}" stroke="{color}" stroke-width="2.5"/>'
            )
    parts.append(
        svg_text(left - 12, top - 46, title, fill=SCALING_INK["title"], size=20, weight="600")
    )
    parts.append(svg_text(left - 12, top - 24, subtitle, fill=SCALING_INK["muted"], size=13))


def scaling_svg(scaling: Mapping[str, object], pattern: str) -> bytes:
    """One dark chart per pattern. Without RSS data: throughput vs worker
    count. With the RSS side-car: throughput and peak-RSS panels side by side
    so per-thread-cache footprint growth reads against the throughput curve."""

    height = SCALING_HEIGHT
    top, bottom = SCALING_TOP, SCALING_BOTTOM
    plot_height = height - top - bottom
    # The report's own sweep, not the current contract: a published row from an
    # older lineage keeps rendering on the axis it was measured against.
    thread_points = tuple(
        int_value(item, "scaling thread point", 1)
        for item in list_value(scaling["thread_points"], "scaling thread points")
    )
    summaries = [
        object_value(item, "scaling cell summary")
        for item in list_value(scaling["cell_summaries"], "scaling cell summaries")
    ]
    series: dict[str, list[tuple[int, float]]] = {}
    for summary in summaries:
        if summary.get("pattern") != pattern:
            continue
        allocator = cast(str, summary["allocator_id"])
        series.setdefault(allocator, []).append(
            (
                int(cast(int, summary["thread_count"])),
                float(cast(float, summary["median_throughput"])),
            )
        )
    for points in series.values():
        points.sort()
    peak = max(
        (value for points in series.values() for _threads, value in points),
        default=1.0,
    )
    ceiling = peak * 1.12
    topology = object_value(scaling.get("topology", {}), "scaling topology")
    allowed = int(cast(int, topology.get("allowed_logical_cpus", 1)) or 1)
    unit = axis_unit(ceiling)
    rss = scaling.get("rss")
    if rss is not None:
        rss_report = validate_scaling_rss(rss, "scaling rss", thread_points)
        rss_series: dict[str, list[tuple[int, float]]] = {}
        for summary_value in list_value(rss_report["cell_summaries"], "scaling rss summaries"):
            summary = object_value(summary_value, "scaling rss summary")
            if summary.get("pattern") != pattern:
                continue
            allocator = cast(str, summary["allocator_id"])
            rss_series.setdefault(allocator, []).append(
                (
                    int(cast(int, summary["thread_count"])),
                    float(cast(int, summary["median_peak_rss_bytes"])),
                )
            )
        for points in rss_series.values():
            points.sort()
        rss_peak = max(
            (value for points in rss_series.values() for _threads, value in points),
            default=1.0,
        )
        rss_ceiling = rss_peak * 1.12
        rss_unit = axis_unit(rss_ceiling)
        width = SCALING_DUAL_WIDTH
        parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img">',
            f'<rect width="{width}" height="{height}" fill="{SCALING_INK["background"]}"/>',
        ]
        _scaling_plot_parts(
            parts,
            SCALING_LEFT,
            top,
            SCALING_DUAL_PLOT_WIDTH,
            plot_height,
            series,
            ceiling,
            allowed,
            SCALING_PANEL_TITLES.get(pattern, pattern),
            "aggregate throughput (operations/second) by worker thread count",
            unit,
            thread_points,
        )
        _scaling_plot_parts(
            parts,
            SCALING_DUAL_RSS_LEFT,
            top,
            SCALING_DUAL_PLOT_WIDTH,
            plot_height,
            rss_series,
            rss_ceiling,
            allowed,
            "peak RSS by worker count",
            "external smaps_rollup peak during the measured block; lower is better",
            rss_unit,
            thread_points,
        )
    else:
        width = SCALING_WIDTH
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img">',
            f'<rect width="{width}" height="{height}" fill="{SCALING_INK["background"]}"/>',
        ]
        _scaling_plot_parts(
            parts,
            SCALING_LEFT,
            top,
            SCALING_PLOT_WIDTH,
            plot_height,
            series,
            ceiling,
            allowed,
            SCALING_PANEL_TITLES.get(pattern, pattern),
            "aggregate throughput (operations/second) by worker thread count",
            unit,
            thread_points,
        )
    legend_x = SCALING_LEFT - 12
    for allocator in ALLOCATOR_IDS:
        if allocator not in series:
            continue
        color = SCALING_SERIES[allocator]
        parts.append(
            f'<rect x="{legend_x:.1f}" y="{height - 54}" width="22" height="4" rx="2" fill="{color}"/>'
        )
        parts.append(
            svg_text(legend_x + 30, height - 47, allocator, fill=SCALING_INK["axis"], size=12)
        )
        legend_x += 34 + 7.4 * len(allocator)
    parts.append(
        svg_text(
            SCALING_LEFT - 12,
            height - 16,
            f"{SCALING_RIGOR_LABEL} | median of {SCALING_BLOCKS} paired blocks | "
            f"runner {topology.get('physical_cores', '?')}P/{topology.get('logical_cores', '?')}L "
            f"| informational",
            fill=SCALING_INK["muted"],
            size=11,
        )
    )
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def pending_scaling_svg(pattern: str, reason: str) -> bytes:
    """Dark placeholder in the same visual system, carrying no numbers."""

    width, height = 1000, 560
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">',
        f'<rect width="{width}" height="{height}" fill="{SCALING_INK["background"]}"/>',
        f'<rect x="60" y="60" width="{width - 120}" height="{height - 120}" '
        f'fill="{SCALING_INK["plot"]}" rx="10"/>',
        f'<rect x="60" y="60" width="{width - 120}" height="6" rx="3" fill="{SCALING_INK["badge"]}"/>',
        svg_text(
            80,
            140,
            SCALING_PANEL_TITLES.get(pattern, pattern),
            fill=SCALING_INK["title"],
            size=22,
            weight="600",
        ),
        svg_text(80, 178, "pending", fill=SCALING_INK["badge"], size=15, weight="600"),
        svg_text(80, 214, reason, fill=SCALING_INK["muted"], size=13),
        svg_text(
            80,
            height - 100,
            "No measured values are shown until the first validated sweep publishes.",
            fill=SCALING_INK["muted"],
            size=13,
        ),
        "</svg>",
    ]
    return ("\n".join(parts) + "\n").encode("utf-8")


def carry_forward_optional_metrics(latest: dict[str, object], prior: dict[str, object]) -> bool:
    validate_latest(prior, "prior latest")
    carried = False
    for metric in ("memory", "latency", "scaling"):
        if metric not in latest and metric in prior:
            latest[metric] = copy.deepcopy(prior[metric])
            pending = list_value(latest["pending_metrics"], "latest.pending_metrics")
            latest["pending_metrics"] = [
                value
                for value in pending
                if object_value(value, "latest pending metric").get("metric_id") != metric
            ]
            carried = True
    return carried


def escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_html(latest: Mapping[str, object]) -> bytes:
    run = object_value(latest["run"], "run")
    runner = object_value(latest["runner"], "runner")
    absolute = [
        validate_absolute(value, "absolute")
        for value in list_value(latest["absolute_summaries"], "absolute")
    ]
    paired = [
        validate_paired(value, "paired")
        for value in list_value(latest["paired_summaries"], "paired")
    ]
    allocator_rows = "".join(
        f"<tr><td>{escaped(value['allocator_id'])}</td><td><code>{escaped(value['source_sha'])}</code></td><td><code>{escaped(value['child_binary_sha256'])}</code></td></tr>"
        for value in cast(list[dict[str, object]], latest["allocators"])
    )
    absolute_rows = "".join(
        f"<tr><td>{escaped(value['scenario_id'])}</td><td>{escaped(value['thread_point'])}</td><td>{escaped(value['allocator_id'])}</td><td>{escaped(object_value(value['summary'], 'summary')['median'])}</td><td>{'yes' if object_value(value['summary'], 'summary')['noisy'] else 'no'}</td></tr>"
        for value in absolute
    )
    paired_rows = "".join(
        f"<tr><td>{escaped(value['scenario_id'])}</td><td>{escaped(object_value(value['summary'], 'summary')['candidate_id'])}</td><td>{escaped(object_value(value['summary'], 'summary')['effect'])}</td><td>{escaped(object_value(object_value(value['summary'], 'summary')['confidence_interval'], 'interval')['lower'])}&ndash;{escaped(object_value(object_value(value['summary'], 'summary')['confidence_interval'], 'interval')['upper'])}</td><td>informational</td></tr>"
        for value in paired
    )
    pending = cast(list[dict[str, object]], latest["pending_metrics"])
    pending_names = {
        "memory": "benchmark-memory.png",
        "latency": "benchmark-latency.png",
        # The scaling metric publishes one panel per pattern; its pending card
        # previews the first of them.
        "scaling": SCALING_PANELS[SCALING_PATTERN_IDS[0]],
        "pprof-tax": "benchmark-pprof-tax.png",
    }
    pending_html = "".join(
        f"<article class='pending'><img src='{pending_names[cast(str, item['metric_id'])]}' alt='{escaped(item['metric_id'])} pending: {escaped(item['reason'])}'><h3>{escaped(item['metric_id'])}: pending</h3><p>{escaped(item['reason'])}</p><a href='{escaped(item['phase_issue_url'])}'>Phase issue</a></article>"
        for item in pending
    )
    memory_html = ""
    if "memory" in latest:
        memory = validate_memory_report(latest["memory"], "latest.memory")
        memory_run = object_value(memory["run"], "latest.memory.run")
        memory_runner = object_value(memory["runner"], "latest.memory.runner")
        memory_absolute = [
            validate_absolute(value, "memory absolute")
            for value in list_value(memory["absolute_summaries"], "memory absolute summaries")
        ]
        upstream_peaks = {
            (
                cast(str, value["scenario_id"]),
                cast(str, value["thread_point"]),
            ): float_value(
                object_value(value["summary"], "memory summary")["median"],
                "memory upstream median",
                True,
            )
            for value in memory_absolute
            if value["metric_id"] == "sampled-peak-rss-bytes"
            and value["allocator_id"] == "upstream-mimalloc"
        }

        def memory_row(value: Mapping[str, object]) -> str:
            median = float_value(
                object_value(value["summary"], "memory summary")["median"],
                "memory median",
                True,
            )
            display = (
                median
                if value["metric_id"] == "fragmentation-proxy"
                else round(median / (1024 * 1024), 3)
            )
            if value["metric_id"] == "sampled-peak-rss-bytes":
                reference = upstream_peaks[
                    (cast(str, value["scenario_id"]), cast(str, value["thread_point"]))
                ]
                relative = f"{median / reference:.2f}x"
            else:
                relative = "-"
            return (
                f"<tr><td>{escaped(value['scenario_id'])}</td>"
                f"<td>{escaped(value['thread_point'])}</td>"
                f"<td>{escaped(value['metric_id'])}</td>"
                f"<td>{escaped(value['allocator_id'])}</td>"
                f"<td>{escaped(display)}</td><td>{escaped(relative)}</td></tr>"
            )

        memory_rows = "".join(memory_row(value) for value in memory_absolute)
        memory_actions = (
            f"https://github.com/zackees/mimalloc-pprof/actions/runs/{escaped(memory_run['run_id'])}"
            if memory_run["run_origin"] == "github-actions"
            else "https://github.com/zackees/mimalloc-pprof/actions"
        )
        memory_html = f"""<section><h2 id="memory">Linux process memory</h2><img src="benchmark-memory.png" alt="Sampled peak RSS normalized to upstream-mimalloc; 1.0 equals upstream; lower is better"><img src="benchmark-pareto.png" alt="Speed-memory Pareto scatter: fragmentation proxy versus median throughput; upper-left is better"><img src="benchmark-rss-timeline.png" alt="Linux process RSS over time with workload-drained marker and post-drain return-to-OS points; lower is better"><img src="benchmark-fragmentation.png" alt="Fragmentation proxy ratio bars with a 1.0 reference line; lower is better"><p>Externally sampled from <code>/proc/&lt;pid&gt;/smaps_rollup</code> every {escaped(memory["sampling_target_interval_ns"])} ns with VmHWM cross-checks and natural purge only. The bar chart normalizes sampled peak RSS to <code>upstream-mimalloc</code> = 1.0 (matching the throughput panel), with absolute MiB in the table below. The Pareto scatter pairs each allocator's fragmentation proxy with its median throughput on the matching scenario/thread cell; upper-left is better. The timeline shows RSS growth, the workload-drained marker, and the 100 ms / 1 s / 5 s post-drain points so return-to-OS behavior is visible. Runner: {escaped(memory_runner["runner_class"])}; results are informational. Memory run <a href="{memory_actions}">{escaped(memory_run["run_id"])}/{escaped(memory_run["run_attempt"])}</a>; metric key <code>{escaped(memory["metric_comparison_key"])}</code>.</p><table><thead><tr><th>Scenario</th><th>Threads</th><th>Metric</th><th>Allocator</th><th>Median (MiB or ratio)</th><th>vs upstream</th></tr></thead><tbody>{memory_rows}</tbody></table></section>"""
    latency_html = ""
    if "latency" in latest:
        latency = validate_latency_report(latest["latency"], "latest.latency")
        latency_run = object_value(latency["run"], "latest.latency.run")
        latency_runner = object_value(latency["runner"], "latest.latency.runner")
        latency_absolute = [
            object_value(value, "latency absolute")
            for value in list_value(latency["absolute_summaries"], "latency absolute summaries")
        ]
        latency_rows = "".join(
            f"<tr><td>{escaped(value['scenario_id'])}</td><td>{escaped(value['thread_point'])}</td><td>{escaped(value['allocator_id'])}</td><td>{escaped(round(float_value(object_value(value['measured'], 'latency measured')['p50_ns'], 'latency p50'), 3))}</td><td>{escaped(round(float_value(object_value(value['measured'], 'latency measured')['p95_ns'], 'latency p95'), 3))}</td><td>{escaped(round(float_value(object_value(value['measured'], 'latency measured')['p99_ns'], 'latency p99'), 3))}</td><td>control OK ({escaped(object_value(value['control'], 'latency control')['p50_ns'])} ns p50)</td><td>{escaped(object_value(value['measured'], 'latency measured')['count'])}</td></tr>"
            for value in latency_absolute
        )
        first_raw = object_value(
            list_value(latency["raw_samples"], "latency raw samples")[0], "latency raw sample"
        )
        scheduling = object_value(
            object_value(first_raw["measured"], "latency measured")["scheduling"],
            "latency scheduling",
        )
        definitions = object_value(
            object_value(latency["methodology"], "latency methodology")["transaction_boundaries"],
            "latency transaction boundaries",
        )
        latency_actions = (
            f"https://github.com/zackees/mimalloc-pprof/actions/runs/{escaped(latency_run['run_id'])}"
            if latency_run["run_origin"] == "github-actions"
            else "https://github.com/zackees/mimalloc-pprof/actions"
        )
        block_count = min(
            cast(
                int,
                object_value(
                    object_value(value, "latency paired")["summary"],
                    "latency paired summary",
                )["block_count"],
            )
            for value in list_value(latency["paired_summaries"], "latency paired summaries")
        )
        latency_html = f"""<section><h2 id="latency">Transaction latency</h2><img src="benchmark-latency.png" alt="End-to-end transaction latency p99 for all five allocators; lower is better"><p><strong>Lower is better; informational hosted-runner measurements.</strong> These are transaction latencies, never allocator-call latencies and never throughput reciprocals. Local: {escaped(definitions["local"])}. Cross-thread: {escaped(definitions["cross-thread"])}. Large object: {escaped(definitions["large-object"])}.</p><p>Each allocator/cell has at least 10,000 raw samples across {escaped(block_count)} paired blocks. Controls are reported without subtraction. Runner: {escaped(latency_runner["runner_class"])}; affinity: {escaped(scheduling["affinity_policy"])}; upstream reference: <code>upstream-mimalloc</code>. Latency run <a href="{latency_actions}">{escaped(latency_run["run_id"])}/{escaped(latency_run["run_attempt"])}</a>; metric key <code>{escaped(latency["metric_comparison_key"])}</code>.</p><table><thead><tr><th>Scenario</th><th>Threads</th><th>Allocator</th><th>p50 ns</th><th>p95 ns</th><th>p99 ns</th><th>Overhead</th><th>Samples</th></tr></thead><tbody>{latency_rows}</tbody></table></section>"""
    scaling_html = ""
    if "scaling" in latest:
        scaling = validate_scaling_report(latest["scaling"], "latest.scaling")
        scaling_run = object_value(scaling["run"], "latest.scaling.run")
        scaling_topology = object_value(scaling["topology"], "latest.scaling.topology")
        scaling_methodology = object_value(scaling["methodology"], "latest.scaling.methodology")
        # Read from the report, not from the current contract: a published row
        # measured under the earlier sweep must describe its own worker counts.
        scaling_thread_points = [
            int_value(item, "latest.scaling.thread_points", 1)
            for item in list_value(scaling["thread_points"], "latest.scaling.thread_points")
        ]
        scaling_summaries = [
            object_value(value, "scaling summary")
            for value in list_value(scaling["cell_summaries"], "scaling cell summaries")
        ]
        scaling_rows = "".join(
            f"<tr><td>{escaped(value['pattern'])}</td><td>{escaped(value['thread_count'])}</td>"
            f"<td>{escaped(value['allocator_id'])}</td>"
            f"<td>{escaped(round(float_value(value['median_throughput'], 'scaling median'), 1))}</td>"
            f"<td>{escaped(round(float_value(value['min_throughput'], 'scaling min'), 1))} - "
            f"{escaped(round(float_value(value['max_throughput'], 'scaling max'), 1))}</td>"
            f"<td>{escaped(round(float_value(value['speedup_vs_single_worker'], 'scaling speedup'), 2))}x</td>"
            f"<td>{'yes' if value['oversubscribed'] else 'no'}</td></tr>"
            for value in scaling_summaries
        )
        scaling_images = "".join(
            f'<img src="{name}" alt="Aggregate throughput by worker count for the '
            f'{escaped(SCALING_PANEL_TITLES[pattern])} pattern, one line per allocator">'
            for pattern, name in SCALING_PANELS.items()
        )
        scaling_actions = (
            f"https://github.com/zackees/mimalloc-pprof/actions/runs/{escaped(scaling_run['run_id'])}"
            if scaling_run["run_origin"] == "github-actions"
            else "https://github.com/zackees/mimalloc-pprof/actions"
        )
        scaling_html = f"""<section><h2 id="scaling">Thread scaling (sparse sweep)</h2>{scaling_images}<p><strong>{escaped(SCALING_RIGOR_LABEL)}.</strong> These panels trade statistical rigor for thread coverage: {escaped(SCALING_BLOCKS)} blocks per cell, median with min/max, and deliberately no confidence intervals or noise gating. Do not read them as headline-grade numbers.</p><p>Worker counts are literal {escaped(", ".join(str(point) for point in scaling_thread_points))}; the runner allows {escaped(scaling_topology["allowed_logical_cpus"])} logical CPUs, so higher points are oversubscribed and describe contention rather than core scaling. {escaped(scaling_methodology["seed_chain"])}. Scaling run <a href="{scaling_actions}">{escaped(scaling_run["run_id"])}/{escaped(scaling_run["run_attempt"])}</a> measured mimalloc-pprof at source <code>{escaped(scaling_run["source_sha"])}</code>, which is not necessarily the commit above; metric key <code>{escaped(scaling["metric_comparison_key"])}</code>.</p><table><thead><tr><th>Pattern</th><th>Workers</th><th>Allocator</th><th>Median ops/s</th><th>Min - max</th><th>Speedup vs 1</th><th>Oversubscribed</th></tr></thead><tbody>{scaling_rows}</tbody></table></section>"""
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>mimalloc allocator benchmarks</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1200px;margin:auto;padding:24px;color:#182334}}table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border:1px solid #ccd4dd;padding:7px;text-align:left}}img{{max-width:100%;height:auto}}.pending{{border:1px solid #ccd4dd;padding:12px;margin:12px 0}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap}}small{{color:#596575}}</style></head><body>
<h1>Allocator benchmark report</h1><p>Suite <code>{escaped(latest["suite_version"])}</code>; source <code>{escaped(run["source_sha"])}</code>; runner {escaped(runner["runner_class"])}. Intervals are informational.</p>
<nav><a href="latest.json">validated latest data</a> · <a href="history.jsonl">compact history</a></nav>
<h2 id="throughput">Throughput</h2><img src="benchmark-throughput.png" alt="Per-scenario absolute throughput bars for all five allocators"><table><thead><tr><th>Scenario</th><th>Threads</th><th>Allocator</th><th>Median</th><th>Noisy</th></tr></thead><tbody>{absolute_rows}</tbody></table>
{memory_html}<h2>Paired effects</h2><table><thead><tr><th>Scenario</th><th>Candidate</th><th>Effect</th><th>95% interval</th><th>Interpretation</th></tr></thead><tbody>{paired_rows}</tbody></table>
<h2 id="history">Compatible history</h2><img src="benchmark-history.png" alt="History connected only across the selected identical comparison key">
{latency_html}{scaling_html}<h2>Pending Phase 6 panels</h2>{pending_html}<section id="phase-6"><p>Pending panels contain no measured values.</p></section>
<h2>Provenance</h2><p>Run {escaped(run["run_id"])}, attempt {escaped(run["run_attempt"])}; target {escaped(runner["target"])}; fingerprint <code>{escaped(runner["fingerprint_sha256"])}</code>.</p><table><thead><tr><th>Allocator</th><th>Source</th><th>Binary</th></tr></thead><tbody>{allocator_rows}</tbody></table><p><a href="{escaped(latest["actions_run_url"])}">Actions run</a></p>
<h2>Methodology</h2><pre>{escaped(json.dumps(latest["methodology"], sort_keys=True, ensure_ascii=False))}</pre><h2>Reproduce</h2><pre><code>{escaped(latest["reproduction_command"])}</code></pre>
</body></html>"""
    return document.encode("utf-8")


def manifest_for(site: Path) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for name in sorted(SITE_FILES - {"manifest.json"}):
        path = site / name
        entries.append(
            {
                "path": name,
                "size": path.stat().st_size,
                "sha256": sha256(path),
                "media_type": MEDIA_TYPES[name],
                "role": ROLES[name],
            }
        )
    return {"schema_version": "benchmark-site-manifest-v1", "files": entries}


def ensure_empty_output(path: Path) -> None:
    if path.is_symlink():
        fail(f"{path}: output directory may not be a symlink")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            fail(f"{path}: output directory must be new or empty")
    else:
        path.mkdir(parents=True)


def render(
    input_path: Path,
    history_path: Path,
    output: Path,
    digest_out: Path,
    initialize: bool,
    prior_latest_path: Path | None = None,
) -> str:
    latest = read_json(input_path)
    validate_latest(latest, str(input_path))
    carried_optional_metrics = False
    if prior_latest_path is not None:
        prior = read_json(prior_latest_path)
        carried_optional_metrics = carry_forward_optional_metrics(latest, prior)
        validate_latest(latest, str(input_path))
    rows = read_history(history_path, initialize)
    rows = merge_history(
        rows,
        history_row(latest, include_optional_metrics=not carried_optional_metrics),
    )
    output_resolved = output.resolve()
    digest_resolved = digest_out.resolve()
    try:
        digest_resolved.relative_to(output_resolved)
    except ValueError:
        pass
    else:
        fail(f"{digest_out}: detached digest must be outside the site tree")
    ensure_empty_output(output)
    (output / ".nojekyll").write_bytes(b"")
    (output / "latest.json").write_bytes(compact_json(latest))
    (output / "history.jsonl").write_bytes(b"".join(compact_json(row) for row in rows))
    (output / "index.html").write_bytes(render_html(latest))
    (output / "benchmark-throughput.png").write_bytes(throughput_png(latest))
    key = comparison_digest(latest["comparison_key"], "comparison_key")
    (output / "benchmark-history.png").write_bytes(history_png(rows, key))
    pending = {
        cast(str, item["metric_id"]): item
        for item in cast(list[dict[str, object]], latest["pending_metrics"])
    }
    image_names = {
        "memory": "benchmark-memory.png",
        "latency": "benchmark-latency.png",
        "pprof-tax": "benchmark-pprof-tax.png",
    }
    if "memory" in latest:
        memory = validate_memory_report(latest["memory"], "latest.memory")
        (output / image_names["memory"]).write_bytes(memory_png(memory))
        (output / "benchmark-pareto.png").write_bytes(pareto_png(latest))
        (output / "benchmark-rss-timeline.png").write_bytes(rss_timeline_png(memory))
        (output / "benchmark-fragmentation.png").write_bytes(fragmentation_png(memory))
    else:
        item = pending["memory"]
        (output / image_names["memory"]).write_bytes(
            pending_png("memory", cast(str, item["reason"]))
        )
        (output / "benchmark-pareto.png").write_bytes(
            pending_png("speed-memory Pareto scatter", cast(str, item["reason"]))
        )
        (output / "benchmark-rss-timeline.png").write_bytes(
            pending_png(
                "RSS timeline with return-to-OS points",
                cast(str, item["reason"]),
                TIMELINE_WIDTH,
                TIMELINE_HEIGHT,
            )
        )
        (output / "benchmark-fragmentation.png").write_bytes(
            pending_png("fragmentation proxy panel", cast(str, item["reason"]))
        )
    if "latency" in latest:
        latency = validate_latency_report(latest["latency"], "latest.latency")
        (output / image_names["latency"]).write_bytes(latency_png(latency))
    else:
        item = pending["latency"]
        (output / image_names["latency"]).write_bytes(
            pending_png("latency", cast(str, item["reason"]))
        )
    if "scaling" in latest:
        scaling = validate_scaling_report(latest["scaling"], "latest.scaling")
        for pattern, name in SCALING_PANELS.items():
            (output / name).write_bytes(scaling_svg(scaling, pattern))
    else:
        reason = cast(str, pending["scaling"]["reason"])
        for pattern, name in SCALING_PANELS.items():
            (output / name).write_bytes(pending_scaling_svg(pattern, reason))
    item = pending["pprof-tax"]
    (output / image_names["pprof-tax"]).write_bytes(
        pending_png("pprof-tax", cast(str, item["reason"]))
    )
    (output / "manifest.json").write_bytes(compact_json(manifest_for(output)))
    digest = sha256(output / "manifest.json")
    digest_out.parent.mkdir(parents=True, exist_ok=True)
    digest_out.write_text(digest + "\n", encoding="ascii", newline="\n")
    validate_site(output, digest_out)
    return digest


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        fail(f"{path}: invalid PNG signature/IHDR")
    return struct.unpack(">II", data[16:24])


def validate_svg(path: Path) -> None:
    """Panels must be self-contained and inert: they are served from a raw
    branch URL and embedded in the README, so no script, no external fetch,
    and no interactive handlers may survive."""

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        fail(f"{path}: unreadable SVG: {error}")
    if not source.lstrip().startswith("<svg"):
        fail(f"{path}: SVG must begin with an <svg> root element")
    if "viewBox=" not in source:
        fail(f"{path}: SVG needs a viewBox so it scales in the README")
    for forbidden in ("<script", "<foreignObject", "xlink:href", "<image", "@import"):
        if forbidden in source:
            fail(f"{path}: forbidden SVG construct {forbidden!r}")
    if re.search(r"\bon[a-z]+\s*=", source, re.IGNORECASE):
        fail(f"{path}: inline event handlers are forbidden")
    if re.search(r"https?://(?!www\.w3\.org/)", source):
        fail(f"{path}: remote references are forbidden")


def validate_html_links(site: Path) -> None:
    path = site / "index.html"
    source = path.read_text(encoding="utf-8")
    if re.search(r"\bsrc=[\"']https?://", source, re.IGNORECASE) or re.search(
        r"(?:<link[^>]+href=[\"']https?://|@import\s+.*https?://|url\([^)]*https?://)",
        source,
        re.IGNORECASE,
    ):
        fail(f"{path}: remote assets are forbidden")
    for tag in re.findall(r"<img\b[^>]*>", source, re.IGNORECASE):
        if not re.search(r"\balt=[\"'][^\"']+[\"']", tag, re.IGNORECASE):
            fail(f"{path}: every image needs non-empty alt text")
    for target in re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", source, re.IGNORECASE):
        if target.startswith(("https://", "http://", "#")):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0]
        relative = PurePosixPath(clean)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not (site / Path(*relative.parts)).is_file()
        ):
            fail(f"{path}: broken or unsafe local link {target!r}")


def validate_site(site: Path, detached: Path | None = None) -> None:
    if site.is_symlink() or not site.is_dir():
        fail(f"{site}: site must be a real directory")
    actual: set[str] = set()
    # os.walk includes dotfiles unconditionally; rglob("*") may not on every Python.
    for dirpath_str, dirnames, filenames in os.walk(site):
        dirpath = Path(dirpath_str)
        for name in dirnames:
            path = dirpath / name
            if path.is_symlink():
                fail(f"{path}: symlinks are forbidden")
            fail(f"{path}: nested directories are forbidden")
        for name in filenames:
            path = dirpath / name
            if path.is_symlink():
                fail(f"{path}: symlinks are forbidden")
            if not path.is_file():
                fail(f"{path}: special entries are forbidden")
            actual.add(path.relative_to(site).as_posix())
    if actual != SITE_FILES:
        fail(
            f"{site}: site allowlist mismatch; missing={sorted(SITE_FILES - actual)} unexpected={sorted(actual - SITE_FILES)}"
        )
    for name in SITE_FILES:
        size = (site / name).stat().st_size
        cap = FILE_CAPS[name]
        if size > cap or (cap == 0 and size != 0):
            fail(f"{site / name}: size {size} exceeds cap {cap}")
    manifest = read_json(site / "manifest.json")
    exact_fields(manifest, {"schema_version", "files"}, str(site / "manifest.json"))
    if manifest.get("schema_version") != "benchmark-site-manifest-v1":
        fail(f"{site / 'manifest.json'}: incompatible schema")
    entries = list_value(manifest.get("files"), "manifest.files")
    expected_payload = SITE_FILES - {"manifest.json"}
    seen: set[str] = set()
    for index, value in enumerate(entries):
        item = object_value(value, f"manifest.files[{index}]")
        exact_fields(
            item, {"path", "size", "sha256", "media_type", "role"}, f"manifest.files[{index}]"
        )
        name = string_value(item.get("path"), f"manifest.files[{index}].path")
        pure = PurePosixPath(name)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in name
            or name not in expected_payload
            or name in seen
        ):
            fail(f"manifest.files[{index}].path: unsafe, duplicate, or unexpected path")
        seen.add(name)
        path = site / name
        if (
            int_value(item.get("size"), f"manifest.files[{index}].size") != path.stat().st_size
            or item.get("sha256") != sha256(path)
            or item.get("media_type") != MEDIA_TYPES[name]
            or item.get("role") != ROLES[name]
        ):
            fail(f"{path}: manifest metadata/digest mismatch")
    if seen != expected_payload:
        fail("manifest.files: payload inventory is incomplete")
    for name, dimensions in PNG_DIMENSIONS.items():
        if png_dimensions(site / name) != dimensions:
            fail(f"{site / name}: unexpected PNG dimensions")
    for name in SVG_FILES:
        validate_svg(site / name)
    validate_latest(read_json(site / "latest.json"), str(site / "latest.json"))
    history_data = (site / "history.jsonl").read_bytes()
    if history_data and not history_data.endswith(b"\n"):
        fail(f"{site / 'history.jsonl'}: final newline required")
    lines = history_data.splitlines()
    if any(not line.strip() for line in lines):
        fail(f"{site / 'history.jsonl'}: blank lines are forbidden")
    rows = [
        validate_history_row(parse_json_bytes(line, f"history:{index}"), f"history:{index}")
        for index, line in enumerate(lines, 1)
    ]
    if len(rows) > 1000:
        fail(f"{site / 'history.jsonl'}: history exceeds 1000 rows")
    reject_duplicate_history(rows, "history.jsonl")
    if rows != sorted(rows, key=history_sort_key):
        fail(f"{site / 'history.jsonl'}: history is not deterministically sorted")
    validate_html_links(site)
    if detached is not None:
        try:
            expected = detached.read_text(encoding="ascii").strip()
        except OSError as error:
            fail(f"{detached}: cannot read detached digest: {error}")
        if not HEX_64.fullmatch(expected) or expected != sha256(site / "manifest.json"):
            fail(f"{detached}: detached manifest digest mismatch")


def git_command(repository: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        fail(f"git {' '.join(arguments)}: cannot execute: {error}")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        fail(f"git {' '.join(arguments)} failed in {repository}: {detail}")
    return completed.stdout


def decode_git_paths(data: bytes, label: str) -> set[str]:
    paths: set[str] = set()
    for raw in data.split(b"\0"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            fail(f"{label}: non-UTF-8 path: {error}")
        if path in paths:
            fail(f"{label}: duplicate path {path!r}")
        paths.add(path)
    return paths


def git_index_files(repository: Path) -> set[str]:
    return decode_git_paths(git_command(repository, "ls-files", "-z"), "git index")


def git_revision_files(repository: Path, revision: str) -> set[str]:
    return decode_git_paths(
        git_command(repository, "ls-tree", "-r", "--name-only", "-z", revision),
        f"git revision {revision}",
    )


def require_exact_files(actual: set[str], label: str) -> None:
    if actual != SITE_FILES:
        fail(
            f"{label} allowlist mismatch; missing={sorted(SITE_FILES - actual)} "
            f"unexpected={sorted(actual - SITE_FILES)}"
        )


def prepare_branch(worktree: Path, site: Path) -> None:
    """Replace a linked worktree's index with exactly the sealed site."""

    validate_site(site)
    worktree = worktree.resolve()
    site = site.resolve()
    if worktree == site:
        fail("publication worktree and site directory must be distinct")
    administrative_file = worktree / ".git"
    if not administrative_file.is_file() or administrative_file.is_symlink():
        fail(f"{administrative_file}: expected linked-worktree administrative file")
    top_level = Path(
        git_command(worktree, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve()
    if top_level != worktree:
        fail(f"{worktree}: not the Git worktree root ({top_level})")
    if git_command(worktree, "status", "--porcelain=v1", "-z"):
        fail(f"{worktree}: publication worktree must start clean")

    git_command(worktree, "rm", "-r", "-f", "--ignore-unmatch", "--", ".")
    leftovers = sorted(path.name for path in worktree.iterdir() if path.name != ".git")
    if leftovers:
        fail(f"{worktree}: unexpected untracked files after git rm: {leftovers}")
    for name in sorted(SITE_FILES):
        shutil.copy2(site / name, worktree / name)
    git_command(worktree, "add", "-A")

    if not administrative_file.is_file():
        fail(f"{administrative_file}: worktree administration was removed")
    require_exact_files(git_index_files(worktree), "staged publication index")
    for name in SITE_FILES:
        if (worktree / name).read_bytes() != (site / name).read_bytes():
            fail(f"{worktree / name}: copied bytes differ from sealed site")


def validate_git_revision(repository: Path, revision: str, site: Path) -> None:
    """Prove a revision contains exactly the sealed site and identical bytes."""

    validate_site(site)
    require_exact_files(git_revision_files(repository, revision), "revision")
    for name in SITE_FILES:
        published = git_command(repository, "show", f"{revision}:{name}")
        if published != (site / name).read_bytes():
            fail(f"{revision}:{name}: bytes differ from sealed site")


def audit_pages(site: Path, page_url: str, attempts: int, delay_seconds: float) -> None:
    """Wait for the deployed public payload to become byte-identical to the sealed site."""

    validate_site(site)
    parsed = urllib.parse.urlparse(page_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        fail(f"{page_url!r}: expected a plain HTTPS Pages base URL")
    if attempts < 1 or delay_seconds < 0:
        fail("Pages audit attempts must be positive and delay non-negative")
    base = page_url.rstrip("/") + "/"
    version = sha256(site / "manifest.json")
    public_files = SITE_FILES - {".nojekyll"}
    last_error = "Pages payload did not match"
    for attempt in range(1, attempts + 1):
        try:
            for name in sorted(public_files):
                url = urllib.parse.urljoin(base, name) + f"?manifest={version}"
                request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read(FILE_CAPS[name] + 1)
                if data != (site / name).read_bytes():
                    raise ReportError(f"{url}: deployed bytes differ from sealed site")
            return
        except (OSError, urllib.error.URLError, ReportError) as error:
            last_error = str(error)
            if attempt < attempts:
                time.sleep(delay_seconds)
    fail(f"Pages audit failed after {attempts} attempts: {last_error}")


def assert_fixture_determinism(
    input_path: Path,
    history_path: Path,
    rendered_site: Path,
    rendered_digest: Path,
    initialize: bool,
    prior_latest_path: Path | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="benchmark-report-fixture-repeat-") as temporary:
        root = Path(temporary)
        repeated_site = root / "site"
        repeated_digest = root / "manifest.sha256"
        render(
            input_path,
            history_path,
            repeated_site,
            repeated_digest,
            initialize,
            prior_latest_path,
        )
        for name in SITE_FILES:
            if (rendered_site / name).read_bytes() != (repeated_site / name).read_bytes():
                fail(f"fixture mode: nondeterministic output {name}")
        if rendered_digest.read_bytes() != repeated_digest.read_bytes():
            fail("fixture mode: nondeterministic detached manifest digest")


def selftest() -> int:
    fixture = Path(__file__).parent / "tests" / "fixtures" / "benchmark"
    with tempfile.TemporaryDirectory(prefix="benchmark-report-selftest-") as temporary:
        root = Path(temporary)
        digest = render(
            fixture / "latest.json",
            fixture / "history.jsonl",
            root / "site",
            root / "manifest.sha256",
            False,
        )
        if not HEX_64.fullmatch(digest):
            fail("selftest: renderer did not produce a detached digest")
        assert_fixture_determinism(
            fixture / "latest.json",
            fixture / "history.jsonl",
            root / "site",
            root / "manifest.sha256",
            False,
        )
    print(
        "PASS benchmark report selftest: validated, rendered, manifested, and sealed fixture site"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--input", type=Path, required=True)
    render_parser.add_argument("--history-in", type=Path, required=True)
    render_parser.add_argument("--output-dir", type=Path, required=True)
    render_parser.add_argument("--detached-digest-out", type=Path, required=True)
    render_parser.add_argument("--initialize-history", action="store_true")
    render_parser.add_argument(
        "--prior-latest",
        type=Path,
        help="carry forward validated optional metrics from the prior public latest.json",
    )
    render_parser.add_argument(
        "--fixture-mode",
        action="store_true",
        help="assert deterministic fixture workflow (rendering is always deterministic)",
    )
    validate_parser = subparsers.add_parser("validate-site")
    validate_parser.add_argument("--site-dir", type=Path, required=True)
    validate_parser.add_argument("--detached-digest", type=Path)
    prepare_parser = subparsers.add_parser("prepare-branch")
    prepare_parser.add_argument("--worktree", type=Path, required=True)
    prepare_parser.add_argument("--site-dir", type=Path, required=True)
    revision_parser = subparsers.add_parser("validate-revision")
    revision_parser.add_argument("--repository", type=Path, required=True)
    revision_parser.add_argument("--revision", required=True)
    revision_parser.add_argument("--site-dir", type=Path, required=True)
    pages_parser = subparsers.add_parser("audit-pages")
    pages_parser.add_argument("--site-dir", type=Path, required=True)
    pages_parser.add_argument("--page-url", required=True)
    pages_parser.add_argument("--attempts", type=int, default=12)
    pages_parser.add_argument("--delay-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.selftest:
        if args.command is not None:
            parser.error("--selftest cannot be combined with a subcommand")
        return selftest()
    if args.command == "render":
        digest = render(
            args.input,
            args.history_in,
            args.output_dir,
            args.detached_digest_out,
            args.initialize_history,
            args.prior_latest,
        )
        if args.fixture_mode:
            assert_fixture_determinism(
                args.input,
                args.history_in,
                args.output_dir,
                args.detached_digest_out,
                args.initialize_history,
                args.prior_latest,
            )
        print(f"PASS rendered and sealed {args.output_dir}; manifest sha256={digest}")
        return 0
    if args.command == "validate-site":
        validate_site(args.site_dir, args.detached_digest)
        print(f"PASS validated sealed benchmark site {args.site_dir}")
        return 0
    if args.command == "prepare-branch":
        prepare_branch(args.worktree, args.site_dir)
        print(f"PASS prepared exact benchmark branch index in {args.worktree}")
        return 0
    if args.command == "validate-revision":
        validate_git_revision(args.repository, args.revision, args.site_dir)
        print(f"PASS validated {args.revision} against sealed benchmark site")
        return 0
    if args.command == "audit-pages":
        audit_pages(args.site_dir, args.page_url, args.attempts, args.delay_seconds)
        print(f"PASS Pages payload matches sealed benchmark site at {args.page_url}")
        return 0
    parser.error(
        "choose --selftest, render, validate-site, prepare-branch, validate-revision, or audit-pages"
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
