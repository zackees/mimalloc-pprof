#!/usr/bin/env python3
"""Render and seal validator-approved mimalloc benchmark reports.

The renderer deliberately owns no benchmark statistics.  It accepts only the
strict ``benchmark-latest-v1`` publication envelope emitted by the Rust
validator and rejects raw or aggregate-only inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import struct
import sys
import tempfile
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
    "benchmark-memory.png": "pending-memory-panel",
    "benchmark-latency.png": "pending-latency-panel",
    "benchmark-scaling.png": "pending-scaling-panel",
    "benchmark-pprof-tax.png": "pending-pprof-tax-panel",
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


def validate_latest(latest: dict[str, object], label: str) -> None:
    exact_fields(latest, TOP_LEVEL_FIELDS, label)
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
    pending = list_value(latest.get("pending_metrics"), f"{label}.pending_metrics")
    if len(pending) != 4:
        fail(f"{label}.pending_metrics: expected four explicit pending metrics")
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
    if tuple(object_value(item, "pending")["metric_id"] for item in pending) != (
        "memory",
        "latency",
        "scaling",
        "pprof-tax",
    ):
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


def history_row(latest: dict[str, object]) -> dict[str, object]:
    allocators = [
        object_value(value, "latest.allocators")
        for value in list_value(latest["allocators"], "latest.allocators")
    ]
    return {
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


def validate_history_row(value: object, label: str) -> dict[str, object]:
    row = object_value(value, label)
    exact_fields(row, HISTORY_FIELDS, label)
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
    combined = [*rows, current]
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
    pending_names = [
        "benchmark-memory.png",
        "benchmark-latency.png",
        "benchmark-scaling.png",
        "benchmark-pprof-tax.png",
    ]
    pending_html = "".join(
        f"<article class='pending'><img src='{name}' alt='{escaped(item['metric_id'])} pending: {escaped(item['reason'])}'><h3>{escaped(item['metric_id'])}: pending</h3><p>{escaped(item['reason'])}</p><a href='{escaped(item['phase_issue_url'])}'>Phase issue</a></article>"
        for name, item in zip(pending_names, pending)
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>mimalloc allocator benchmarks</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1200px;margin:auto;padding:24px;color:#182334}}table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border:1px solid #ccd4dd;padding:7px;text-align:left}}img{{max-width:100%;height:auto}}.pending{{border:1px solid #ccd4dd;padding:12px;margin:12px 0}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap}}small{{color:#596575}}</style></head><body>
<h1>Allocator benchmark report</h1><p>Suite <code>{escaped(latest["suite_version"])}</code>; source <code>{escaped(run["source_sha"])}</code>; runner {escaped(runner["runner_class"])}. Intervals are informational.</p>
<nav><a href="latest.json">validated latest data</a> · <a href="history.jsonl">compact history</a></nav>
<h2>Throughput</h2><img src="benchmark-throughput.png" alt="Per-scenario absolute throughput bars for all four allocators"><table><thead><tr><th>Scenario</th><th>Threads</th><th>Allocator</th><th>Median</th><th>Noisy</th></tr></thead><tbody>{absolute_rows}</tbody></table>
<h2>Paired effects</h2><table><thead><tr><th>Scenario</th><th>Candidate</th><th>Effect</th><th>95% interval</th><th>Interpretation</th></tr></thead><tbody>{paired_rows}</tbody></table>
<h2>Compatible history</h2><img src="benchmark-history.png" alt="History connected only across the selected identical comparison key">
<h2>Pending Phase 6 panels</h2>{pending_html}<section id="phase-6"><p>Pending panels contain no measured values.</p></section>
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
    input_path: Path, history_path: Path, output: Path, digest_out: Path, initialize: bool
) -> str:
    latest = read_json(input_path)
    validate_latest(latest, str(input_path))
    rows = read_history(history_path, initialize)
    rows = merge_history(rows, history_row(latest))
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
    pending = cast(list[dict[str, object]], latest["pending_metrics"])
    for name, item in zip(
        (
            "benchmark-memory.png",
            "benchmark-latency.png",
            "benchmark-scaling.png",
            "benchmark-pprof-tax.png",
        ),
        pending,
    ):
        (output / name).write_bytes(
            pending_png(cast(str, item["metric_id"]), cast(str, item["reason"]))
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
    for path in site.rglob("*"):
        relative = path.relative_to(site).as_posix()
        if path.is_symlink():
            fail(f"{path}: symlinks are forbidden")
        if not path.is_file():
            fail(f"{path}: nested directories and special entries are forbidden")
        actual.add(relative)
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


def assert_fixture_determinism(
    input_path: Path,
    history_path: Path,
    rendered_site: Path,
    rendered_digest: Path,
    initialize: bool,
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
        "--fixture-mode",
        action="store_true",
        help="assert deterministic fixture workflow (rendering is always deterministic)",
    )
    validate_parser = subparsers.add_parser("validate-site")
    validate_parser.add_argument("--site-dir", type=Path, required=True)
    validate_parser.add_argument("--detached-digest", type=Path)
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
        )
        if args.fixture_mode:
            assert_fixture_determinism(
                args.input,
                args.history_in,
                args.output_dir,
                args.detached_digest_out,
                args.initialize_history,
            )
        print(f"PASS rendered and sealed {args.output_dir}; manifest sha256={digest}")
        return 0
    if args.command == "validate-site":
        validate_site(args.site_dir, args.detached_digest)
        print(f"PASS validated sealed benchmark site {args.site_dir}")
        return 0
    parser.error("choose --selftest, render, or validate-site")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
