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
ALLOCATOR_IDS = ("tcmalloc", "jemalloc", "upstream-mimalloc", "mimalloc-pprof")
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
OPTIONAL_TOP_LEVEL_FIELDS = {"memory", "latency"}
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
OPTIONAL_HISTORY_FIELDS = {"memory", "latency"}
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
SITE_FILES = {
    ".nojekyll",
    "index.html",
    "latest.json",
    "manifest.json",
    "history.jsonl",
    "benchmark-throughput.png",
    "benchmark-history.png",
    "benchmark-memory.png",
    "benchmark-latency.png",
    "benchmark-scaling.png",
    "benchmark-pprof-tax.png",
}
PNG_DIMENSIONS = {
    "benchmark-throughput.png": (1280, 720),
    "benchmark-history.png": (1280, 720),
    "benchmark-memory.png": (960, 540),
    "benchmark-latency.png": (960, 540),
    "benchmark-scaling.png": (960, 540),
    "benchmark-pprof-tax.png": (960, 540),
}
FILE_CAPS = {
    ".nojekyll": 0,
    "index.html": 2 * 1024 * 1024,
    "latest.json": 128 * 1024 * 1024,
    "manifest.json": 1024 * 1024,
    "history.jsonl": 32 * 1024 * 1024,
    **dict.fromkeys(PNG_DIMENSIONS, 12 * 1024 * 1024),
}
MEDIA_TYPES = {
    ".nojekyll": "application/octet-stream",
    "index.html": "text/html; charset=utf-8",
    "latest.json": "application/json",
    "history.jsonl": "application/x-ndjson",
    **dict.fromkeys(PNG_DIMENSIONS, "image/png"),
}
ROLES = {
    ".nojekyll": "github-pages-marker",
    "index.html": "report-index",
    "latest.json": "validated-latest-data",
    "history.jsonl": "bounded-compatible-history",
    "benchmark-throughput.png": "throughput-chart",
    "benchmark-history.png": "history-chart",
    "benchmark-memory.png": "memory-panel",
    "benchmark-latency.png": "latency-panel",
    "benchmark-scaling.png": "pending-scaling-panel",
    "benchmark-pprof-tax.png": "pending-pprof-tax-panel",
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
    if ordinal > 3:
        fail(f"{label}.ordinal: expected 0..3")
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
            len(samples) != 4
            or ids != set(ALLOCATOR_IDS)
            or ordinals != {0, 1, 2, 3}
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
    if item.get("quantile") not in ("p50", "p95", "p99"):
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
    if len(absolute) != 20:
        fail(f"{label}.absolute_summaries: expected exact 5x4 matrix")
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
    if len(paired) != 45:
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
        if ordinal > 3:
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
            len(group) != 4
            or {item["allocator_id"] for item in group} != set(ALLOCATOR_IDS)
            or {item["ordinal"] for item in group} != {0, 1, 2, 3}
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
    if len(allocators) != 4:
        fail(f"{label}.allocators: expected exactly four")
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
    pending = list_value(latest.get("pending_metrics"), f"{label}.pending_metrics")
    expected_pending = tuple(
        metric
        for metric, complete in (
            ("memory", memory is not None),
            ("latency", latency is not None),
            ("scaling", False),
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
    if len(allocators) != 4:
        fail(f"{label}.allocator_identities: expected four")
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
    return row


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
        optional = {"memory", "latency"}
        previous_base = {key: value for key, value in row.items() if key not in optional}
        current_base = {key: value for key, value in current.items() if key not in optional}
        gained = (set(current) & optional) - (set(row) & optional)
        if previous_base != current_base or not gained:
            fail("history append: duplicate run may only gain a validated optional metric")
        combined[index] = current
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


COLORS = [(53, 132, 228), (239, 108, 0), (15, 157, 88), (171, 71, 188)]


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
        for index, median in enumerate(medians[:4]):
            canvas.rectangle(
                left + 18, top + 18 + index * 23, int(325 * median / maximum), 14, COLORS[index]
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


def pending_png(metric: str, reason: str) -> bytes:
    canvas = Canvas(960, 540, (248, 250, 252))
    canvas.rectangle(55, 55, 850, 430, (231, 236, 242))
    canvas.rectangle(55, 55, 850, 18, (117, 126, 140))
    canvas.rectangle(170, 205, 620, 95, (255, 193, 7))
    canvas.line(205, 345, 755, 345, (117, 126, 140), 5)
    return encode_png(960, 540, canvas.pixels, f"PENDING {metric}: {reason}")


def memory_png(memory: Mapping[str, object]) -> bytes:
    canvas = Canvas(960, 540, (248, 250, 252))
    canvas.rectangle(0, 0, 960, 62, (24, 35, 52))
    records = [
        validate_absolute(value, "memory absolute summary")
        for value in list_value(memory["absolute_summaries"], "memory absolute summaries")
        if object_value(value, "memory absolute summary").get("metric_id")
        == "sampled-peak-rss-bytes"
    ]
    cells: dict[tuple[str, str], list[dict[str, object]]] = {}
    for record in records:
        key = (cast(str, record["scenario_id"]), cast(str, record["thread_point"]))
        cells.setdefault(key, []).append(record)
    for ordinal, (_key, values) in enumerate(sorted(cells.items())):
        column, row = ordinal % 2, ordinal // 2
        left, top = 45 + column * 460, 85 + row * 112
        canvas.rectangle(left, top, 420, 92, (235, 240, 246))
        medians = [
            float_value(object_value(value["summary"], "summary")["median"], "median", True)
            for value in values
        ]
        maximum = max(medians)
        for index, median in enumerate(medians):
            canvas.rectangle(
                left + 14,
                top + 10 + index * 19,
                max(1, int(380 * median / maximum)),
                11,
                COLORS[index],
            )
    return encode_png(960, 540, canvas.pixels, "Linux process RSS; lower is better")


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
        for index, duration in enumerate(p99_values):
            canvas.rectangle(
                left + 14,
                top + 13 + index * 24,
                max(1, int(380 * duration / maximum)),
                14,
                COLORS[index],
            )
    return encode_png(
        960,
        540,
        canvas.pixels,
        "Transaction latency p99; allocation plus touch/checksum through free; lower is better",
    )


def carry_forward_optional_metrics(latest: dict[str, object], prior: dict[str, object]) -> bool:
    validate_latest(prior, "prior latest")
    carried = False
    for metric in ("memory", "latency"):
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
        "scaling": "benchmark-scaling.png",
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
        memory_rows = "".join(
            f"<tr><td>{escaped(value['scenario_id'])}</td><td>{escaped(value['thread_point'])}</td><td>{escaped(value['metric_id'])}</td><td>{escaped(value['allocator_id'])}</td><td>{escaped(round(float_value(object_value(value['summary'], 'memory summary')['median'], 'memory median') / (1024 * 1024), 3) if value['metric_id'] != 'fragmentation-proxy' else object_value(value['summary'], 'memory summary')['median'])}</td></tr>"
            for value in memory_absolute
        )
        memory_actions = (
            f"https://github.com/zackees/mimalloc-pprof/actions/runs/{escaped(memory_run['run_id'])}"
            if memory_run["run_origin"] == "github-actions"
            else "https://github.com/zackees/mimalloc-pprof/actions"
        )
        memory_html = f"""<section><h2 id="memory">Linux process memory</h2><img src="benchmark-memory.png" alt="Absolute Linux process RSS for all four allocators; lower is better"><p>Externally sampled from <code>/proc/&lt;pid&gt;/smaps_rollup</code> every {escaped(memory["sampling_target_interval_ns"])} ns with VmHWM cross-checks and natural purge only. Runner: {escaped(memory_runner["runner_class"])}; results are informational. Memory run <a href="{memory_actions}">{escaped(memory_run["run_id"])}/{escaped(memory_run["run_attempt"])}</a>; metric key <code>{escaped(memory["metric_comparison_key"])}</code>.</p><table><thead><tr><th>Scenario</th><th>Threads</th><th>Metric</th><th>Allocator</th><th>Median (MiB or ratio)</th></tr></thead><tbody>{memory_rows}</tbody></table></section>"""
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
        latency_html = f"""<section><h2 id="latency">Transaction latency</h2><img src="benchmark-latency.png" alt="End-to-end transaction latency p99 for all four allocators; lower is better"><p><strong>Lower is better; informational hosted-runner measurements.</strong> These are transaction latencies, never allocator-call latencies and never throughput reciprocals. Local: {escaped(definitions["local"])}. Cross-thread: {escaped(definitions["cross-thread"])}. Large object: {escaped(definitions["large-object"])}.</p><p>Each allocator/cell has at least 10,000 raw samples across {escaped(block_count)} paired blocks. Controls are reported without subtraction. Runner: {escaped(latency_runner["runner_class"])}; affinity: {escaped(scheduling["affinity_policy"])}; upstream reference: <code>upstream-mimalloc</code>. Latency run <a href="{latency_actions}">{escaped(latency_run["run_id"])}/{escaped(latency_run["run_attempt"])}</a>; metric key <code>{escaped(latency["metric_comparison_key"])}</code>.</p><table><thead><tr><th>Scenario</th><th>Threads</th><th>Allocator</th><th>p50 ns</th><th>p95 ns</th><th>p99 ns</th><th>Overhead</th><th>Samples</th></tr></thead><tbody>{latency_rows}</tbody></table></section>"""
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>mimalloc allocator benchmarks</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1200px;margin:auto;padding:24px;color:#182334}}table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border:1px solid #ccd4dd;padding:7px;text-align:left}}img{{max-width:100%;height:auto}}.pending{{border:1px solid #ccd4dd;padding:12px;margin:12px 0}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap}}small{{color:#596575}}</style></head><body>
<h1>Allocator benchmark report</h1><p>Suite <code>{escaped(latest["suite_version"])}</code>; source <code>{escaped(run["source_sha"])}</code>; runner {escaped(runner["runner_class"])}. Intervals are informational.</p>
<nav><a href="latest.json">validated latest data</a> · <a href="history.jsonl">compact history</a></nav>
<h2 id="throughput">Throughput</h2><img src="benchmark-throughput.png" alt="Per-scenario absolute throughput bars for all four allocators"><table><thead><tr><th>Scenario</th><th>Threads</th><th>Allocator</th><th>Median</th><th>Noisy</th></tr></thead><tbody>{absolute_rows}</tbody></table>
<h2>Paired effects</h2><table><thead><tr><th>Scenario</th><th>Candidate</th><th>Effect</th><th>95% interval</th><th>Interpretation</th></tr></thead><tbody>{paired_rows}</tbody></table>
<h2 id="history">Compatible history</h2><img src="benchmark-history.png" alt="History connected only across the selected identical comparison key">
{memory_html}{latency_html}<h2>Pending Phase 6 panels</h2>{pending_html}<section id="phase-6"><p>Pending panels contain no measured values.</p></section>
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
        "scaling": "benchmark-scaling.png",
        "pprof-tax": "benchmark-pprof-tax.png",
    }
    if "memory" in latest:
        memory = validate_memory_report(latest["memory"], "latest.memory")
        (output / image_names["memory"]).write_bytes(memory_png(memory))
    else:
        item = pending["memory"]
        (output / image_names["memory"]).write_bytes(
            pending_png("memory", cast(str, item["reason"]))
        )
    if "latency" in latest:
        latency = validate_latency_report(latest["latency"], "latest.latency")
        (output / image_names["latency"]).write_bytes(latency_png(latency))
    else:
        item = pending["latency"]
        (output / image_names["latency"]).write_bytes(
            pending_png("latency", cast(str, item["reason"]))
        )
    for metric in ("scaling", "pprof-tax"):
        item = pending[metric]
        (output / image_names[metric]).write_bytes(pending_png(metric, cast(str, item["reason"])))
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
