#!/usr/bin/env python3
"""Collect opt-in Linux diagnostics around a separate large-span replay.

This deliberately runs the supplied command outside acceptance timing.  It records
tool output rather than guessing when a kernel counter is unavailable.  Cgroup v2
counters are scoped to the current cgroup; /proc/vmstat and THP policy are host
scoped and are never presented as process-attributed deltas.

Example:
  python3 ci/large_span_deep.py --output deep.json --profile /tmp/span \
      --command ./perf_ab 8 1 65536 4194304 40000 300 0 uniform 8 1500
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeAlias, Union, cast

SCHEMA = "large-span-deep-v1"
PERF_EVENTS = (
    "task-clock:u,cycles:u,instructions:u,cache-references:u,cache-misses:u,"
    "dTLB-loads:u,dTLB-load-misses:u,iTLB-loads:u,iTLB-load-misses:u,page-faults:u"
)
CGROUP_FILES = ("memory.current", "memory.peak", "memory.events", "memory.stat", "memory.pressure")


@dataclass(frozen=True)
class CgroupKeySpec:
    filename: str
    keys: tuple[str, ...]


CGROUP_KEY_SPECS = (
    CgroupKeySpec("memory.events", ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")),
    CgroupKeySpec(
        "memory.stat",
        (
            "anon",
            "file",
            "kernel",
            "kernel_stack",
            "pagetables",
            "sock",
            "shmem",
            "file_mapped",
            "file_dirty",
            "file_writeback",
            "pgfault",
            "pgmajfault",
            "pgrefill",
            "pgscan",
            "pgsteal",
            "pgactivate",
            "pgdeactivate",
            "pglazyfree",
            "pglazyfreed",
            "thp_fault_alloc",
            "thp_collapse_alloc",
            "thp_swpout",
            "thp_swpout_fallback",
            "workingset_refault_anon",
            "workingset_refault_file",
            "workingset_activate_anon",
            "workingset_activate_file",
            "workingset_restore_anon",
            "workingset_restore_file",
            "pgscan_kswapd",
            "pgscan_direct",
            "pgsteal_kswapd",
            "pgsteal_direct",
            "pgdemote_kswapd",
            "pgdemote_direct",
            "pgpromote_success",
            "pgpromote_candidate",
        ),
    ),
)
CGROUP_STAT_COUNT_KEYS = {
    "workingset_refault_anon",
    "workingset_refault_file",
    "pgfault",
    "pgmajfault",
    "pgrefill",
    "pgscan",
    "pgsteal",
    "pgactivate",
    "pgdeactivate",
    "pglazyfree",
    "pglazyfreed",
    "thp_fault_alloc",
    "thp_collapse_alloc",
    "thp_swpout",
    "thp_swpout_fallback",
}
VMSTAT_KEYS = (
    "thp_fault_alloc",
    "thp_fault_fallback",
    "thp_collapse_alloc",
    "thp_split_page",
    "thp_split_page_failed",
    "thp_deferred_split_page",
    "nr_anon_pages",
    "nr_file_pages",
)


CounterScalar: TypeAlias = Union[int, float, str, None]


@dataclass
class CounterRecord:
    source: str
    scope: str
    unit: str
    phase: str
    availability: str
    value: CounterScalar
    reason: str | None = None


@dataclass
class CounterGroup:
    source: str
    scope: str
    unit: str
    phase: str
    availability: str
    value: tuple[NamedCounter, ...] = ()
    reason: str | None = None


CounterNode: TypeAlias = Union[CounterRecord, CounterGroup]


@dataclass
class NamedCounter:
    name: str
    counter: CounterNode


@dataclass
class NamedInt:
    name: str
    value: int


@dataclass
class NamedUnit:
    name: str
    unit: str


@dataclass
class NamedProfile:
    name: str
    profile: ProfileRun


@dataclass
class CgroupDiagnostic:
    path: str | None
    before: tuple[NamedCounter, ...]
    after: tuple[NamedCounter, ...]
    delta: tuple[NamedCounter, ...]


@dataclass
class HostMemoryDiagnostic:
    vmstat: tuple[NamedCounter, ...]
    thp_policy: tuple[NamedCounter, ...]


@dataclass
class ToolAvailability:
    path: str | None
    permission_probe: str | None = None


@dataclass
class ToolInventory:
    perf: ToolAvailability
    strace: ToolAvailability


@dataclass
class PerfStatistics:
    events: tuple[NamedCounter, ...]
    raw_stderr: str
    replay: ReplayEvidence | None = None


@dataclass(frozen=True)
class ThreadCredentials:
    uid: int
    euid: int
    gid: int
    egid: int
    cap_eff: str
    cap_amb: str
    valid: bool


@dataclass(frozen=True)
class ReplayEvidence:
    availability: str
    reason: str | None
    completed_operations: int | None
    trace_checksum: str | None
    process: ThreadCredentials | None
    workers: tuple[ThreadCredentials, ...]
    expected_uid: int
    expected_workers: int


@dataclass(frozen=True)
class PerfCollector:
    mode: str
    executable: str | None
    access_report: str | None
    target_scope: str
    startup_coverage: str


@dataclass
class ReplayControl:
    availability: str
    returncode: int | None
    elapsed_ms: float
    stdout: str
    stderr: str
    phase: str
    scope: str
    unit: str
    reason: str | None = None


@dataclass
class ProfileRun:
    availability: str
    path: str | None
    source: str
    scope: str
    unit: str
    phase: str
    returncode: int | None
    raw_stderr: str
    record_elapsed_ms: CounterRecord
    overhead_vs_control_percent: CounterRecord
    report: CounterRecord
    report_scope: str
    report_stderr: str
    reason: str | None = None
    event: str | None = None
    sample_count: int | None = None
    replay: ReplayEvidence | None = None


@dataclass
class ProfileDiagnostic:
    runs: tuple[NamedProfile, ...] = ()
    control: ReplayControl | None = None
    status: CounterRecord | None = None
    note: str | None = None


@dataclass
class ReturnCodes:
    perf_stat: CounterRecord
    strace: CounterRecord


@dataclass
class DeepDiagnosticArtifact:
    schema_version: str
    created_unix_seconds: float
    command: list[str]
    execution_note: str
    cgroup_v2: CgroupDiagnostic
    host_memory: HostMemoryDiagnostic
    tools: ToolInventory
    perf_stat: PerfStatistics
    mapping_syscalls: CounterRecord
    mapping_raw: str
    profiles: ProfileDiagnostic
    replay_return_codes: ReturnCodes
    paired_run: PairedRunLink | None = None
    collector: PerfCollector | None = None


@dataclass
class PairedRunLink:
    candidate_sha: str
    cell: str
    timed_artifact: str


@dataclass
class PairedEventEstimate:
    event: str
    baseline_per_operation: CounterRecord
    candidate_per_operation: CounterRecord
    paired_change_percent: CounterRecord


@dataclass(frozen=True)
class PairReplayIdentity:
    availability: str
    reason: str | None
    baseline_checksum: str | None
    candidate_checksum: str | None
    operations_per_arm: int


@dataclass
class PairedDeepDiagnosticArtifact:
    schema_version: str
    baseline_sha: str
    candidate_sha: str
    cell: str
    operations_per_arm: int
    baseline: DeepDiagnosticArtifact
    candidate: DeepDiagnosticArtifact
    event_estimates: tuple[PairedEventEstimate, ...]
    inference_note: str
    replay_identity: PairReplayIdentity | None = None


def unavailable(
    source: str, scope: str, unit: str, reason: str, phase: str = "diagnostic replay"
) -> CounterRecord:
    return CounterRecord(source, scope, unit, phase, "unavailable", None, reason)


def available(
    source: str, scope: str, unit: str, value: CounterScalar, phase: str = "diagnostic replay"
) -> CounterRecord:
    return CounterRecord(source, scope, unit, phase, "available", value)


def pressure_values(
    text: str,
    source: str = "cgroup v2 memory.pressure",
    scope: str = "current cgroup and descendants",
    phase: str = "snapshot",
) -> CounterGroup:
    values: list[NamedCounter] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        mode, pairs = fields[0], fields[1:]
        row: list[NamedCounter] = []
        for pair in pairs:
            key, separator, raw = pair.partition("=")
            if not separator:
                continue
            try:
                value = int(raw) if key == "total" else float(raw)
            except ValueError:
                continue
            unit = "microseconds" if key == "total" else "percent"
            row.append(NamedCounter(key, available(source, scope, unit, value, phase)))
        if row:
            values_mode = CounterGroup(
                source,
                scope,
                "per-counter; each entry declares its unit",
                phase,
                "available",
                tuple(row),
            )
            values.append(NamedCounter(mode, values_mode))
    return CounterGroup(
        source,
        scope,
        "cgroup memory pressure",
        phase,
        "available",
        tuple(values),
    )


def read_key_values(path: Path, keys: Sequence[str]) -> tuple[NamedInt, ...]:
    values: list[NamedInt] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] in keys:
            try:
                values.append(NamedInt(fields[0], int(fields[1])))
            except ValueError:
                continue
    return tuple(values)


def named_counter(entries: tuple[NamedCounter, ...], name: str) -> CounterNode:
    for entry in entries:
        if entry.name == name:
            return entry.counter
    raise KeyError(name)


def upsert_counter(entries: list[NamedCounter], name: str, counter: CounterNode) -> None:
    for index, entry in enumerate(entries):
        if entry.name == name:
            entries[index] = NamedCounter(name, counter)
            return
    entries.append(NamedCounter(name, counter))


def named_profile(entries: tuple[NamedProfile, ...], name: str) -> ProfileRun:
    for entry in entries:
        if entry.name == name:
            return entry.profile
    raise KeyError(name)


def cgroup_v2_path(
    proc_cgroup: Path = Path("/proc/self/cgroup"), mountinfo: Path = Path("/proc/self/mountinfo")
) -> Path | None:
    try:
        relative = next(
            line.split(":", 2)[2]
            for line in proc_cgroup.read_text().splitlines()
            if line.startswith("0::")
        )
        for line in mountinfo.read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields, fs = left.split(), right.split()
            if fs[0] == "cgroup2":
                mount = Path(fields[4])
                root = Path(fields[3])
                rel = Path(relative)
                try:
                    suffix = rel.relative_to(root)
                except ValueError:
                    suffix = rel.relative_to("/")
                return mount / suffix
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    return None


def counter_group_from_values(
    values: tuple[NamedInt, ...],
    source: str,
    scope: str,
    units: tuple[NamedUnit, ...],
    phase: str,
) -> CounterGroup:
    records: list[NamedCounter] = []
    for item in values:
        unit = next((entry.unit for entry in units if entry.name == item.name), "count")
        records.append(NamedCounter(item.name, available(source, scope, unit, item.value, phase)))
    return CounterGroup(
        source,
        scope,
        "per-counter; each entry declares its unit",
        phase,
        "available",
        tuple(records),
    )


def snapshot_cgroup(path: Path | None, phase: str = "snapshot") -> tuple[NamedCounter, ...]:
    result: list[NamedCounter] = []
    for name in CGROUP_FILES:
        source = f"{path / name}" if path else "/proc/self/cgroup + cgroup2 mount"
        scope = "current cgroup and descendants"
        if path is None:
            result.append(
                NamedCounter(
                    name,
                    unavailable(
                        source,
                        scope,
                        "bytes"
                        if name.startswith("memory.") and name.endswith(("current", "peak"))
                        else "kernel-defined",
                        "cgroup v2 path unavailable",
                        phase,
                    ),
                )
            )
            continue
        file = path / name
        if not file.is_file():
            result.append(
                NamedCounter(
                    name,
                    unavailable(
                        source,
                        scope,
                        "kernel-defined",
                        "counter file is absent (controller or kernel may not expose it)",
                        phase,
                    ),
                )
            )
            continue
        try:
            if name == "memory.pressure":
                counter = pressure_values(file.read_text(encoding="utf-8"), source, scope, phase)
            elif (
                key_spec := next((spec for spec in CGROUP_KEY_SPECS if spec.filename == name), None)
            ) is not None:
                values = read_key_values(file, key_spec.keys)
                if name == "memory.events":
                    value_units = tuple(NamedUnit(item.name, "count") for item in values)
                else:
                    value_units = tuple(
                        NamedUnit(
                            item.name,
                            "pages/events; kernel-defined"
                            if item.name in CGROUP_STAT_COUNT_KEYS
                            else "bytes",
                        )
                        for item in values
                    )
                counter = counter_group_from_values(values, source, scope, value_units, phase)
            else:
                counter = available(source, scope, "bytes", int(file.read_text().strip()), phase)
            result.append(NamedCounter(name, counter))
        except (OSError, ValueError) as error:
            result.append(
                NamedCounter(name, unavailable(source, scope, "kernel-defined", str(error), phase))
            )
    return tuple(result)


def host_memory_metadata(
    proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")
) -> HostMemoryDiagnostic:
    vmstat_path = proc_root / "vmstat"
    vmstat: list[NamedCounter] = []
    for key in VMSTAT_KEYS:
        try:
            value = next(
                (item.value for item in read_key_values(vmstat_path, (key,)) if item.name == key),
                None,
            )
        except OSError as error:
            value = None
            reason = str(error)
        else:
            reason = (
                "host-wide counter is not attributable to this replay"
                if value is not None
                else "counter absent"
            )
        counter = (
            available(
                "/proc/vmstat",
                "host-wide; unattributable on shared hosts",
                "pages/events; kernel-defined",
                value,
            )
            if value is not None
            else unavailable("/proc/vmstat", "host-wide", "pages/events; kernel-defined", reason)
        )
        vmstat.append(NamedCounter(key, counter))
    thp: list[NamedCounter] = []
    policy_dir = sys_root / "kernel/mm/transparent_hugepage"
    try:
        policy_files = [
            *sorted(policy_dir.glob("*/enabled")),
            policy_dir / "enabled",
            policy_dir / "defrag",
        ]
        seen_policy_files: set[Path] = set()
        for file in policy_files:
            if file in seen_policy_files:
                continue
            seen_policy_files.add(file)
            name = str(file.relative_to(sys_root))
            try:
                thp.append(
                    NamedCounter(
                        name, available("sysfs", "host policy", "text", file.read_text().strip())
                    )
                )
            except OSError as error:
                thp.append(
                    NamedCounter(name, unavailable("sysfs", "host policy", "text", str(error)))
                )
    except OSError as error:
        thp.append(NamedCounter("policy", unavailable("sysfs", "host policy", "text", str(error))))
    return HostMemoryDiagnostic(
        tuple(vmstat),
        tuple(thp),
    )


def parse_perf_stat(text: str, returncode: int | None) -> tuple[NamedCounter, ...]:
    parsed: list[NamedCounter] = []
    expected_events = [event.partition(":")[0] for event in PERF_EVENTS.split(",")]
    missing_reason = (
        "permission denied by perf_event_open; see raw output"
        if "access to performance monitoring" in text.lower() or "permission denied" in text.lower()
        else f"perf exited {returncode}; see raw output"
    )
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 3:
            continue
        raw, _, event = fields[:3]
        event = event.strip().split(":", 1)[0]
        if event not in expected_events:
            continue
        raw = raw.strip()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            upsert_counter(
                parsed,
                event,
                unavailable(
                    "perf stat",
                    "command process tree",
                    "event units per perf",
                    raw or "counter unavailable",
                ),
            )
        else:
            upsert_counter(
                parsed,
                event,
                available("perf stat", "command process tree", "event units per perf", value),
            )
    if returncode != 0:
        for event in expected_events:
            if not any(entry.name == event for entry in parsed):
                parsed.append(
                    NamedCounter(
                        event,
                        unavailable(
                            "perf stat",
                            "command process tree",
                            "event units per perf",
                            missing_reason,
                            "perf stat diagnostic replay",
                        ),
                    )
                )
    for event in expected_events:
        if not any(entry.name == event for entry in parsed):
            parsed.append(
                NamedCounter(
                    event,
                    unavailable(
                        "perf stat",
                        "command process tree",
                        "event units per perf",
                        "event missing from perf output",
                        "perf stat diagnostic replay",
                    ),
                )
            )
    for entry in parsed:
        entry.counter.phase = "perf stat diagnostic replay"
    return tuple(parsed)


def _credential(value: object) -> ThreadCredentials:
    if not isinstance(value, Mapping):
        raise ValueError("credential record is not an object")
    row = cast(Mapping[str, object], value)
    numbers = (row.get("uid"), row.get("euid"), row.get("gid"), row.get("egid"))
    if any(type(item) is not int for item in numbers):
        raise ValueError("credential UID/GID is missing or invalid")
    cap_eff, cap_amb, valid = row.get("cap_eff"), row.get("cap_amb"), row.get("valid")
    if not isinstance(cap_eff, str) or not isinstance(cap_amb, str) or type(valid) is not bool:
        raise ValueError("credential capability fields are missing or invalid")
    try:
        int(cap_eff, 16)
        int(cap_amb, 16)
    except ValueError as error:
        raise ValueError("credential capability mask is not hexadecimal") from error
    return ThreadCredentials(
        cast(int, numbers[0]),
        cast(int, numbers[1]),
        cast(int, numbers[2]),
        cast(int, numbers[3]),
        cap_eff,
        cap_amb,
        valid,
    )


def parse_replay_evidence(
    stdout: str, expected_uid: int, expected_workers: int, control_stdout: str = ""
) -> ReplayEvidence:
    def missing(reason: str) -> ReplayEvidence:
        return ReplayEvidence(
            "unavailable", reason, None, None, None, (), expected_uid, expected_workers
        )

    if expected_uid == 0:
        return missing("benchmark runner UID is root")
    if expected_workers <= 0:
        return missing("benchmark worker count is missing or invalid")
    try:
        value = json.loads(stdout)
        if not isinstance(value, Mapping):
            return missing("benchmark child output is not a JSON object")
        data = cast(Mapping[str, object], value)
        count, checksum = data.get("completed_operations"), data.get("trace_checksum")
        if type(count) is not int or not isinstance(checksum, str):
            return missing("benchmark child lacks operation count or trace checksum")
        credentials = data.get("perf_credentials")
        if not isinstance(credentials, Mapping):
            return missing("benchmark child omitted perf credentials")
        credential_data = cast(Mapping[str, object], credentials)
        process = _credential(credential_data.get("process"))
        raw_workers = credential_data.get("workers")
        if not isinstance(raw_workers, list):
            return missing("worker credential list is missing")
        workers = tuple(_credential(item) for item in cast(list[object], raw_workers))
        if len(workers) != expected_workers:
            return missing("worker credential count does not match workload")
        for credential in (process, *workers):
            if not credential.valid:
                return missing("a child or worker credential snapshot failed")
            if credential.uid != expected_uid or credential.euid != expected_uid:
                return missing("a benchmark child or worker ran under a different UID")
            if int(credential.cap_eff, 16) & (1 << 38) or int(credential.cap_amb, 16) & (1 << 38):
                return missing("a benchmark child or worker retained CAP_PERFMON")
        if control_stdout:
            control_value = json.loads(control_stdout)
            if not isinstance(control_value, Mapping):
                return missing("direct replay control is not a JSON object")
            control = cast(Mapping[str, object], control_value)
            if (
                control.get("completed_operations") != count
                or control.get("trace_checksum") != checksum
            ):
                return missing("profiled replay work differs from direct control")
    except (ValueError, TypeError) as error:
        return missing(f"invalid benchmark child evidence: {error}")
    return ReplayEvidence(
        "available",
        None,
        count,
        checksum,
        process,
        workers,
        expected_uid,
        expected_workers,
    )


def run_tool(
    command: list[str], timeout: int = 3600, env: dict[str, str] | None = None
) -> tuple[int | None, str, str]:
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, check=False, env=env
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, "", str(error)


def delta_nodes(before: CounterNode, after: CounterNode, phase: str) -> CounterNode:
    if isinstance(before, CounterGroup) and isinstance(after, CounterGroup):
        if before.availability != "available" or after.availability != "available":
            return CounterGroup(
                before.source,
                before.scope,
                before.unit,
                phase,
                "unavailable",
                (),
                "one or both snapshots unavailable",
            )
        result: list[NamedCounter] = []
        for entry in before.value:
            try:
                after_counter = named_counter(after.value, entry.name)
            except KeyError:
                result.append(
                    NamedCounter(
                        entry.name,
                        unavailable(
                            entry.counter.source,
                            entry.counter.scope,
                            entry.counter.unit,
                            "key missing from after snapshot",
                            phase,
                        ),
                    )
                )
            else:
                result.append(
                    NamedCounter(entry.name, delta_nodes(entry.counter, after_counter, phase))
                )
        for entry in after.value:
            try:
                named_counter(before.value, entry.name)
            except KeyError:
                result.append(
                    NamedCounter(
                        entry.name,
                        unavailable(
                            entry.counter.source,
                            entry.counter.scope,
                            entry.counter.unit,
                            "key missing from before snapshot",
                            phase,
                        ),
                    )
                )
        return CounterGroup(
            before.source,
            before.scope,
            before.unit,
            phase,
            "available",
            tuple(result),
        )
    if isinstance(before, CounterRecord) and isinstance(after, CounterRecord):
        if before.availability != "available" or after.availability != "available":
            return unavailable(
                before.source, before.scope, before.unit, "one or both snapshots unavailable", phase
            )
        if isinstance(before.value, (int, float)) and isinstance(after.value, (int, float)):
            return available(
                "cgroup v2 snapshots", before.scope, before.unit, after.value - before.value, phase
            )
        return unavailable(
            before.source, before.scope, before.unit, "counter is non-numeric", phase
        )
    return unavailable(
        "cgroup v2 snapshots",
        "current cgroup and descendants",
        "kernel-defined",
        "counter changed shape between snapshots",
        phase,
    )


def run_control(command: list[str]) -> ReplayControl:
    started = time.monotonic_ns()
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=3600, check=False)
        return ReplayControl(
            "available",
            result.returncode,
            (time.monotonic_ns() - started) / 1_000_000,
            result.stdout,
            result.stderr,
            "untimed diagnostic replay control",
            "command process tree",
            "milliseconds",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return ReplayControl(
            "unavailable",
            None,
            (time.monotonic_ns() - started) / 1_000_000,
            "",
            str(error),
            "untimed diagnostic replay control",
            "command process tree",
            "milliseconds",
            str(error),
        )


def collect(command: list[str], output: Path, profile: str | None = None) -> DeepDiagnosticArtifact:
    if not command:
        raise ValueError("--command must name a replay executable")
    start_wall = time.time()
    cgpath = cgroup_v2_path()
    before_cgroup = snapshot_cgroup(cgpath, "before diagnostic replays")
    configured_perf = os.environ.get("MIMALLOC_PERF_EXECUTABLE")
    perf_available = configured_perf if configured_perf else shutil.which("perf")
    collector = PerfCollector(
        "private-cap-perfmon" if configured_perf else "unprivileged",
        perf_available,
        os.environ.get("MIMALLOC_PERF_ACCESS_REPORT"),
        "launched benchmark command and its inherited worker threads only",
        "perf launches the child before event counting; no PID-attach startup gap",
    )
    perf_environment = os.environ.copy()
    perf_environment["PERF_AB_PERF_CREDENTIALS"] = "1"
    expected_uid = os.getuid()
    try:
        expected_workers = int(command[1])
    except (IndexError, ValueError):
        expected_workers = 0
    strace_available = shutil.which("strace")
    perf_tool = ToolAvailability(perf_available)
    strace_tool = ToolAvailability(strace_available)
    perf_stat: tuple[NamedCounter, ...] = ()
    perf_raw = ""
    perf_stat_returncode: int | None = None
    perf_replay: ReplayEvidence | None = None
    if perf_available:
        rc, stat_stdout, perf_raw = run_tool(
            [perf_available, "stat", "-x;", "-e", PERF_EVENTS, "--", *command],
            env=perf_environment,
        )
        perf_stat_returncode = rc
        perf_replay = parse_replay_evidence(stat_stdout, expected_uid, expected_workers)
        perf_tool.permission_probe = (
            "command replayed under perf stat; inspect availability per event"
        )
        perf_stat = parse_perf_stat(perf_raw, rc if rc is not None else 1)
        if rc == 0 and perf_replay.availability != "available":
            perf_stat = tuple(
                NamedCounter(
                    entry.name,
                    unavailable(
                        "perf stat",
                        "benchmark process tree",
                        entry.counter.unit,
                        f"target credentials or trace unverified: {perf_replay.reason}",
                        "perf stat diagnostic replay",
                    ),
                )
                for entry in perf_stat
            )
    else:
        perf_stat = tuple(
            NamedCounter(
                event,
                unavailable(
                    "perf",
                    "command process tree",
                    "event units per perf",
                    "perf executable not installed",
                    "perf stat diagnostic replay",
                ),
            )
            for event in (name.partition(":")[0] for name in PERF_EVENTS.split(","))
        )
        perf_raw = "perf executable not installed"

    mapping: CounterRecord
    strace_returncode: int | None = None
    strace_raw = ""
    if strace_available:
        rc, _, strace_raw = run_tool(
            [strace_available, "-f", "-c", "-e", "trace=mmap,munmap,madvise", "--", *command]
        )
        strace_returncode = rc
        mapping = (
            available(
                "strace -f -c",
                "replayed process tree",
                "syscall summary",
                strace_raw,
                "strace diagnostic replay",
            )
            if rc == 0
            else unavailable(
                "strace -f -c",
                "replayed process tree",
                "syscall summary",
                f"strace failed or permission denied (returncode {rc}); see mapping_raw",
                "strace diagnostic replay",
            )
        )
    else:
        mapping = unavailable(
            "strace", "replayed process tree", "syscall summary", "strace executable not installed"
        )
    profiles = ProfileDiagnostic()
    if profile:
        control = run_control(command)
        profiles.control = control
        if not perf_available:
            profiles.status = unavailable(
                "perf record",
                "replayed process tree",
                "samples",
                "perf executable not installed",
                "separate profile diagnostic",
            )
        else:
            runs: list[NamedProfile] = []
            cycles = named_counter(perf_stat, "cycles")
            cpu_event = "cycles:u" if cycles.availability == "available" else "cpu-clock:u"
            for kind, event in (("cpu", cpu_event), ("page_faults", "page-faults:u")):
                profile_path = f"{profile}.cpu.data" if kind == "cpu" else f"{profile}.faults.data"
                record_started = time.monotonic_ns()
                rc, record_stdout, stderr = run_tool(
                    [
                        perf_available,
                        "record",
                        "-q",
                        "-F",
                        "99",
                        "-g",
                        "-e",
                        event,
                        "-o",
                        profile_path,
                        "--",
                        *command,
                    ],
                    env=perf_environment,
                )
                elapsed_ms = (time.monotonic_ns() - record_started) / 1_000_000
                report_rc, report_out, report_err = (
                    run_tool(
                        [
                            perf_available,
                            "report",
                            "--stdio",
                            "--header",
                            "-n",
                            "-g",
                            "graph,0.5,caller",
                            "--sort=comm,dso,symbol",
                            "-i",
                            profile_path,
                        ]
                    )
                    if rc == 0
                    else (None, "", "profile recording unavailable")
                )
                sample_match = re.search(r"# Samples:\s*([0-9,]+)", report_out)
                sample_count = int(sample_match.group(1).replace(",", "")) if sample_match else None
                replay = parse_replay_evidence(
                    record_stdout, expected_uid, expected_workers, control.stdout
                )
                profile_reason = (
                    "perf record failed or permission denied"
                    if rc != 0
                    else "perf report failed"
                    if report_rc != 0
                    else "perf report has no counted samples"
                    if sample_count is None or sample_count == 0
                    else replay.reason
                )
                overhead = None
                overhead_reason = None
                if control.availability == "available" and control.elapsed_ms > 0:
                    overhead = (elapsed_ms - control.elapsed_ms) / control.elapsed_ms * 100
                else:
                    overhead_reason = "replay control was unavailable or had zero elapsed time"
                phase = f"separate {kind} profile replay"
                elapsed_record = available(
                    "monotonic clock", "perf record replay", "milliseconds", elapsed_ms, phase
                )
                overhead_record = (
                    available(
                        "perf record elapsed vs direct replay control",
                        "command process tree",
                        "percent",
                        overhead,
                        phase,
                    )
                    if overhead is not None
                    else unavailable(
                        "perf record elapsed vs direct replay control",
                        "command process tree",
                        "percent",
                        overhead_reason or "unavailable",
                        phase,
                    )
                )
                report_record = (
                    available(
                        "perf report --stdio -n -g graph,0.5,caller --sort=comm,dso,symbol",
                        "user/kernel DSO symbols with call-chain graph; percentages and sample counts",
                        "overhead percent and sample count",
                        report_out,
                        f"report for separate {kind} profile replay",
                    )
                    if report_rc == 0 and sample_count is not None and sample_count > 0
                    else unavailable(
                        "perf report",
                        "replayed process tree",
                        "overhead percent and sample count",
                        profile_reason or "perf report failed",
                        f"report for separate {kind} profile replay",
                    )
                )
                profile_run = ProfileRun(
                    "available" if profile_reason is None else "unavailable",
                    profile_path if rc == 0 else None,
                    "perf record",
                    "replayed process tree",
                    "samples",
                    phase,
                    rc,
                    stderr,
                    elapsed_record,
                    overhead_record,
                    report_record,
                    "DSO/symbol call chains distinguish user and kernel samples",
                    report_err,
                    profile_reason,
                    event,
                    sample_count,
                    replay,
                )
                runs.append(NamedProfile(kind, profile_run))
            profiles.runs = tuple(runs)
            profiles.note = "each profile is a separate replay; overhead compares its perf record elapsed time with the untimed replay control; neither is acceptance timing"
    after_cgroup = snapshot_cgroup(cgpath, "after diagnostic replays")
    cgroup_deltas: list[NamedCounter] = []
    for before_entry in before_cgroup:
        name = before_entry.name
        cgroup_deltas.append(
            NamedCounter(
                name,
                delta_nodes(
                    before_entry.counter,
                    named_counter(after_cgroup, name),
                    "after minus before diagnostic replays",
                ),
            )
        )
    perf_stat_record = (
        available(
            "perf stat",
            "replayed process tree",
            "exit status",
            perf_stat_returncode,
            "perf stat diagnostic replay",
        )
        if perf_stat_returncode is not None
        else unavailable(
            "perf stat",
            "replayed process tree",
            "exit status",
            "perf executable not installed or could not run",
            "perf stat diagnostic replay",
        )
    )

    strace_record = (
        available(
            "strace",
            "replayed process tree",
            "exit status",
            strace_returncode,
            "strace diagnostic replay",
        )
        if strace_returncode is not None
        else unavailable(
            "strace",
            "replayed process tree",
            "exit status",
            "strace executable not installed or could not run",
            "strace diagnostic replay",
        )
    )
    return DeepDiagnosticArtifact(
        SCHEMA,
        start_wall,
        command,
        "Every command replay here is diagnostic and outside acceptance timing.",
        CgroupDiagnostic(
            str(cgpath) if cgpath else None, before_cgroup, after_cgroup, tuple(cgroup_deltas)
        ),
        host_memory_metadata(),
        ToolInventory(perf_tool, strace_tool),
        PerfStatistics(perf_stat, perf_raw, perf_replay),
        mapping,
        strace_raw,
        profiles,
        ReturnCodes(perf_stat_record, strace_record),
        collector=collector,
    )


def _per_operation_estimate(
    artifact: DeepDiagnosticArtifact, event: str, operations: int, arm: str
) -> CounterRecord:
    counter = named_counter(artifact.perf_stat.events, event)
    phase = f"{arm} diagnostic perf-stat estimate"
    unit = f"{event} per operation"
    replay = artifact.perf_stat.replay
    if replay is None or replay.availability != "available":
        return unavailable(
            counter.source,
            counter.scope,
            unit,
            "profiled replay target credentials and trace are not verified",
            phase,
        )
    if replay.completed_operations != operations:
        return unavailable(
            counter.source,
            counter.scope,
            unit,
            "profiled replay operation count differs from paired timed workload",
            phase,
        )
    if counter.availability != "available" or not isinstance(counter.value, (int, float)):
        return unavailable(
            counter.source,
            counter.scope,
            unit,
            counter.reason or f"{event} counter unavailable",
            phase,
        )
    return available(counter.source, counter.scope, unit, counter.value / operations, phase)


def collect_paired_deep_diagnostic(
    baseline_command: list[str],
    candidate_command: list[str],
    operations_per_arm: int,
    baseline_sha: str,
    candidate_sha: str,
    cell: str,
    output: Path,
    profile_prefix: Path | None = None,
) -> PairedDeepDiagnosticArtifact:
    """Collect separate untimed deep replays for one identical baseline/candidate cell.

    `output` names the caller's eventual paired JSON artifact; this helper only returns
    typed data. Profile data files use arm-specific prefixes beside that output path.
    """
    if not baseline_command or not candidate_command:
        raise ValueError("baseline and candidate commands must be non-empty")
    if baseline_command[1:] != candidate_command[1:]:
        raise ValueError("baseline and candidate workload arguments differ")
    if operations_per_arm <= 0:
        raise ValueError("operations_per_arm must be positive")
    for label, sha in (("baseline_sha", baseline_sha), ("candidate_sha", candidate_sha)):
        if len(sha) != 40 or any(char not in "0123456789abcdefABCDEF" for char in sha):
            raise ValueError(f"{label} must be a full 40-hex SHA")
    if not cell:
        raise ValueError("cell must be non-empty")
    baseline_profile = f"{profile_prefix}.baseline" if profile_prefix is not None else None
    candidate_profile = f"{profile_prefix}.candidate" if profile_prefix is not None else None
    baseline = collect(
        baseline_command, output.with_name(output.stem + ".baseline"), baseline_profile
    )
    candidate = collect(
        candidate_command, output.with_name(output.stem + ".candidate"), candidate_profile
    )
    baseline_replay = baseline.perf_stat.replay
    candidate_replay = candidate.perf_stat.replay
    identity_reason: str | None = None
    if baseline_replay is None or candidate_replay is None:
        identity_reason = "a perf-stat replay has no target evidence"
    elif (
        baseline_replay.availability != "available" or candidate_replay.availability != "available"
    ):
        identity_reason = "a perf-stat replay target credential or trace is unverified"
    elif (
        baseline_replay.completed_operations != operations_per_arm
        or candidate_replay.completed_operations != operations_per_arm
    ):
        identity_reason = "a perf-stat replay operation count differs from the timed pair"
    elif baseline_replay.trace_checksum != candidate_replay.trace_checksum:
        identity_reason = "baseline and candidate perf-stat trace checksums differ"
    identity = PairReplayIdentity(
        "available" if identity_reason is None else "unavailable",
        identity_reason,
        baseline_replay.trace_checksum if baseline_replay else None,
        candidate_replay.trace_checksum if candidate_replay else None,
        operations_per_arm,
    )
    estimates: list[PairedEventEstimate] = []
    for event in ("cycles", "instructions"):
        baseline_estimate = _per_operation_estimate(baseline, event, operations_per_arm, "baseline")
        candidate_estimate = _per_operation_estimate(
            candidate, event, operations_per_arm, "candidate"
        )
        phase = "paired baseline-to-candidate diagnostic estimate"
        if (
            identity.availability == "available"
            and baseline_estimate.availability == "available"
            and candidate_estimate.availability == "available"
            and isinstance(baseline_estimate.value, (int, float))
            and isinstance(candidate_estimate.value, (int, float))
            and baseline_estimate.value != 0
        ):
            change = available(
                "derived perf stat counters",
                "paired command replays",
                "percent",
                (candidate_estimate.value - baseline_estimate.value)
                / baseline_estimate.value
                * 100,
                phase,
            )
        else:
            reason = (
                identity_reason
                if identity_reason is not None
                else "baseline counter unavailable or zero"
                if baseline_estimate.value in (None, 0)
                else "candidate counter unavailable"
            )
            change = unavailable(
                "derived perf stat counters", "paired command replays", "percent", reason, phase
            )
        estimates.append(PairedEventEstimate(event, baseline_estimate, candidate_estimate, change))
    return PairedDeepDiagnosticArtifact(
        "large-span-paired-deep-v1",
        baseline_sha.lower(),
        candidate_sha.lower(),
        cell,
        operations_per_arm,
        baseline,
        candidate,
        tuple(estimates),
        "Separate diagnostic replays only; per-operation event estimates are descriptive and have no timed-run confidence interval.",
        identity,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new JSON artifact path")
    parser.add_argument("--profile", help="optional output prefix for separate perf record runs")
    parser.add_argument(
        "--command",
        nargs=argparse.REMAINDER,
        required=True,
        help="replay command and arguments (use -- before executable)",
    )
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    try:
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        artifact = collect(command, args.output, args.profile)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(asdict(artifact), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as error:
        print(f"large_span_deep: {error}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
