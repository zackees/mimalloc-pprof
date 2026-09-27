#!/usr/bin/env python3
"""Prepare a job-private CAP_PERFMON collector for untimed Linux diagnostics.

Never relax perf_event_paranoid or grant a capability to the installed perf binary.
The private executable is root-owned, non-writable, and lives in RUNNER_TEMP.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

PROBE_EVENTS = (
    "task-clock:u",
    "cpu-clock:u",
    "cpu-clock",
    "cycles:u",
    "cycles",
    "instructions:u",
    "cache-references:u",
    "cache-misses:u",
    "dTLB-loads:u",
    "dTLB-load-misses:u",
    "iTLB-loads:u",
    "iTLB-load-misses:u",
    "page-faults:u",
    "page-faults",
)
BUSY_PROBE = "sum(i * i for i in range(1_000_000))"
CAP_PERFMON_BIT = 1 << 38


@dataclass(frozen=True)
class RunnerPermissions:
    kernel: str
    perf_version: str
    perf_event_paranoid: str
    cap_eff: str
    cap_bnd: str
    cap_amb: str
    no_new_privs: str
    mount_options: str
    installed_perf: str | None
    installed_perf_capabilities: str


@dataclass(frozen=True)
class EventProbe:
    event: str
    availability: str
    value: float | None
    reason: str | None
    returncode: int | None
    raw_stderr: str


@dataclass(frozen=True)
class AccessReport:
    schema_version: str
    collector_mode: str
    collector_path: str | None
    reason: str | None
    permissions_before: RunnerPermissions
    permissions_after: RunnerPermissions
    private_capabilities: str
    event_probes: tuple[EventProbe, ...]


def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def status_value(status: str, key: str) -> str:
    for line in status.splitlines():
        name, separator, value = line.partition(":")
        if separator and name == key:
            return value.strip()
    return "unavailable"


def mount_options(path: Path) -> str:
    target = path.resolve()
    best_mount = Path("/")
    best_options = "unavailable"
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return best_options
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        mount = Path(fields[4])
        if (target == mount or mount in target.parents) and len(str(mount)) >= len(str(best_mount)):
            best_mount = mount
            best_options = fields[5]
    return best_options


def snapshot_permissions(directory: Path, installed_perf: str | None) -> RunnerPermissions:
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        status = ""
    try:
        paranoid = Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
    except OSError:
        paranoid = "unavailable"
    version = run([installed_perf, "version"]).stdout.strip() if installed_perf else "unavailable"
    installed_caps = (
        run(["getcap", "-n", installed_perf]).stdout.strip()
        if installed_perf and shutil.which("getcap")
        else "unavailable"
    )
    return RunnerPermissions(
        platform.release(),
        version or "unavailable",
        paranoid,
        status_value(status, "CapEff"),
        status_value(status, "CapBnd"),
        status_value(status, "CapAmb"),
        status_value(status, "NoNewPrivs"),
        mount_options(directory),
        installed_perf,
        installed_caps,
    )


def classify_probe(event: str, result: subprocess.CompletedProcess[str]) -> EventProbe:
    raw = result.stderr
    lowered = raw.lower()
    if "permission" in lowered or "access to performance monitoring" in lowered:
        return EventProbe(event, "unavailable", None, "permission denied", result.returncode, raw)
    for marker, reason in (
        ("<not supported>", "event unsupported by runner PMU"),
        ("<not counted>", "event was not counted"),
    ):
        if marker in lowered:
            return EventProbe(event, "unavailable", None, reason, result.returncode, raw)
    for line in raw.splitlines():
        fields = line.split(";")
        if len(fields) < 3 or fields[2].strip() != event:
            continue
        try:
            value = float(fields[0].strip().replace(",", ""))
        except ValueError:
            break
        if result.returncode == 0 and value > 0:
            return EventProbe(event, "available", value, None, 0, raw)
        break
    return EventProbe(
        event,
        "unavailable",
        None,
        f"tool failure or zero count (exit {result.returncode})",
        result.returncode,
        raw,
    )


def probe_events(perf: str) -> tuple[EventProbe, ...]:
    return tuple(
        classify_probe(
            event,
            run([perf, "stat", "-x;", "-e", event, "--", sys.executable, "-c", BUSY_PROBE]),
        )
        for event in PROBE_EVENTS
    )


def prepare(directory: Path) -> AccessReport:
    installed_perf = shutil.which("perf")
    before = snapshot_permissions(directory, installed_perf)
    reason: str | None = None
    private: Path | None = None
    private_caps = "unavailable"
    try:
        bounding_capabilities = int(before.cap_bnd, 16)
    except ValueError:
        bounding_capabilities = 0
    if installed_perf is None:
        reason = "perf executable not installed"
    elif not shutil.which("setcap") or not shutil.which("getcap") or not shutil.which("sudo"):
        reason = "setcap, getcap or sudo unavailable"
    elif not bounding_capabilities & CAP_PERFMON_BIT:
        reason = "CAP_PERFMON absent from runner capability bounding set"
    elif "nosuid" in before.mount_options.split(","):
        reason = "private executable filesystem is mounted nosuid"
    else:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        private = directory / "perf-private"
        if private.exists():
            reason = "job-private perf path already exists"
        else:
            try:
                with Path(installed_perf).open("rb") as source:
                    if source.read(4) != b"\x7fELF":
                        reason = "installed perf is not an ELF executable"
            except OSError as error:
                reason = f"could not inspect installed perf: {error}"
        if reason is None:
            shutil.copyfile(installed_perf, private)
            for command in (
                ["sudo", "-n", "chown", f"root:{os.getgid()}", str(private)],
                ["sudo", "-n", "chmod", "0550", str(private)],
                ["sudo", "-n", "setcap", "cap_perfmon=ep", str(private)],
            ):
                result = run(command)
                if result.returncode != 0:
                    reason = f"private perf setup failed: {' '.join(command[2:3])}: {result.stderr.strip()}"
                    break
            private_caps = run(["getcap", "-n", str(private)]).stdout.strip()
            if reason is None and "cap_perfmon=ep" not in private_caps.lower():
                reason = f"private perf lacks CAP_PERFMON: {private_caps or 'no file capability'}"
    selected = str(private) if private is not None and reason is None else None
    probes = probe_events(selected) if selected else ()
    after = snapshot_permissions(directory, installed_perf)
    if before.perf_event_paranoid != after.perf_event_paranoid:
        raise RuntimeError("host-wide perf_event_paranoid changed during perf setup")
    if before.installed_perf_capabilities != after.installed_perf_capabilities:
        raise RuntimeError("installed perf capabilities changed during perf setup")
    return AccessReport(
        "perf-access-v1",
        "private-cap-perfmon" if selected else "unavailable",
        selected,
        reason,
        before,
        after,
        private_caps,
        probes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args(argv)
    report = prepare(args.directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asdict(report), indent=2) + "\n")
    if args.github_env:
        with args.github_env.open("a") as environment:
            environment.write(f"MIMALLOC_PERF_SETUP_STATUS={report.collector_mode}\n")
            if report.collector_path:
                environment.write(f"MIMALLOC_PERF_EXECUTABLE={report.collector_path}\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
