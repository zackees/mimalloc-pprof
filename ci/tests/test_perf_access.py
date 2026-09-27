"""Private, process-scoped perf collector setup and availability classification."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import perf_access as access


def permissions(perf: str, bounding: str = "000000ffffffffff") -> access.RunnerPermissions:
    return access.RunnerPermissions(
        "6.8.0",
        "perf version 6.8",
        "4",
        "0000000000000000",
        bounding,
        "0000000000000000",
        "0",
        "rw,relatime",
        perf,
        "",
    )


def no_probes(_perf: str) -> tuple[access.EventProbe, ...]:
    return ()


def test_permission_denial_is_not_an_unsupported_pmu() -> None:
    denied = access.classify_probe(
        "cycles:u",
        subprocess.CompletedProcess(
            ["perf"], 255, "", "Access to performance monitoring is limited"
        ),
    )
    unsupported = access.classify_probe(
        "cycles:u",
        subprocess.CompletedProcess(["perf"], 0, "", "<not supported>;;cycles:u;"),
    )
    not_counted = access.classify_probe(
        "cycles:u",
        subprocess.CompletedProcess(["perf"], 0, "", "<not counted>;;cycles:u;"),
    )
    counted = access.classify_probe(
        "task-clock:u",
        subprocess.CompletedProcess(["perf"], 0, "", "123.45;;task-clock:u;"),
    )
    assert denied.reason == "permission denied"
    assert unsupported.reason == "event unsupported by runner PMU"
    assert not_counted.reason == "event was not counted"
    assert counted.availability == "available" and counted.value == 123.45


def test_missing_bounding_capability_never_changes_host_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "perf"
    source.write_bytes(b"\x7fELFdummy")

    def which(_name: str) -> str:
        return str(source)

    def snapshot(_directory: Path, _path: str | None) -> access.RunnerPermissions:
        return permissions(str(source), "0")

    monkeypatch.setattr(access.shutil, "which", which)
    monkeypatch.setattr(access, "snapshot_permissions", snapshot)

    def forbidden(_command: object) -> None:
        pytest.fail("no command may run when CAP_PERFMON is absent from the bounding set")

    monkeypatch.setattr(access, "run", forbidden)
    report = access.prepare(tmp_path / "private")
    assert report.collector_mode == "unavailable"
    assert report.reason == "CAP_PERFMON absent from runner capability bounding set"
    assert report.event_probes == ()
    assert not (tmp_path / "private").exists()


def test_private_file_capability_never_touches_installed_perf_or_sysctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "perf-system"
    source.write_bytes(b"\x7fELFdummy")
    commands: list[tuple[str, ...]] = []

    def which(_name: str) -> str:
        return str(source)

    def snapshot(_directory: Path, _path: str | None) -> access.RunnerPermissions:
        return permissions(str(source))

    monkeypatch.setattr(access.shutil, "which", which)
    monkeypatch.setattr(access, "snapshot_permissions", snapshot)

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        output = f"{command[-1]} cap_perfmon=ep\n" if command[0] == "getcap" else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(access, "run", fake_run)
    monkeypatch.setattr(access, "probe_events", no_probes)
    report = access.prepare(tmp_path / "private")
    assert report.collector_mode == "private-cap-perfmon"
    assert report.collector_path == str(tmp_path / "private/perf-private")
    assert source.read_bytes() == b"\x7fELFdummy"
    assert any(command[0:4] == ("sudo", "-n", "setcap", "cap_perfmon=ep") for command in commands)
    assert all(str(source) not in command for command in commands)
    assert all("sysctl" not in command for command in commands)
