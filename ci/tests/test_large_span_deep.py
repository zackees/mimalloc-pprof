"""Contracts for the opt-in large-span deep diagnostic collector."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

import large_span_deep as deep


def no_cgroup() -> None:
    return None


def missing_cgroup_snapshot(
    _path: Path | None, _phase: str = "snapshot"
) -> tuple[deep.NamedCounter, ...]:
    return (
        deep.NamedCounter(
            "memory.current", deep.unavailable("cgroup", "cgroup", "bytes", "missing")
        ),
    )


def empty_host_memory_metadata() -> deep.HostMemoryDiagnostic:
    return deep.HostMemoryDiagnostic((), ())


def no_host_memory_metadata() -> deep.HostMemoryDiagnostic:
    return deep.HostMemoryDiagnostic((), ())


def no_tool(_name: str) -> None:
    return None


def perf_only_tool(name: str) -> str | None:
    return "/usr/bin/perf" if name == "perf" else None


def permission_denied_tool(
    _command: list[str], _timeout: int = 3600, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    assert env is not None and env["PERF_AB_PERF_CREDENTIALS"] == "1"
    return (17, "", "permission denied")


def empty_deep_artifact(
    command: list[str], cycles: deep.CounterRecord, instructions: deep.CounterRecord
) -> deep.DeepDiagnosticArtifact:
    return deep.DeepDiagnosticArtifact(
        "large-span-deep-v1",
        1.0,
        command,
        "diagnostic",
        deep.CgroupDiagnostic(None, (), (), ()),
        deep.HostMemoryDiagnostic((), ()),
        deep.ToolInventory(deep.ToolAvailability(None), deep.ToolAvailability(None)),
        deep.PerfStatistics(
            (deep.NamedCounter("cycles", cycles), deep.NamedCounter("instructions", instructions)),
            "",
            deep.ReplayEvidence("available", None, 10, "same-trace", None, (), 1000, 1),
        ),
        deep.unavailable("strace", "process", "calls", "absent"),
        "",
        deep.ProfileDiagnostic(),
        deep.ReturnCodes(
            deep.unavailable("perf", "process", "status", "absent"),
            deep.unavailable("strace", "process", "status", "absent"),
        ),
    )


def test_cgroup_v2_path_resolves_mount_root(tmp_path: Path) -> None:
    proc_cgroup = tmp_path / "cgroup"
    proc_cgroup.write_text("0::/slice/job/run\n")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("31 22 0:28 /slice /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
    assert deep.cgroup_v2_path(proc_cgroup, mountinfo) == Path("/sys/fs/cgroup/job/run")


def test_cgroup_snapshots_keep_missing_counters_null(tmp_path: Path) -> None:
    (tmp_path / "memory.current").write_text("1234\n")
    (tmp_path / "memory.stat").write_text("anon 10\npgfault 20\n")
    values = deep.snapshot_cgroup(tmp_path)
    current = deep.named_counter(values, "memory.current")
    stats = deep.named_counter(values, "memory.stat")
    events = deep.named_counter(values, "memory.events")
    pressure = deep.named_counter(values, "memory.pressure")
    assert current.value == 1234
    assert current.scope == "current cgroup and descendants"
    assert isinstance(stats, deep.CounterGroup)
    assert deep.named_counter(stats.value, "anon").value == 10
    assert deep.named_counter(stats.value, "pgfault").value == 20
    assert events.value is None
    assert events.availability == "unavailable"
    assert isinstance(pressure, deep.CounterRecord)
    assert pressure.value is None and pressure.availability == "unavailable"


def test_pressure_snapshots_have_numeric_deltas_and_missing_is_not_zero(tmp_path: Path) -> None:
    before_text = "some avg10=1.00 avg60=2.00 avg300=3.00 total=100\nfull avg10=0.50 avg60=1.00 avg300=1.50 total=40\n"
    after_text = "some avg10=1.50 avg60=2.50 avg300=3.50 total=175\n"
    before = deep.pressure_values(before_text)
    after = deep.pressure_values(after_text)
    delta = deep.delta_nodes(before, after, "after minus before diagnostic replays")
    assert isinstance(delta, deep.CounterGroup)
    some = deep.named_counter(delta.value, "some")
    assert isinstance(some, deep.CounterGroup)
    total = deep.named_counter(some.value, "total")
    avg10 = deep.named_counter(some.value, "avg10")
    full = deep.named_counter(delta.value, "full")
    assert total.value == 75
    assert total.unit == "microseconds"
    assert avg10.value == 0.5
    assert avg10.unit == "percent"
    assert full.value is None
    reason = full.reason
    assert reason is not None
    assert "missing from after" in reason


def test_perf_stat_parser_preserves_unavailable_not_zero() -> None:
    text = "1000.00; ;task-clock;100.00;%;\n<not supported>; ;cycles; ; ;\n"
    values = deep.parse_perf_stat(text, 0)
    assert deep.named_counter(values, "task-clock").value == 1000.0
    cycles = deep.named_counter(values, "cycles")
    assert cycles.value is None
    assert cycles.availability == "unavailable"
    assert cycles.reason == "<not supported>"


def credential_stdout(uid: int, worker_uid: int, cap_eff: str = "0") -> str:
    template = (
        '{"completed_operations":800,"trace_checksum":"deadbeef","perf_credentials":{'
        '"process":{"uid":<UID>,"euid":<UID>,"gid":100,"egid":100,'
        '"cap_eff":"<CAP>","cap_amb":"0","valid":true},'
        '"workers":[{"uid":<WORKER>,"euid":<WORKER>,"gid":100,"egid":100,'
        '"cap_eff":"0","cap_amb":"0","valid":true}]}}'
    )
    return (
        template.replace("<UID>", str(uid))
        .replace("<WORKER>", str(worker_uid))
        .replace("<CAP>", cap_eff)
    )


def test_profiled_child_and_workers_remain_unprivileged_and_match_control() -> None:
    control = '{"completed_operations":800,"trace_checksum":"deadbeef"}'
    good = deep.parse_replay_evidence(credential_stdout(1000, 1000), 1000, 1, control)
    root_worker = deep.parse_replay_evidence(credential_stdout(1000, 0), 1000, 1, control)
    privileged_child = deep.parse_replay_evidence(
        credential_stdout(1000, 1000, "0000004000000000"), 1000, 1, control
    )
    changed_work = deep.parse_replay_evidence(
        credential_stdout(1000, 1000),
        1000,
        1,
        '{"completed_operations":800,"trace_checksum":"other"}',
    )
    assert good.availability == "available"
    assert good.completed_operations == 800
    assert len(good.workers) == 1
    assert root_worker.availability == "unavailable" and "different UID" in str(root_worker.reason)
    assert privileged_child.availability == "unavailable"
    assert "retained CAP_PERFMON" in str(privileged_child.reason)
    assert changed_work.availability == "unavailable"


def test_target_pid_does_not_grant_perf_permission() -> None:
    denied = deep.parse_perf_stat(
        "Access to performance monitoring and observability operations is limited", 255
    )
    assert deep.named_counter(denied, "task-clock").availability == "unavailable"


def test_host_thp_counters_are_explicitly_unattributable(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    sysfs = tmp_path / "sys"
    proc.mkdir()
    policy = sysfs / "kernel/mm/transparent_hugepage"
    (policy / "madvise").mkdir(parents=True)
    (policy / "madvise/enabled").write_text("always [madvise] never\n")
    (policy / "enabled").write_text("[always] madvise never\n")
    (policy / "defrag").write_text("always defer [madvise] never\n")
    (proc / "vmstat").write_text("thp_fault_alloc 77\nnr_anon_pages 12\n")
    data = deep.host_memory_metadata(proc, sysfs)
    vmstat = deep.named_counter(data.vmstat, "thp_fault_alloc")
    assert vmstat.value == 77
    assert "unattributable" in vmstat.scope
    assert (
        deep.named_counter(data.thp_policy, "kernel/mm/transparent_hugepage/enabled").value
        == "[always] madvise never"
    )
    assert vmstat.phase == "diagnostic replay"


def test_collect_marks_missing_tools_and_keeps_profile_optional(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep, "cgroup_v2_path", no_cgroup)
    monkeypatch.setattr(deep, "snapshot_cgroup", missing_cgroup_snapshot)
    monkeypatch.setattr(deep, "host_memory_metadata", empty_host_memory_metadata)
    monkeypatch.setattr(deep.shutil, "which", no_tool)
    artifact = deep.collect(["./replay", "arg"], tmp_path / "out.json")
    assert artifact.schema_version == "large-span-deep-v1"
    assert artifact.command == ["./replay", "arg"]
    assert deep.named_counter(artifact.perf_stat.events, "cycles").value is None
    assert artifact.mapping_syscalls.availability == "unavailable"
    assert artifact.profiles.runs == ()
    assert deep.named_counter(artifact.cgroup_v2.delta, "memory.current").value is None
    assert asdict(artifact)["schema_version"] == "large-span-deep-v1"


def test_hosted_setup_failure_does_not_silently_retry_unprivileged_perf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MIMALLOC_PERF_SETUP_STATUS", "unavailable")
    monkeypatch.setattr(deep, "cgroup_v2_path", no_cgroup)
    monkeypatch.setattr(deep, "snapshot_cgroup", missing_cgroup_snapshot)
    monkeypatch.setattr(deep, "host_memory_metadata", no_host_memory_metadata)
    monkeypatch.setattr(deep.shutil, "which", perf_only_tool)

    def forbidden(
        _command: list[str], _timeout: int = 3600, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        pytest.fail("unprivileged perf must not run after scoped setup failed")

    monkeypatch.setattr(deep, "run_tool", forbidden)
    artifact = deep.collect(["./replay", "1"], tmp_path / "out")
    assert artifact.collector is not None and artifact.collector.mode == "unavailable"
    assert deep.named_counter(artifact.perf_stat.events, "cycles").value is None
    assert "private collector unavailable" in artifact.perf_stat.raw_stderr


def test_collect_runs_tools_separately_and_records_raw_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def cgroup_path() -> Path:
        return Path("/cg")

    calls: list[list[str]] = []

    def snapshot(_path: Path | None, phase: str = "snapshot") -> tuple[deep.NamedCounter, ...]:
        return (
            deep.NamedCounter(
                "memory.stat",
                deep.CounterGroup(
                    "/cg/memory.stat",
                    "current cgroup and descendants",
                    "kernel-defined",
                    phase,
                    "available",
                    (
                        deep.NamedCounter(
                            "pgfault",
                            deep.available(
                                "/cg/memory.stat",
                                "current cgroup and descendants",
                                "count",
                                len(calls),
                                phase,
                            ),
                        ),
                    ),
                ),
            ),
        )

    def run_tool(
        command: list[str], timeout: int = 3600, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        calls.append(command)
        if command[1] in ("stat", "record"):
            assert env is not None and env["PERF_AB_PERF_CREDENTIALS"] == "1"
        if "stat" in command:
            return (
                0,
                credential_stdout(1000, 1000),
                "100; ;task-clock;100%;\n<not supported>; ;cycles; ; ;\n",
            )
        if "strace" in command[0]:
            return 0, "", "% time seconds usecs/call calls errors syscall\n"
        if command[1] == "record":
            return 0, credential_stdout(1000, 1000), "profiled\n"
        return 0, "# Samples: 10 of event cpu-clock:u\n", ""

    def replay_control(_command: list[str]) -> deep.ReplayControl:
        return deep.ReplayControl(
            "available",
            0,
            100.0,
            '{"completed_operations":800,"trace_checksum":"deadbeef"}',
            "",
            "untimed diagnostic replay control",
            "command process tree",
            "milliseconds",
        )

    monkeypatch.setattr(deep, "snapshot_cgroup", snapshot)
    monkeypatch.setattr(deep, "cgroup_v2_path", cgroup_path)
    monkeypatch.setattr(deep, "run_control", replay_control)
    monkeypatch.setattr(deep, "host_memory_metadata", no_host_memory_metadata)

    def runner_uid() -> int:
        return 1000

    monkeypatch.setattr(deep.os, "getuid", runner_uid)

    def installed_tool(name: str) -> str:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(deep.shutil, "which", installed_tool)
    monkeypatch.setattr(deep, "run_tool", run_tool)
    result = deep.collect(["./replay", "1"], tmp_path / "out", str(tmp_path / "profile"))
    assert len(calls) == 6  # perf/strace, two profile replays, and their two reports
    assert deep.named_counter(result.perf_stat.events, "cycles").value is None
    assert result.mapping_syscalls.scope == "replayed process tree"
    stat_delta = deep.named_counter(result.cgroup_v2.delta, "memory.stat")
    assert isinstance(stat_delta, deep.CounterGroup)
    assert deep.named_counter(stat_delta.value, "pgfault").value == 6
    cpu = deep.named_profile(result.profiles.runs, "cpu")
    assert cpu.availability == "available"
    assert cpu.event == "cpu-clock"  # cycles unsupported: use a software CPU sampler
    assert cpu.sample_count == 10
    assert cpu.replay is not None and cpu.replay.availability == "available"
    assert cpu.overhead_vs_control_percent.value is not None
    assert "graph,0.5,caller" in cpu.report.source
    assert result.replay_return_codes.perf_stat.value == 0
    assert "outside acceptance timing" in result.execution_note


def test_perf_stat_records_returncode_and_does_not_lose_missing_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep, "cgroup_v2_path", no_cgroup)
    monkeypatch.setattr(deep, "snapshot_cgroup", missing_cgroup_snapshot)
    monkeypatch.setattr(deep, "host_memory_metadata", no_host_memory_metadata)
    monkeypatch.setattr(deep.shutil, "which", perf_only_tool)
    monkeypatch.setattr(deep, "run_tool", permission_denied_tool)
    result = deep.collect(["./replay"], tmp_path / "out")
    assert result.replay_return_codes.perf_stat.value == 17
    cycles = deep.named_counter(result.perf_stat.events, "cycles")
    assert cycles.value is None
    assert cycles.phase == "perf stat diagnostic replay"


def test_paired_collector_derives_per_operation_events_and_keeps_profiles_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], Path, str | None]] = []

    def fake_collect(
        command: list[str], output: Path, profile: str | None = None
    ) -> deep.DeepDiagnosticArtifact:
        calls.append((command, output, profile))
        scale = 1 if command[0] == "baseline-bin" else 1.2
        cycles = deep.available("perf stat", "command tree", "cycles", 1000 * scale)
        instructions = deep.available("perf stat", "command tree", "instructions", 5000 * scale)
        return empty_deep_artifact(command, cycles, instructions)

    monkeypatch.setattr(deep, "collect", fake_collect)
    result = deep.collect_paired_deep_diagnostic(
        ["baseline-bin", "8", "128k"],
        ["candidate-bin", "8", "128k"],
        10,
        "a" * 40,
        "b" * 40,
        "large-span/8",
        tmp_path / "paired.json",
        tmp_path / "profile",
    )
    assert len(calls) == 2
    assert calls[0][0][1:] == calls[1][0][1:]
    assert calls[0][2] == str(tmp_path / "profile") + ".baseline"
    assert calls[1][2] == str(tmp_path / "profile") + ".candidate"
    cycles = result.event_estimates[0]
    assert cycles.baseline_per_operation.value == 100
    assert cycles.candidate_per_operation.value == 120
    assert cycles.paired_change_percent.value == 20
    instructions = result.event_estimates[1]
    assert instructions.paired_change_percent.value == 20
    assert result.replay_identity is not None
    assert result.replay_identity.availability == "available"
    assert "no timed-run confidence interval" in result.inference_note


def test_paired_collector_keeps_unavailable_event_null_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_collect(
        command: list[str], output: Path, profile: str | None = None
    ) -> deep.DeepDiagnosticArtifact:
        assert profile is None  # profile recording remains opt-in for paired deep replays
        cycles = deep.unavailable("perf stat", "command tree", "cycles", "PMU permission denied")
        instructions = deep.available("perf stat", "command tree", "instructions", 50_000)
        return empty_deep_artifact(command, cycles, instructions)

    monkeypatch.setattr(deep, "collect", fake_collect)
    result = deep.collect_paired_deep_diagnostic(
        ["base", "same-args"],
        ["candidate", "same-args"],
        10,
        "a" * 40,
        "b" * 40,
        "cell",
        tmp_path / "paired.json",
        None,
    )
    cycles = result.event_estimates[0]
    assert cycles.baseline_per_operation.value is None
    assert cycles.candidate_per_operation.value is None
    assert cycles.paired_change_percent.value is None
    assert cycles.paired_change_percent.reason == "baseline counter unavailable or zero"


def test_paired_collector_rejects_changed_profiled_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_collect(
        command: list[str], output: Path, profile: str | None = None
    ) -> deep.DeepDiagnosticArtifact:
        artifact = empty_deep_artifact(
            command,
            deep.available("perf stat", "command tree", "cycles", 1000),
            deep.available("perf stat", "command tree", "instructions", 500),
        )
        artifact.perf_stat.replay = deep.ReplayEvidence(
            "available", None, 10, command[0], None, (), 1000, 1
        )
        return artifact

    monkeypatch.setattr(deep, "collect", fake_collect)
    result = deep.collect_paired_deep_diagnostic(
        ["base", "same-args"],
        ["candidate", "same-args"],
        10,
        "a" * 40,
        "b" * 40,
        "cell",
        tmp_path / "paired.json",
    )
    assert result.replay_identity is not None
    assert result.replay_identity.availability == "unavailable"
    assert "checksums differ" in str(result.replay_identity.reason)
    assert result.event_estimates[0].paired_change_percent.value is None


def test_paired_collector_rejects_mismatched_args_and_short_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workload arguments differ"):
        deep.collect_paired_deep_diagnostic(
            ["base", "8"],
            ["candidate", "4"],
            10,
            "a" * 40,
            "b" * 40,
            "cell",
            tmp_path / "paired.json",
        )
    with pytest.raises(ValueError, match="full 40-hex SHA"):
        deep.collect_paired_deep_diagnostic(
            ["base", "8"],
            ["candidate", "8"],
            10,
            "short",
            "b" * 40,
            "cell",
            tmp_path / "paired.json",
        )
