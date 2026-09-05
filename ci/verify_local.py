#!/usr/bin/env python3
"""Fast, parallel local mirror of the Linux-runnable subset of CI.

Behavior-correctness verification for this repo (see `.github/workflows/c-unit.yml`,
`rust-native.yml`, `python-lint.yml`, `asan.yml`) is normally done by hand, serially,
running the same handful of cmake/ctest/cargo/ruff/pyright commands one after another.
This script runs the same commands -- same flags, same env, same assertions -- as a
pool of concurrent configs, each building into its own directory under
`out/verify/<config>/` so repeated invocations are incremental.

A pass here is a strong (not perfect) predictor of CI: only the ubuntu-latest portion
of each matrixed job is covered (no Windows/macOS/MSYS2), and every job's exact cmake
flags/env are copied from the workflow files rather than re-derived, so drift is
caught by `ci/tests/test_verify_local.py` rather than trusted to stay in sync by hand.

`--bundle <name>` is the one thing here that is not about Linux: it reproduces a
macOS/Windows lane of `macos-bundles.yml`/`windows-bundles.yml` on this box, through
the same `soldr prepare`, the same `cmake/toolchains/soldr-<triple>.cmake` and the same
`ci/bundle_tests.py` CI uses -- everything up to the execution step, which needs the
target OS (#277 phase F).

Usage:
    uv run ci/verify_local.py                        # everything fast (slow tests excluded)
    uv run ci/verify_local.py --only release,lint     # just these configs
    uv run ci/verify_local.py --slow                  # also run the long-tail ctest suite
    uv run ci/verify_local.py --list                  # print the config + bundle tables
    uv run ci/verify_local.py --like-ci               # c-unit.yml's own two-stage layout
    uv run ci/verify_local.py --bundle macos-arm64-release   # cross-build one CI bundle
    uv run ci/verify_local.py --jobs 8                # override the worker/build budget
    uv run ci/verify_local.py --keep-going             # don't skip queued configs on a failure
    uv run ci/verify_local.py --selftest               # trivially fast dry-run, no real builds

Exit code is non-zero if any selected config failed (a config SKIPPED because a tool
it needs -- e.g. clang for `asan` -- is unavailable does not count as a failure).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "out" / "verify"

# ctest name substrings for the tests long enough that they get their own opt-in tier.
# Matches test-zero-tracking AND test-zero-tracking-enabled (substring, per ctest -E).
SLOW_TEST_REGEX = "test-profile-race|test-subproc-lifecycle|test-zero-tracking"

# Seconds; only applies to tests without their own TIMEOUT property (test-profile-race,
# test-subproc-lifecycle, test-zero-tracking* already have theirs from CMakeLists.txt).
# CI itself sets no timeout at all here (ctest's own default is 1500s); this exists only
# to bound a genuine hang, not to be tight -- ten configs sharing this machine's cores
# (plus, right now, another agent's concurrent build) is real contention, not a hang.
DEFAULT_CTEST_TIMEOUT = 600


def cmake_bin() -> str:
    return "cmake"


def have_ninja() -> bool:
    return shutil.which("ninja") is not None


def have_ccache() -> bool:
    return shutil.which("ccache") is not None


def generator_args() -> list[str]:
    return ["-G", "Ninja"] if have_ninja() else []


def launcher_args() -> list[str]:
    return ["-DCMAKE_C_COMPILER_LAUNCHER=ccache"] if have_ccache() else []


@dataclasses.dataclass
class RunCtx:
    """Everything one config's runner function needs, nothing it should reach past."""

    name: str
    dir: Path
    log: Path
    jobs: int
    slow: bool


@dataclasses.dataclass
class Outcome:
    name: str
    job: str
    ok: bool | None  # True/False = ran; None = SKIPPED (tool unavailable)
    seconds: float
    build_dir: str
    log_path: str
    reason: str = ""


def log_write(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(text)


def log_start(log: Path, text: str) -> None:
    """Truncate `log` and write `text` as its first line. Every run of a config starts
    here so a rerun's log never contains a stale PASS-looking line from a previous run
    (log_write() itself always appends within one run).
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as f:
        f.write(text)


def run_logged(
    cmd: list[str],
    *,
    cwd: Path,
    log: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, str]:
    """Run `cmd`, streaming output into `log` line-by-line (so a hang still leaves a
    partial log) and also returning the captured text for callers that need to grep it.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    log.parent.mkdir(parents=True, exist_ok=True)
    header = f"\n$ {' '.join(shlex.quote(c) for c in cmd)}  (cwd={cwd})\n"
    with log.open("a", encoding="utf-8") as f:
        f.write(header)
        f.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines: list[str] = []
        assert proc.stdout is not None
        start = time.monotonic()
        timed_out = False
        for line in proc.stdout:
            lines.append(line)
            f.write(line)
            f.flush()
            if timeout is not None and (time.monotonic() - start) > timeout:
                proc.kill()
                timed_out = True
                break
        rc = proc.wait()
        if timed_out:
            f.write(f"\n[verify_local] TIMEOUT after {timeout}s, process killed\n")
            rc = rc or 124
    return rc, "".join(lines)


def run_captured(log: Path, func: Callable[[], int], *, label: str) -> tuple[int, str]:
    """Call a Python function (e.g. a ci/*.py module entry point) capturing its stdout
    into the log instead of shelling out to `uv run ci/<script>.py`.
    """
    log_write(log, f"\n$ <python> {label}\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = func()
    text = buf.getvalue()
    log_write(log, text)
    return rc, text


def cmake_configure(
    ctx: RunCtx, build: Path, args: list[str], *, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Returns (rc, configure output) -- callers that need to assert on the resolved
    configure output (e.g. `Compiler defines :`) use the text directly rather than
    re-reading the (append-mode, cross-run) log file.
    """
    cmd = [
        cmake_bin(),
        "-S",
        str(ROOT),
        "-B",
        str(build),
        *generator_args(),
        *launcher_args(),
        *args,
    ]
    return run_logged(cmd, cwd=ROOT, log=ctx.log, env=env)


def cmake_build(
    ctx: RunCtx,
    build: Path,
    *,
    config: str | None = None,
    target: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    cmd = [cmake_bin(), "--build", str(build), "--parallel", str(ctx.jobs)]
    if config:
        cmd += ["--config", config]
    if target:
        cmd += ["--target", target]
    rc, _ = run_logged(cmd, cwd=ROOT, log=ctx.log, env=env)
    return rc


def ctest_run(
    ctx: RunCtx,
    build: Path,
    *,
    config: str | None = None,
    filter_regex: str | None = None,
    exclude_slow: bool = True,
    timeout: int = DEFAULT_CTEST_TIMEOUT,
    env: dict[str, str] | None = None,
    junit: Path | None = None,
) -> int:
    cmd = [
        "ctest",
        "--test-dir",
        str(build),
        "--output-on-failure",
        "-j",
        str(ctx.jobs),
        "--timeout",
        str(timeout),
    ]
    if config:
        cmd += ["-C", config]
    if junit:
        # Must be absolute: ctest resolves a relative --output-junit against --test-dir.
        cmd += ["--output-junit", str(junit.resolve())]
    if filter_regex:
        cmd += ["-R", filter_regex]
    excluded_now = False
    if exclude_slow and not ctx.slow:
        cmd += ["-E", SLOW_TEST_REGEX]
        excluded_now = True
    if excluded_now:
        log_write(
            ctx.log,
            f"\n[verify_local] excluding slow tier (-E '{SLOW_TEST_REGEX}'); pass --slow to include it\n",
        )
    rc, _ = run_logged(cmd, cwd=ROOT, log=ctx.log, env=env)
    return rc


# --------------------------------------------------------------------------------------
# Per-config runners. Each mirrors one job (or the ubuntu-latest slice of a matrixed
# job) from a workflow file 1:1 -- flags and env are copied verbatim from the yml, not
# re-derived, so `ci/tests/test_verify_local.py` can catch drift by grepping this file.
# --------------------------------------------------------------------------------------


def run_release(ctx: RunCtx) -> bool:
    """c-unit.yml `build (release)` + its slice of `run-linux`: -DMI_PPROF=ON, Release."""
    build = ctx.dir / "build"
    rc, _ = cmake_configure(ctx, build, ["-DMI_PPROF=ON"])
    if rc:
        return False
    if cmake_build(ctx, build, config="Release"):
        return False
    return ctest_run(ctx, build, config="Release") == 0


def run_off(ctx: RunCtx) -> bool:
    """c-unit.yml `build (pprof-off)` + its slice of `run-linux`: -DMI_PPROF=OFF."""
    build = ctx.dir / "build"
    rc, _ = cmake_configure(ctx, build, ["-DMI_PPROF=OFF"])
    if rc:
        return False
    if cmake_build(ctx, build):
        return False
    return ctest_run(ctx, build) == 0


def run_debug_full(ctx: RunCtx) -> bool:
    """c-unit.yml `build (debug-full)` + its slice of `run-linux`: -DMI_PPROF=ON -DMI_DEBUG_FULL=ON, Debug."""
    build = ctx.dir / "build"
    rc, _ = cmake_configure(ctx, build, ["-DMI_PPROF=ON", "-DMI_DEBUG_FULL=ON"])
    if rc:
        return False
    if cmake_build(ctx, build, config="Debug"):
        return False
    return ctest_run(ctx, build, config="Debug") == 0


def run_debug3_extra(ctx: RunCtx) -> bool:
    """c-unit.yml `build (debug3-extra)` (#312): MI_DEBUG=3 reaching the compiler via
    `-DMI_EXTRA_CPPDEFS=MI_DEBUG=3` instead of `-DMI_DEBUG_FULL=ON`. CI only links
    `mimalloc-test-api` (the failure was an undefined reference at executable link time);
    this local config additionally runs a small ctest subset.

    Only `test-fork-locks*` and `test-api*` run here (ctest -R 'lock|api'), not the full
    suite `debug-full` covers -- and notably not #167's `test-lock-reentrancy` /
    `-uncleared-owner` / `-nonowner-release` / `-destroy-owned` positive controls, which
    are separately registered under `if(MI_DEBUG_FULL)` in CMakeLists.txt (the same
    option-vs-effective-value gap this issue is about, but for *test* registration rather
    than the *source* compiled -- there's no CMake-side way to see a MI_DEBUG=N smuggled
    into CMAKE_C_FLAGS at the point those tests get registered, so left out of #312's fix).
    """
    build = ctx.dir / "build"
    rc, configure_out = cmake_configure(
        ctx,
        build,
        ["-DCMAKE_BUILD_TYPE=Debug", "-DMI_PPROF=ON", "-DMI_EXTRA_CPPDEFS=MI_DEBUG=3"],
    )
    if rc:
        return False
    # #312: MI_DEBUG=3 must actually reach the compiler through this route (it collides
    # with the MI_DEBUG=2 that CMAKE_BUILD_TYPE=Debug also appends; the later -D wins).
    if not re.search(r"Compiler defines\s*:.*MI_DEBUG=3", configure_out):
        log_write(ctx.log, "\n[verify_local] FAIL: MI_DEBUG=3 did not reach mi_defines\n")
        return False
    if cmake_build(ctx, build, config="Debug"):
        return False
    return ctest_run(ctx, build, config="Debug", filter_regex="lock|api") == 0


def run_bundle(ctx: RunCtx) -> bool:
    """Bundle/ctest pass-and-fail equivalence: -DMI_PPROF=ON -DMI_DEBUG_FULL=ON, Debug.

    This is the half of #277 phase A's `bundle-roundtrip` job that CI can no longer run:
    since #307 the run stage has no build tree, so there is no native ctest result to
    compare against there (it checks executed-vs-registered *names* instead, with
    ci/bundle_coverage.py). A build tree exists here, so the stronger claim -- same names
    AND same pass/fail -- is still worth making, and this is where it lives now.

    Builds, records a reference `ctest --output-junit`, bundles with
    `ci/bundle_tests.py`, moves the build tree away so the executables' RPATH cannot
    rescue a broken bundle, then replays with `ci/run_test_bundle.py --compare-junit`
    and requires the same test names with the same pass/fail (#277 phase A).

    Note the absolute `--output-junit` path: ctest resolves a relative one against the
    *build* directory, so a relative path would be carried off by the move.
    """
    build = ctx.dir / "build"
    bundle = ctx.dir / "bundle"
    junit = ctx.dir / "ctest.xml"
    moved = ctx.dir / "build.moved-away"
    for stale in (bundle, moved):
        if stale.exists():
            shutil.rmtree(stale)
    rc, _ = cmake_configure(ctx, build, ["-DMI_PPROF=ON", "-DMI_DEBUG_FULL=ON"])
    if rc:
        return False
    if cmake_build(ctx, build, config="Debug"):
        return False
    # The comparison is only meaningful if the reference ran the whole suite, so the
    # slow tier is NOT excluded here regardless of --slow.
    if ctest_run(
        ctx,
        build,
        config="Debug",
        exclude_slow=False,
        timeout=DEFAULT_CTEST_TIMEOUT,
        junit=junit,
    ):
        return False
    rc, _ = run_logged(
        [
            sys.executable,
            str(ROOT / "ci" / "bundle_tests.py"),
            str(build),
            str(bundle),
            "--config",
            "Debug",
        ],
        cwd=ROOT,
        log=ctx.log,
    )
    if rc:
        return False
    build.rename(moved)
    try:
        rc, _ = run_logged(
            [
                sys.executable,
                str(ROOT / "ci" / "run_test_bundle.py"),
                str(bundle),
                "--compare-junit",
                str(junit),
            ],
            cwd=ROOT,
            log=ctx.log,
        )
    finally:
        moved.rename(build)
    return rc == 0


def run_guarded(ctx: RunCtx) -> bool:
    """c-unit.yml `build (guarded)` + its slice of `run-linux`: Debug + MI_GUARDED=ON, run
    twice -- plain, then with MIMALLOC_GUARDED_SAMPLE_RATE=1 forcing guarding on every
    allocation (in CI that second pass is an `--env-variant` scoped to this bundle)."""
    build = ctx.dir / "build-guarded"
    rc, configure_out = cmake_configure(
        ctx, build, ["-DCMAKE_BUILD_TYPE=Debug", "-DMI_PPROF=ON", "-DMI_GUARDED=ON"]
    )
    if rc:
        return False
    # The whole point of #116 is that the flag silently did not reach the compiler --
    # assert on THIS configure's resolved defines, not on the option passed in (and not
    # on the append-mode log file, which would still show a stale PASS from a previous
    # run of this same config after a real regression).
    if not re.search(r"Compiler defines\s*:.*MI_GUARDED=1", configure_out):
        log_write(ctx.log, "\n[verify_local] FAIL: MI_GUARDED=1 did not reach mi_defines\n")
        return False
    if cmake_build(ctx, build):
        return False
    if ctest_run(ctx, build, timeout=900) != 0:
        return False
    return ctest_run(ctx, build, timeout=900, env={"MIMALLOC_GUARDED_SAMPLE_RATE": "1"}) == 0


def run_shared(ctx: RunCtx) -> bool:
    """c-unit.yml `build (shared)` + its slice of `run-linux`: shared lib only, no static/object."""
    build = ctx.dir / "build"
    args = [
        "-DMI_PPROF=ON",
        "-DMI_BUILD_SHARED=ON",
        "-DMI_BUILD_STATIC=OFF",
        "-DMI_BUILD_OBJECT=OFF",
    ]
    rc, _ = cmake_configure(ctx, build, args)
    if rc:
        return False
    if cmake_build(ctx, build, config="Release"):
        return False
    return ctest_run(ctx, build, config="Release") == 0


def run_gate_binary_pinned(ctx: RunCtx, binary: Path, out_dir: Path, runs: int = 8) -> list[str]:
    """Run `binary` `runs` times, each writing MI_BENCH_JSON to its own file under
    `out_dir`, pinned via the external `taskset` command to <= memory_gate.MAX_GATE_CPUS
    CPUs (matching the committed min-of-N baselines, which assume a 4-core run).

    Deliberately NOT `memory_gate.run_gate_binary`: that function pins CPUs with
    `subprocess.Popen(..., preexec_fn=...)`, and forking via `preexec_fn` from a worker
    thread while nine other configs' threads are alive is documented by the `os` module
    as deadlock-prone (the child only inherits the forking thread, so a lock held by
    another thread at fork time is held forever in the child). `taskset` is an external
    process, so pinning happens with no fork from this (multi-threaded) interpreter.
    """
    import memory_gate

    cpu_count = os.cpu_count() or memory_gate.MAX_GATE_CPUS
    pin = list(range(min(memory_gate.MAX_GATE_CPUS, cpu_count)))
    have_taskset = shutil.which("taskset") is not None
    out_dir.mkdir(parents=True, exist_ok=True)
    result_paths: list[str] = []
    for i in range(1, runs + 1):
        out_path = out_dir / f"result-{i}.json"
        cmd = (
            ["taskset", "-c", ",".join(str(c) for c in pin), str(binary)]
            if have_taskset and pin
            else [str(binary)]
        )
        run_logged(cmd, cwd=ROOT, log=ctx.log, env={"MI_BENCH_JSON": str(out_path)})
        result_paths.append(str(out_path))
    return result_paths


def run_memory_gate(ctx: RunCtx) -> bool:
    """c-unit.yml `run-linux`'s memory gate. Calls ci/memory_gate.py's `check`/
    `control` comparison functions directly (rather than shelling to `memory_gate.py
    check` with no args) so binary discovery is pinned to THIS config's own build dir
    -- with several configs building concurrently, the module's mtime-based
    auto-discovery across build*/out/* would be a race.
    """
    import memory_gate

    build = ctx.dir / "build"
    rc, _ = cmake_configure(ctx, build, ["-DMI_PPROF=ON"])
    if rc:
        return False
    if cmake_build(ctx, build, config="Release"):
        return False
    if ctest_run(ctx, build, config="Release", filter_regex="test-memory-gate") != 0:
        return False

    def find_binary(under: Path) -> Path | None:
        for name in ("mimalloc-test-memory-gate", "mimalloc-test-memory-gate.exe"):
            for candidate in under.rglob(name):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate
        return None

    binary = find_binary(build)
    if binary is None:
        log_write(ctx.log, "\n[verify_local] FAIL: mimalloc-test-memory-gate binary not found\n")
        return False

    result_paths = run_gate_binary_pinned(ctx, binary, ctx.dir / "results")
    rc, _ = run_captured(
        ctx.log, lambda: memory_gate.check(result_paths), label="memory_gate.check"
    )
    if rc == 2:
        log_write(
            ctx.log, "\n[verify_local] WARNING: no committed baseline for this platform yet\n"
        )
    elif rc != 0:
        return False

    # Positive control: the gate must catch an injected leak, or it is decoration.
    build_leak = ctx.dir / "build-leak"
    rc, _ = cmake_configure(ctx, build_leak, ["-DMI_PPROF=ON", "-DMI_BENCH_INJECT_LEAK=600000"])
    if rc:
        return False
    if cmake_build(ctx, build_leak, config="Release", target="mimalloc-test-memory-gate"):
        return False
    leak_binary = find_binary(build_leak)
    if leak_binary is None:
        log_write(
            ctx.log, "\n[verify_local] FAIL: leaky mimalloc-test-memory-gate binary not found\n"
        )
        return False
    leak_paths = run_gate_binary_pinned(ctx, leak_binary, ctx.dir / "results-leak")
    control_rc, _ = run_captured(
        ctx.log, lambda: memory_gate.control(leak_paths), label="memory_gate.control"
    )
    return control_rc == 0


def run_diag(ctx: RunCtx) -> bool:
    """c-unit.yml `run-linux`'s diagnostic gates AND its x64 `isa-baseline` scans --
    both are cheap, ubuntu-only diagnostic checks over the same kind of throwaway
    `mimalloc-static`-only build, so they share one config. (In CI those builds are the
    `diag-pprof-*` and `isa-*` rows of the `build` matrix and the scans happen in the run
    stage; here there is one machine, so build and scan stay together.)

    Shells out to `uv run ci/<script>.py` for each check (rather than importing the
    module and calling its argparse-driven `main()` in-process) so there is no need to
    juggle a process-global `sys.argv` from a worker thread that runs concurrently with
    every other config. `uv run` (rather than a bare `python3`) guarantees the
    interpreter running each script is the one `uv` resolves, not whatever `python3`
    happens to be first on PATH.
    """
    ok = True

    def py(*args: str) -> bool:
        rc, _ = run_logged(["uv", "run", f"ci/{args[0]}", *args[1:]], cwd=ROOT, log=ctx.log)
        return rc == 0

    ok = py("check_internal_state.py") and ok
    ok = py("check_internal_state.py", "--selftest") and ok
    ok = py("check_release_equivalence.py", "--selftest") and ok

    for pprof in ("ON", "OFF"):
        build = ctx.dir / f"release-{pprof.lower()}"
        args = [
            "-DCMAKE_BUILD_TYPE=Release",
            "-DMI_DEBUG=OFF",
            f"-DMI_PPROF={pprof}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        rc, _ = cmake_configure(ctx, build, args)
        if rc:
            return False
        compile_commands = build / "compile_commands.json"
        if compile_commands.is_file() and "diagnostic.c" in compile_commands.read_text(
            encoding="utf-8"
        ):
            log_write(
                ctx.log,
                f"\n[verify_local] FAIL: diagnostic.c entered the MI_DEBUG=0 {pprof} build\n",
            )
            return False
        if cmake_build(ctx, build, target="mimalloc-static"):
            return False
        libs = list(build.glob("libmimalloc*.a"))
        if not libs:
            log_write(ctx.log, f"\n[verify_local] FAIL: no libmimalloc*.a produced for {pprof}\n")
            return False
        _, nm_out = run_logged(["nm", "-a", str(libs[0])], cwd=ROOT, log=ctx.log)
        if re.search(r"_mi_.*diagnostic|_mi_lock_debug", nm_out):
            log_write(
                ctx.log,
                f"\n[verify_local] FAIL: diagnostic symbols in the MI_DEBUG=0 {pprof} library\n",
            )
            return False

    ok = py("check_isa_baseline.py", "--selftest") and ok

    build_portable = ctx.dir / "build-portable"
    rc, _ = cmake_configure(ctx, build_portable, ["-DMI_PPROF=ON", "-DMI_NO_OPT_ARCH=ON"])
    if rc:
        return False
    if cmake_build(ctx, build_portable, target="mimalloc-static"):
        return False
    portable_libs = list(build_portable.glob("libmimalloc*.a"))
    if not portable_libs:
        return False
    ok = py("check_isa_baseline.py", str(portable_libs[0])) and ok

    build_arch = ctx.dir / "build-arch"
    rc, _ = cmake_configure(ctx, build_arch, ["-DMI_PPROF=ON", "-DMI_OPT_ARCH=ON"])
    if rc:
        return False
    if cmake_build(ctx, build_arch, target="mimalloc-static"):
        return False
    arch_libs = list(build_arch.glob("libmimalloc*.a"))
    if not arch_libs:
        return False
    return py("check_isa_baseline.py", str(arch_libs[0]), "--expect-dirty") and ok


def run_rust(ctx: RunCtx) -> bool:
    """rust-native.yml job `test` (ubuntu-latest slice), simplified to a plain,
    un-cached `cargo` invocation (no soldr wrapper) with its own CARGO_TARGET_DIR so
    it never contends with another cargo process building the same workspace.
    """
    rust_dir = ROOT / "rust"
    env = {
        "CARGO_TARGET_DIR": str(ctx.dir / "target"),
        "CARGO_BUILD_JOBS": str(ctx.jobs),
    }
    rc, _ = run_logged(
        ["cargo", "run", "-p", "xtask", "--", "check"], cwd=rust_dir, log=ctx.log, env=env
    )
    if rc:
        return False
    # rust-native.yml's "Rust binding surface covers the fork's C API" step. Cheap, and it
    # names a missing binding as a missing binding instead of letting it surface (or not
    # surface at all) three steps later.
    rc, _ = run_logged(["uv", "run", "ci/check_rust_surface.py"], cwd=ROOT, log=ctx.log)
    if rc:
        return False
    rc, _ = run_logged(["cargo", "test", "--workspace"], cwd=rust_dir, log=ctx.log, env=env)
    return rc == 0


def run_lint(ctx: RunCtx) -> bool:
    """python-lint.yml job `lint` (its only job, already ubuntu-latest).

    Every tool/script below runs through `uv run --with <pkg>==<version> ...` --
    pinned to the same versions `.github/workflows/python-lint.yml`'s `pip install`
    line uses -- rather than a bare `ruff`/`pyright`/`python3`, so this config never
    depends on (or silently drifts from) whatever happens to be on the ambient PATH.
    """
    ok = True
    rc, _ = run_logged(
        ["uv", "run", "--with", "ruff==0.12.10", "ruff", "check", "ci/"], cwd=ROOT, log=ctx.log
    )
    ok = ok and rc == 0
    rc, _ = run_logged(
        ["uv", "run", "--with", "ruff==0.12.10", "ruff", "format", "--check", "ci/"],
        cwd=ROOT,
        log=ctx.log,
    )
    ok = ok and rc == 0
    rc, _ = run_logged(
        ["uv", "run", "--with", "pyright[nodejs]==1.1.411", "pyright"], cwd=ROOT, log=ctx.log
    )
    ok = ok and rc == 0

    for cmd in (
        ["uv", "run", "ci/check_isa_baseline.py", "--selftest"],
        ["uv", "run", "ci/check_internal_state.py", "--selftest"],
        ["uv", "run", "ci/check_rust_surface.py", "--selftest"],
        ["uv", "run", "ci/check_rust_surface.py"],
        ["uv", "run", "ci/check_isa_baseline.py", "--help"],
        ["uv", "run", "ci/check_release_equivalence.py", "--help"],
        # The committed chart SVGs must still re-render byte-for-byte from the
        # committed CSV/JSON. Nothing ran these before, so a renderer edit could ship
        # SVGs whose captions no longer matched the data they claim to come from.
        # `--check` re-renders and compares; it never re-measures.
        ["uv", "run", "ci/bench_hole_purging.py", "--check"],
        ["uv", "run", "ci/bench_hole_purging.py", "--check", "--table"],
        ["uv", "run", "ci/bench_hole_purging_allocators.py", "--check"],
        ["uv", "run", "ci/bench_hole_purging_allocators.py", "--check", "--table"],
        # Same idea for the README's allocator feature table and its two SVGs: they
        # render from docs/allocator-features.json, and `--check` fails if any of the
        # three has drifted from it.
        ["uv", "run", "ci/render_feature_table.py", "--check"],
        # Selective macOS lane helpers (#339): the label check that runs on every
        # cross-built bundle and the PR-diff decision that picks the lane.
        ["uv", "run", "ci/check_macos_labels.py", "--selftest"],
        ["uv", "run", "ci/macos_lane_decide.py", "--selftest"],
    ):
        rc, _ = run_logged(cmd, cwd=ROOT, log=ctx.log)
        ok = ok and rc == 0
    rc, out = run_logged(["uv", "run", "ci/memory_gate.py"], cwd=ROOT, log=ctx.log)
    ok = ok and "Exit codes" in out

    # These four import `yaml`, which python-lint.yml's job-level `pip install`
    # provides for every subsequent bare `python3` call in that job. Nothing here
    # guarantees the ambient interpreter has PyYAML, so run them the same way
    # ci/tests gets it below: an ephemeral `uv run --with pyyaml==...` environment.
    for script in (
        "ci/check_benchmark_workflow.py",
        "ci/check_benchmark_memory_workflow.py",
        "ci/check_benchmark_latency_workflow.py",
        "ci/check_benchmark_scaling_workflow.py",
    ):
        rc, _ = run_logged(
            ["uv", "run", "--with", "pyyaml==6.0.2", script, "--selftest"],
            cwd=ROOT,
            log=ctx.log,
        )
        ok = ok and rc == 0

    # Issue #277 phase B2: no workflow -- nor azure-pipelines.yml -- may schedule onto a
    # native macOS runner. Run bare, unlike the four above: this script carries a PEP-723
    # header declaring its own PyYAML, so the command in its docstring works on a fresh
    # checkout. Injecting --with here would hide a regression in that header.
    rc, _ = run_logged(["uv", "run", "ci/lint_no_macos_runners.py"], cwd=ROOT, log=ctx.log)
    ok = ok and rc == 0

    rc, _ = run_logged(
        [
            "uv",
            "run",
            "--with",
            "pyyaml==6.0.2",
            "--with",
            "pytest==8.3.4",
            "pytest",
            "ci/tests",
            "-q",
        ],
        cwd=ROOT,
        log=ctx.log,
    )
    return ok and rc == 0


# Issue #301. `MI_TRACK_ASAN` is not a compiler flag we control -- CMakeLists probes for
# `sanitizer/asan_interface.h` and silently resolves the option to OFF when it is missing.
# On a distribution whose clang ships without compiler-rt's headers (Nix, some minimal
# containers) the previous clang-only config could not configure at all, and every agent
# hand-rolled a gcc ASan build instead -- which is how #301 sat unreproduced in CI. So
# probe instead of assuming: prefer clang (what asan.yml's Debug row uses), fall back to
# gcc, and SKIP with the reason when neither compiler can see the header.
ASAN_PROBE_SOURCE = "#include <sanitizer/asan_interface.h>\nint main(void){return 0;}\n"


_ASAN_TOOLCHAIN_CACHE: dict[str, tuple[str, str] | None] = {}


def _asan_toolchain() -> tuple[str, str] | None:
    """(cc, cxx) for the first compiler on PATH whose <sanitizer/asan_interface.h> resolves,
    or None. Cached, since both `needs()` and the runner ask."""
    if "value" in _ASAN_TOOLCHAIN_CACHE:
        return _ASAN_TOOLCHAIN_CACHE["value"]
    found: tuple[str, str] | None = None
    for cc, cxx in (("clang", "clang++"), ("gcc", "g++")):
        if shutil.which(cc) is None or shutil.which(cxx) is None:
            continue
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "probe.c"
            src.write_text(ASAN_PROBE_SOURCE, encoding="utf-8")
            try:
                rc = subprocess.run(
                    [cc, "-fsyntax-only", str(src)],
                    cwd=td,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                ).returncode
            except (OSError, subprocess.SubprocessError):
                continue
        if rc == 0:
            found = (cc, cxx)
            break
    _ASAN_TOOLCHAIN_CACHE["value"] = found
    return found


def _need_asan_toolchain() -> str | None:
    if _asan_toolchain() is None:
        return (
            "no compiler on PATH resolves <sanitizer/asan_interface.h> "
            "(tried clang then gcc); CMake would silently set MI_TRACK_ASAN=OFF"
        )
    return None


def _run_asan_one(ctx: RunCtx, cc: str, cxx: str, label: str, cmake_args: list[str]) -> bool:
    """One asan.yml matrix row: configure, prove ASan survived, build, ctest, UAF control.
    `cmake_args` is copied verbatim from the workflow (ci/tests/test_verify_local.py expands
    the matrix and checks it), so build it at the call site rather than from `label`."""
    build = ctx.dir / f"build-asan-{label}"
    env = {"CC": cc, "CXX": cxx}
    rc, configure_out = cmake_configure(ctx, build, cmake_args, env=env)
    if rc:
        return False
    if "Compile with address sanitizer support (MI_TRACK_ASAN=ON)" not in configure_out:
        log_write(ctx.log, "\n[verify_local] FAIL: MI_TRACK_ASAN did not survive configure\n")
        return False
    if cmake_build(ctx, build, env=env):
        return False

    api_candidates = list(build.rglob("mimalloc-test-api"))
    if not api_candidates:
        log_write(ctx.log, "\n[verify_local] FAIL: mimalloc-test-api not built\n")
        return False
    _, nm_out = run_logged(["nm", "-C", str(api_candidates[0])], cwd=ROOT, log=ctx.log)
    if "__asan_init" not in nm_out:
        log_write(
            ctx.log, "\n[verify_local] FAIL: mimalloc-test-api does not reference __asan_init\n"
        )
        return False

    if ctest_run(ctx, build, timeout=600) != 0:
        return False

    control_candidates = [
        p
        for p in build.rglob("mimalloc-test-asan-control")
        if p.is_file() and os.access(p, os.X_OK)
    ]
    if not control_candidates:
        log_write(ctx.log, "\n[verify_local] FAIL: mimalloc-test-asan-control not built\n")
        return False
    control_rc, control_out = run_logged([str(control_candidates[0])], cwd=ROOT, log=ctx.log)
    if control_rc == 0:
        log_write(
            ctx.log,
            "\n[verify_local] FAIL: asan control exited 0 -- did not catch a use-after-free\n",
        )
        return False
    if not re.search(
        r"AddressSanitizer|use-after-poison|heap-use-after-free", control_out, re.IGNORECASE
    ):
        log_write(
            ctx.log,
            "\n[verify_local] FAIL: control failed but produced no AddressSanitizer report\n",
        )
        return False
    return True


def run_asan(ctx: RunCtx) -> bool:
    """asan.yml job `asan` (already ubuntu-latest only), both build types.

    The RelWithDebInfo row is not a duplicate of the Debug one: `MI_TRACK_ASAN` implies
    `MI_PADDING`, and CMakeLists auto-enables `MI_OPT_FREE_SMALL` (hence
    `MI_PAGE_META_ALIGNED_FREE_SMALL`) only when `MI_DEBUG` is off, so the Debug lane never
    executes `mi_free_small`'s page-from-alignment fast path -- the one #301's SEGV lived on.
    """
    toolchain = _asan_toolchain()
    if toolchain is None:  # pragma: no cover -- guarded by _need_asan_toolchain
        log_write(ctx.log, "\n[verify_local] FAIL: no ASan-capable compiler\n")
        return False
    cc, cxx = toolchain
    log_write(ctx.log, f"\n[verify_local] asan toolchain: CC={cc} CXX={cxx}\n")
    debug_ok = _run_asan_one(
        ctx,
        cc,
        cxx,
        "debug",
        ["-DCMAKE_BUILD_TYPE=Debug", "-DMI_PPROF=ON", "-DMI_DEBUG_FULL=ON", "-DMI_TRACK_ASAN=ON"],
    )
    # `and` in this order, not short-circuited away by `debug_ok and ...`: --keep-going is
    # about seeing every failure in one run, and the release row is the interesting one here.
    release_ok = _run_asan_one(
        ctx,
        cc,
        cxx,
        "relwithdebinfo",
        ["-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DMI_PPROF=ON", "-DMI_TRACK_ASAN=ON"],
    )
    return release_ok and debug_ok


@dataclasses.dataclass(frozen=True)
class ConfigSpec:
    name: str
    job: str
    description: str
    runner: Callable[[RunCtx], bool]
    needs: Callable[[], str | None] = lambda: None  # returns a SKIP reason, or None to run


def _need_clang() -> str | None:
    if shutil.which("clang") is None or shutil.which("clang++") is None:
        return "clang/clang++ not found on PATH"
    return None


def _need_uv() -> str | None:
    if shutil.which("uv") is None:
        return "uv not found on PATH; install it (https://docs.astral.sh/uv/) -- this config pins every tool/script version through `uv run --with ...`"
    return None


CONFIGS: list[ConfigSpec] = [
    ConfigSpec(
        "release",
        "c-unit.yml: build(release)+run-linux",
        "Release, MI_PPROF=ON, full ctest",
        run_release,
    ),
    ConfigSpec(
        "off", "c-unit.yml: build(pprof-off)+run-linux", "MI_PPROF=OFF, full ctest", run_off
    ),
    ConfigSpec(
        "debug-full",
        "c-unit.yml: build(debug-full)+run-linux",
        "Debug, MI_DEBUG_FULL=ON",
        run_debug_full,
    ),
    ConfigSpec(
        "debug3-extra",
        "c-unit.yml: build(debug3-extra)+run-linux",
        "Debug, MI_EXTRA_CPPDEFS=MI_DEBUG=3, lock|api ctest subset",
        run_debug3_extra,
    ),
    ConfigSpec(
        "guarded",
        "c-unit.yml: build(guarded)+run-linux",
        "Debug, MI_GUARDED=ON, run x2",
        run_guarded,
    ),
    ConfigSpec(
        "shared",
        "c-unit.yml: build(shared)+run-linux",
        "shared lib only, no static/object",
        run_shared,
    ),
    ConfigSpec(
        "bundle",
        "local only: bundle pass/fail equivalence",
        "ctest vs. portable test bundle, build tree removed",
        run_bundle,
    ),
    ConfigSpec(
        "memory-gate",
        "c-unit.yml: run-linux memory gate",
        "min-of-8 peak-memory regression gate",
        run_memory_gate,
    ),
    ConfigSpec(
        "diag",
        "c-unit.yml: run-linux diag + isa(x64)",
        "internal-state/release/ISA gates",
        run_diag,
        _need_uv,
    ),
    ConfigSpec("rust", "rust-native.yml: test", "xtask check + cargo test --workspace", run_rust),
    ConfigSpec(
        "lint",
        "python-lint.yml: lint",
        "ruff + pyright + gate selftests + pytest",
        run_lint,
        _need_uv,
    ),
    ConfigSpec(
        "asan",
        "asan.yml: asan",
        "ASan Debug + RelWithDebInfo builds, ctest, UAF positive control",
        run_asan,
        _need_asan_toolchain,
    ),
]

CONFIG_NAMES = [c.name for c in CONFIGS]


# ---------------------------------------------------------------------------------
# Cross bundles (#277 phase F)
#
# macOS and Windows are not executed here -- there is no Mac and no Windows on this box,
# and by owner requirement there is no native Mac anywhere in this repository's CI either
# (ci/lint_no_macos_runners.py). What IS reproducible locally is everything up to the
# execution step: `soldr prepare` provisions the same cross toolchain the runner uses,
# cmake/toolchains/soldr-<triple>.cmake feeds CMake nothing but what soldr exported, and
# ci/bundle_tests.py turns the build tree into the same portable bundle the workflow
# uploads. So a red `run-macos-x64-recovery` or `run-windows` that is really a *build* or
# *bundling* failure can be reproduced in one command instead of a push-and-wait cycle.
#
# The flags below are copied verbatim out of macos-bundles.yml's and windows-bundles.yml's
# matrices, exactly like the CONFIGS table above copies c-unit.yml's, and
# ci/tests/test_verify_local.py parses those matrices and fails if the two drift.

BUNDLE_OUT_ROOT = OUT_ROOT / "bundles"

# The `-- Link libraries   : ...` line each lane's configure must resolve to. This is the
# assertion the build jobs make, and on Darwin it is load-bearing rather than cosmetic:
# without the toolchain file's CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY, CMakeLists.txt's
# find_link_library() falls back to find_library() and hands the host's ELF librt.so to a
# Mach-O link.
DARWIN_LINK_LIBRARIES = "pthread"
WINDOWS_LINK_LIBRARIES = "psapi;shell32;user32;advapi32;bcrypt"


@dataclasses.dataclass(frozen=True)
class BundleSpec:
    """One row of a bundle workflow's build matrix, reproduced locally."""

    name: str  # the matrix's `bundle:` value, and the artifact name CI uploads
    workflow: str
    job: str
    triple: str
    cmake: str  # the matrix's `cmake:` value, verbatim
    link_libraries: str
    # ci/bundle_tests.py arguments, verbatim from the workflow's "Bundle the tests" step.
    # $VARS are expanded from the environment `soldr prepare` exported, not from this
    # shell's -- MINGW_W64_CROSS_BIN only exists after a prepare.
    bundle_args: tuple[str, ...] = ()


BUNDLES: list[BundleSpec] = [
    # --- macos-bundles.yml: build-macos -------------------------------------------
    BundleSpec(
        "macos-arm64-release",
        "macos-bundles.yml",
        "build-macos",
        "aarch64-apple-darwin",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON",
        DARWIN_LINK_LIBRARIES,
    ),
    BundleSpec(
        "macos-arm64-debug-full",
        "macos-bundles.yml",
        "build-macos",
        "aarch64-apple-darwin",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_DEBUG_FULL=ON -DMI_TLS_MODEL_FIXED=ON",
        DARWIN_LINK_LIBRARIES,
    ),
    BundleSpec(
        "macos-arm64-leak",
        "macos-bundles.yml",
        "build-macos",
        "aarch64-apple-darwin",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_BENCH_INJECT_LEAK=200000",
        DARWIN_LINK_LIBRARIES,
    ),
    BundleSpec(
        "macos-x64-release",
        "macos-bundles.yml",
        "build-macos",
        "x86_64-apple-darwin",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON",
        DARWIN_LINK_LIBRARIES,
    ),
    BundleSpec(
        "macos-x64-debug-full",
        "macos-bundles.yml",
        "build-macos",
        "x86_64-apple-darwin",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_DEBUG_FULL=ON",
        DARWIN_LINK_LIBRARIES,
    ),
    BundleSpec(
        "macos-x64-leak",
        "macos-bundles.yml",
        "build-macos",
        "x86_64-apple-darwin",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_BENCH_INJECT_LEAK=200000",
        DARWIN_LINK_LIBRARIES,
    ),
    # --- windows-bundles.yml: build-windows-gnu -----------------------------------
    # --dll-search-dir turns on bundle_tests.py's transitive PE import scan: soldr's
    # mingw-w64 links mimalloc.dll against libgcc_s_seh-1.dll, which no windows-latest
    # runner has, and a missing DLL is a dialog-free 0xC0000135 rather than a test failure.
    BundleSpec(
        "windows-gnu-x64-release",
        "windows-bundles.yml",
        "build-windows-gnu",
        "x86_64-pc-windows-gnu",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON",
        WINDOWS_LINK_LIBRARIES,
        (
            "--objdump",
            "$MINGW_W64_CROSS_BIN/x86_64-w64-mingw32-objdump",
            "--dll-search-dir",
            "$MINGW_W64_CROSS_ROOT/x86_64-w64-mingw32/lib",
        ),
    ),
    BundleSpec(
        "windows-gnu-x64-debug-full",
        "windows-bundles.yml",
        "build-windows-gnu",
        "x86_64-pc-windows-gnu",
        "-DCMAKE_BUILD_TYPE=Debug -DMI_PPROF=ON -DMI_DEBUG_FULL=ON",
        WINDOWS_LINK_LIBRARIES,
        (
            "--objdump",
            "$MINGW_W64_CROSS_BIN/x86_64-w64-mingw32-objdump",
            "--dll-search-dir",
            "$MINGW_W64_CROSS_ROOT/x86_64-w64-mingw32/lib",
        ),
    ),
    BundleSpec(
        "windows-gnu-x64-shared",
        "windows-bundles.yml",
        "build-windows-gnu",
        "x86_64-pc-windows-gnu",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_BUILD_SHARED=ON -DMI_BUILD_STATIC=OFF -DMI_BUILD_OBJECT=OFF",
        WINDOWS_LINK_LIBRARIES,
        (
            "--objdump",
            "$MINGW_W64_CROSS_BIN/x86_64-w64-mingw32-objdump",
            "--dll-search-dir",
            "$MINGW_W64_CROSS_ROOT/x86_64-w64-mingw32/lib",
        ),
    ),
    BundleSpec(
        "windows-gnu-x64-leak",
        "windows-bundles.yml",
        "build-windows-gnu",
        "x86_64-pc-windows-gnu",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_BENCH_INJECT_LEAK=200000",
        WINDOWS_LINK_LIBRARIES,
        (
            "--objdump",
            "$MINGW_W64_CROSS_BIN/x86_64-w64-mingw32-objdump",
            "--dll-search-dir",
            "$MINGW_W64_CROSS_ROOT/x86_64-w64-mingw32/lib",
        ),
    ),
    # --- windows-bundles.yml: build-windows-msvc ----------------------------------
    # No --dll-search-dir: soldr's xwin splat is import libraries only. --check-dll-closure
    # runs the same scan anyway, and --allow-msvc-runtime is the one assumption this lane
    # makes (VCRUNTIME140/MSVCP140 come from the Visual C++ redistributable on the runner).
    BundleSpec(
        "windows-msvc-x64-release",
        "windows-bundles.yml",
        "build-windows-msvc",
        "x86_64-pc-windows-msvc",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON",
        WINDOWS_LINK_LIBRARIES,
        ("--objdump", "llvm-objdump", "--check-dll-closure", "--allow-msvc-runtime"),
    ),
    BundleSpec(
        "windows-msvc-x64-debug-full",
        "windows-bundles.yml",
        "build-windows-msvc",
        "x86_64-pc-windows-msvc",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_DEBUG_FULL=ON",
        WINDOWS_LINK_LIBRARIES,
        ("--objdump", "llvm-objdump", "--check-dll-closure", "--allow-msvc-runtime"),
    ),
    BundleSpec(
        "windows-msvc-x64-shared",
        "windows-bundles.yml",
        "build-windows-msvc",
        "x86_64-pc-windows-msvc",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_BUILD_SHARED=ON -DMI_BUILD_STATIC=OFF -DMI_BUILD_OBJECT=OFF",
        WINDOWS_LINK_LIBRARIES,
        ("--objdump", "llvm-objdump", "--check-dll-closure", "--allow-msvc-runtime"),
    ),
    BundleSpec(
        "windows-msvc-x64-leak",
        "windows-bundles.yml",
        "build-windows-msvc",
        "x86_64-pc-windows-msvc",
        "-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_BENCH_INJECT_LEAK=200000",
        WINDOWS_LINK_LIBRARIES,
        ("--objdump", "llvm-objdump", "--check-dll-closure", "--allow-msvc-runtime"),
    ),
]

BUNDLE_NAMES = [b.name for b in BUNDLES]


def soldr_prepare(triple: str, log: Path) -> dict[str, str] | None:
    """Run `soldr prepare --target <triple>` and return the environment it exported.

    Mirrors the workflows' "Prepare the soldr <X> toolchain" step, including its working
    directory: `soldr prepare` resolves rustc from a rust-toolchain.toml at or above the
    cwd, and this repository's lives in rust/.

    Returns None (after logging) if soldr is missing or the prepare failed. The exported
    environment is returned rather than applied to os.environ so a caller can hand it to
    exactly the subprocesses that should see it.
    """
    if shutil.which("soldr") is None:
        log_write(log, "\n[verify_local] soldr not found on PATH\n")
        return None
    env_file = BUNDLE_OUT_ROOT / f"soldr-{triple}.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("", encoding="utf-8")  # --github-env appends; start clean
    rc, _ = run_logged(
        ["soldr", "prepare", "--target", triple, "--github-env", str(env_file)],
        cwd=ROOT / "rust",
        log=log,
    )
    if rc:
        return None
    exported: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        # GitHub's env-file format also has a `KEY<<DELIM` heredoc shape. soldr does not
        # emit it today; parse it wrong silently and the toolchain file would fail with a
        # confusing "CC_<triple> is not set" much later, so refuse instead.
        if not sep or "<<" in key:
            log_write(log, f"\n[verify_local] unparseable soldr env line: {line!r}\n")
            return None
        exported[key] = value
    return exported


def build_one_bundle(spec: BundleSpec, jobs: int, log: Path) -> tuple[bool, Path | None]:
    """Configure, build and bundle one cross lane. Returns (ok, bundle dir)."""
    out = BUNDLE_OUT_ROOT / spec.name
    build = out / "build"
    bundle = out / "bundle"
    out.mkdir(parents=True, exist_ok=True)

    exported = soldr_prepare(spec.triple, log)
    if exported is None:
        return False, None
    # soldr prepends its own LLVM/ninja/cmake bin directories to PATH; llvm-objdump on the
    # MSVC lane comes from there, not from this host.
    env = dict(exported)

    ctx = RunCtx(name=spec.name, dir=out, log=log, jobs=jobs, slow=False)
    rc, configure_out = run_logged(
        [
            cmake_bin(),
            "-S",
            str(ROOT),
            "-B",
            str(build),
            "-G",
            "Ninja",
            *shlex.split(spec.cmake),
            "--toolchain",
            str(ROOT / "cmake" / "toolchains" / f"soldr-{spec.triple}.cmake"),
            "--log-level=VERBOSE",
        ],
        cwd=ROOT,
        log=log,
        env=env,
    )
    if rc:
        return False, None
    # The same assertion the build job makes, on the RESOLVED list rather than on the
    # option that was passed in.
    if not re.search(
        rf"^-- Link libraries *: *{re.escape(spec.link_libraries)}$", configure_out, re.MULTILINE
    ):
        log_write(
            log,
            f"\n[verify_local] the {spec.triple} link resolved unexpected libraries; "
            f"expected exactly '{spec.link_libraries}'\n",
        )
        for line in configure_out.splitlines():
            if "Link libraries" in line:
                log_write(log, f"  {line}\n")
        return False, None

    if cmake_build(ctx, build, env=env):
        return False, None

    if bundle.exists():
        shutil.rmtree(bundle)
    expanded: list[str] = []
    for arg in spec.bundle_args:
        try:
            expanded.append(Template(arg).substitute(env))
        except KeyError as exc:
            log_write(
                log,
                f"\n[verify_local] {arg!r} needs {exc} which `soldr prepare --target "
                f"{spec.triple}` did not export\n",
            )
            return False, None
    rc, _ = run_logged(
        [
            sys.executable,
            str(ROOT / "ci" / "bundle_tests.py"),
            str(build),
            str(bundle),
            *expanded,
        ],
        cwd=ROOT,
        log=log,
        env=env,
    )
    if rc:
        return False, None
    return True, bundle


def summarize_bundle(spec: BundleSpec, bundle: Path) -> None:
    """Print the bundle path and a manifest summary -- what CI would have uploaded."""
    manifest = json.loads((bundle / "tests.json").read_text(encoding="utf-8"))
    tests = manifest.get("tests", [])
    files = sorted(p.name for p in bundle.iterdir() if p.name != "tests.json")
    libs = [f for f in files if f.endswith((".dylib", ".dll", ".so"))]
    print()
    print(f"bundle    : {bundle}")
    print(f"lane      : {spec.workflow} :: {spec.job} ({spec.triple})")
    print(f"cmake     : {spec.cmake}")
    print(f"tests     : {len(tests)}")
    print(f"files     : {len(files)} ({len(libs)} shared libraries)")
    for lib in libs:
        print(f"    lib   : {lib}")
    for test in tests:
        argv = test.get("argv", [])
        extra: list[str] = []
        if test.get("expect_nonzero"):
            extra.append("expect-nonzero")
        if test.get("env"):
            extra.append(f"env[{len(test['env'])}]")
        suffix = f"  [{', '.join(extra)}]" if extra else ""
        print(f"    test  : {test.get('name')}  <- {' '.join(argv)}{suffix}")
    print()
    print("This host cannot execute it -- that is the one step this tool cannot")
    print("reproduce. Copy the directory to the target OS and replay it there with:")
    print(f"    uv run ci/run_test_bundle.py {bundle}")
    print("(CI does the same, from the artifact `bundle-" + spec.name + "`.)")


def run_bundle_build(name: str, jobs: int) -> int:
    """`--bundle <name>`: reproduce one cross lane's build+bundle stage locally."""
    spec = next((b for b in BUNDLES if b.name == name), None)
    if spec is None:
        print(f"error: unknown bundle {name!r}; choose from {BUNDLE_NAMES}", file=sys.stderr)
        return 2
    out = BUNDLE_OUT_ROOT / spec.name
    log = out / "verify.log"
    out.mkdir(parents=True, exist_ok=True)
    log_start(log, f"=== bundle {spec.name} starting {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    print(f"verify_local: building bundle {spec.name} for {spec.triple} (log: {log})")
    start = time.monotonic()
    ok, bundle = build_one_bundle(spec, jobs, log)
    elapsed = time.monotonic() - start
    if not ok or bundle is None:
        print(f"[{elapsed:7.1f}s] {spec.name:<28} FAIL")
        print_tail(log)
        return 1
    print(f"[{elapsed:7.1f}s] {spec.name:<28} PASS")
    summarize_bundle(spec, bundle)
    return 0


# ---------------------------------------------------------------------------------
# --like-ci (#307): the two-stage layout, locally.
#
# c-unit.yml no longer gives each configuration its own runner and its own serial
# ctest. It builds every configuration once, in parallel, and then ONE job replays
# every bundle in a single wave. The per-config CONFIGS table above still mirrors what
# each configuration *is*; this mode mirrors how they are now scheduled, which is the
# part that can break on its own (a bundle that cannot be replayed without a build
# tree, a test that only passes when it has the machine to itself, a coverage
# comparison that has drifted).
#
# The two musl rows of c-unit.yml's build matrix are not reproduced: they build and run
# inside `alpine:3.20`, and this script has no container runner -- the same, explicitly
# recorded gap as the old `ctest-musl` job (see ci/tests/test_verify_local.py's
# KNOWN_GAPS). Verify those with `docker run alpine:3.20 ...`; docs/ci-gates.md spells
# out the command.
# ---------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LikeCiBuild:
    """One row of c-unit.yml's `build` matrix, reproduced locally."""

    config: str
    cmake: tuple[str, ...]
    build_config: str | None = None
    target: str | None = None
    #: bundle = replayed in the wave; control = built but never executed (the memory
    #: gate's leaky positive control); lib = scanned, not executed.
    kind: str = "bundle"


LIKE_CI_BUILDS: list[LikeCiBuild] = [
    LikeCiBuild("release", ("-DMI_PPROF=ON",), "Release"),
    LikeCiBuild("pprof-off", ("-DMI_PPROF=OFF",)),
    LikeCiBuild("debug-full", ("-DMI_PPROF=ON", "-DMI_DEBUG_FULL=ON"), "Debug"),
    LikeCiBuild("guarded", ("-DCMAKE_BUILD_TYPE=Debug", "-DMI_PPROF=ON", "-DMI_GUARDED=ON")),
    LikeCiBuild(
        "shared",
        (
            "-DMI_PPROF=ON",
            "-DMI_BUILD_SHARED=ON",
            "-DMI_BUILD_STATIC=OFF",
            "-DMI_BUILD_OBJECT=OFF",
        ),
        "Release",
    ),
    LikeCiBuild(
        "memory-gate-leak",
        # See c-unit.yml's memory-gate-leak row for why 600000 rather than 200000.
        ("-DMI_PPROF=ON", "-DMI_BENCH_INJECT_LEAK=600000"),
        "Release",
        kind="control",
    ),
    LikeCiBuild(
        "isa-portable",
        ("-DMI_PPROF=ON", "-DMI_NO_OPT_ARCH=ON"),
        target="mimalloc-static",
        kind="lib",
    ),
    LikeCiBuild(
        "isa-arch",
        ("-DMI_PPROF=ON", "-DMI_OPT_ARCH=ON"),
        target="mimalloc-static",
        kind="lib",
    ),
    LikeCiBuild(
        "diag-pprof-on",
        (
            "-DCMAKE_BUILD_TYPE=Release",
            "-DMI_DEBUG=OFF",
            "-DMI_PPROF=ON",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ),
        target="mimalloc-static",
        kind="lib",
    ),
    LikeCiBuild(
        "diag-pprof-off",
        (
            "-DCMAKE_BUILD_TYPE=Release",
            "-DMI_DEBUG=OFF",
            "-DMI_PPROF=OFF",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ),
        target="mimalloc-static",
        kind="lib",
    ),
]

LIKE_CI_ROOT = OUT_ROOT / "like-ci"


def like_ci_build_one(row: LikeCiBuild, jobs: int) -> tuple[bool, Path]:
    """Configure, build, enumerate and (for a bundle row) bundle one configuration."""
    out = LIKE_CI_ROOT / row.config
    log = out / "verify.log"
    out.mkdir(parents=True, exist_ok=True)
    log_start(log, f"=== build {row.config} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    ctx = RunCtx(name=row.config, dir=out, log=log, jobs=jobs, slow=True)
    build = out / "build"
    rc, configure_out = cmake_configure(ctx, build, list(row.cmake))
    if rc:
        return False, log
    if row.config == "guarded" and not re.search(
        r"Compiler defines\s*:.*MI_GUARDED=1", configure_out
    ):
        log_write(log, "\n[verify_local] FAIL: MI_GUARDED=1 did not reach mi_defines\n")
        return False, log
    if cmake_build(ctx, build, config=row.build_config, target=row.target):
        return False, log
    if row.kind == "lib":
        return True, log

    # The reference side of the coverage comparison, taken from ctest rather than from
    # the bundle -- otherwise the check is the bundle agreeing with itself.
    manifests = LIKE_CI_ROOT / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    argv = ["ctest", "--test-dir", str(build), "--show-only=json-v1"]
    if row.build_config:
        argv += ["-C", row.build_config]
    rc, show_only = run_logged(argv, cwd=ROOT, log=log)
    if rc:
        return False, log
    (manifests / f"show-only-{row.config}.json").write_text(show_only, encoding="utf-8")

    bundle = LIKE_CI_ROOT / "bundle" / row.config
    if bundle.exists():
        shutil.rmtree(bundle)
    argv = [sys.executable, str(ROOT / "ci" / "bundle_tests.py"), str(build), str(bundle)]
    if row.build_config:
        argv += ["--config", row.build_config]
    rc, _ = run_logged(argv, cwd=ROOT, log=log)
    return rc == 0, log


def run_like_ci(jobs: int) -> int:
    """`--like-ci`: build every configuration once, then run every bundle in one wave.

    Stage 1 mirrors c-unit.yml's `build` matrix (configure, build, `ctest
    --show-only=json-v1`, `ci/bundle_tests.py`); stage 2 mirrors `run-linux`
    (`ci/run_test_bundle.py --bundles ... --jobs N`, then `ci/bundle_coverage.py`, the
    memory gate, the diagnostic/ISA scans and `ci/check_bun_surface.py`'s two halves).
    """
    LIKE_CI_ROOT.mkdir(parents=True, exist_ok=True)
    log = LIKE_CI_ROOT / "run.log"
    log_start(log, f"=== like-ci {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    per_build_jobs = max(2, jobs // max(1, len(LIKE_CI_BUILDS)))
    print(
        f"verify_local --like-ci: stage 1, {len(LIKE_CI_BUILDS)} configuration(s) built once "
        f"({per_build_jobs} job(s) each)"
    )
    failures: list[str] = []
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(len(LIKE_CI_BUILDS), max(1, jobs))) as pool:
        futures = {
            pool.submit(like_ci_build_one, row, per_build_jobs): row for row in LIKE_CI_BUILDS
        }
        for fut in as_completed(futures):
            row = futures[fut]
            ok, row_log = fut.result()
            print(f"    build {row.config:<18} {'PASS' if ok else 'FAIL'}")
            if not ok:
                failures.append(f"build {row.config}")
                print_tail(row_log)
    build_seconds = time.monotonic() - start
    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1

    bundles = [row.config for row in LIKE_CI_BUILDS if row.kind == "bundle"]
    # One invocation with the default `--select all`: this box is one machine, so the
    # serial group runs after the wave here. CI splits the same two halves across
    # `run-linux` and `run-linux-serial` (`--select parallel` / `--select serial`), which
    # is a scheduling difference only -- a serial test has nothing else alongside it
    # either way.
    print(f"verify_local --like-ci: stage 2, {len(bundles)} bundle(s) in one wave")
    results = LIKE_CI_ROOT / "results"
    if results.exists():
        shutil.rmtree(results)
    start = time.monotonic()
    rc, _ = run_logged(
        [
            sys.executable,
            str(ROOT / "ci" / "run_test_bundle.py"),
            "--bundles",
            *[str(LIKE_CI_ROOT / "bundle" / name) for name in bundles],
            "--jobs",
            str(jobs),
            "--env-variant",
            "guarded:sample-rate-1",
            "MIMALLOC_GUARDED_SAMPLE_RATE=1",
            "--junit-dir",
            str(results),
        ],
        cwd=ROOT,
        log=log,
    )
    run_seconds = time.monotonic() - start
    if rc:
        failures.append("run_test_bundle")
        print_tail(log, 60)

    # No test may be dropped: every name ctest registered must have been executed.
    compare: list[str] = []
    for name in bundles:
        compare += [
            "--compare",
            name,
            str(LIKE_CI_ROOT / "manifests" / f"show-only-{name}.json"),
            str(results / f"{name}.xml"),
        ]
    rc, _ = run_logged(
        [
            sys.executable,
            str(ROOT / "ci" / "bundle_coverage.py"),
            *compare,
            "--heading",
            "Coverage: registered by ctest vs executed by the run stage",
            "--names",
            "ctest --show-only",
            "executed",
        ],
        cwd=ROOT,
        log=log,
    )
    if rc:
        failures.append("bundle_coverage")

    if not like_ci_gates(jobs, log):
        failures.append("gates")

    print()
    print(f"stage 1 (build all): {build_seconds:7.1f}s")
    print(f"stage 2 (run all)  : {run_seconds:7.1f}s")
    print(f"log                : {log}")
    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    print("\nlike-ci: PASS")
    return 0


def like_ci_gates(jobs: int, log: Path) -> bool:
    """The run-stage gates that need the machine to themselves, in c-unit.yml's order.

    `ci/memory_gate.py` check + control, `ci/check_internal_state.py`,
    `ci/check_release_equivalence.py`, the diagnostic-symbol scan over the two
    `diag-pprof-*` libraries, `ci/check_isa_baseline.py` over the two `isa-*` libraries,
    and `ci/check_bun_surface.py --emit-objects` followed by `--objects` (the build/run
    split #307 introduced so the run stage never recompiles `src/static.c`).
    """
    import memory_gate

    ok = True
    ctx = RunCtx(name="like-ci", dir=LIKE_CI_ROOT, log=log, jobs=jobs, slow=True)

    def find_gate_binary(under: Path) -> Path | None:
        for name in ("mimalloc-test-memory-gate", "mimalloc-test-memory-gate.exe"):
            candidate = under / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    release_gate = find_gate_binary(LIKE_CI_ROOT / "bundle" / "release")
    leak_gate = find_gate_binary(LIKE_CI_ROOT / "bundle" / "memory-gate-leak")
    if release_gate is None or leak_gate is None:
        log_write(log, "\n[verify_local] FAIL: memory-gate binaries missing from the bundles\n")
        return False
    gate_rc, _ = run_captured(
        log,
        lambda: memory_gate.check(run_gate_binary_pinned(ctx, release_gate, LIKE_CI_ROOT / "gate")),
        label="memory_gate.check",
    )
    if gate_rc == 2:
        log_write(log, "\n[verify_local] WARNING: no committed baseline for this platform\n")
    elif gate_rc != 0:
        ok = False
    control_rc, _ = run_captured(
        log,
        lambda: memory_gate.control(
            run_gate_binary_pinned(ctx, leak_gate, LIKE_CI_ROOT / "gate-leak")
        ),
        label="memory_gate.control",
    )
    ok = ok and control_rc == 0

    def py(*args: str) -> bool:
        rc, _ = run_logged(["uv", "run", f"ci/{args[0]}", *args[1:]], cwd=ROOT, log=log)
        return rc == 0

    ok = py("check_internal_state.py") and ok
    ok = py("check_internal_state.py", "--selftest") and ok
    ok = py("check_release_equivalence.py", "--selftest") and ok

    for pprof in ("on", "off"):
        build = LIKE_CI_ROOT / f"diag-pprof-{pprof}" / "build"
        commands = build / "compile_commands.json"
        if commands.is_file() and "diagnostic.c" in commands.read_text(encoding="utf-8"):
            log_write(log, f"\n[verify_local] FAIL: diagnostic.c entered the {pprof} build\n")
            ok = False
        libs = sorted(build.glob("libmimalloc*.a"))
        if not libs:
            log_write(log, f"\n[verify_local] FAIL: no libmimalloc*.a for diag-pprof-{pprof}\n")
            ok = False
            continue
        _, nm_out = run_logged(["nm", "-a", str(libs[0])], cwd=ROOT, log=log)
        if re.search(r"_mi_.*diagnostic|_mi_lock_debug", nm_out):
            log_write(log, f"\n[verify_local] FAIL: diagnostic symbols in the {pprof} library\n")
            ok = False

    ok = py("check_isa_baseline.py", "--selftest") and ok
    for name, extra in (("isa-portable", []), ("isa-arch", ["--expect-dirty"])):
        libs = sorted((LIKE_CI_ROOT / name / "build").glob("libmimalloc*.a"))
        if not libs:
            log_write(log, f"\n[verify_local] FAIL: no libmimalloc*.a for {name}\n")
            ok = False
            continue
        ok = py("check_isa_baseline.py", str(libs[0]), *extra) and ok

    # Bun's consumer surface, in the two halves c-unit.yml uses: the build stage compiles
    # the objects, the run stage only links and runs them.
    objects = LIKE_CI_ROOT / "bun-objects"
    emitted = py("check_bun_surface.py", "--emit-objects", str(objects))
    linked = py("check_bun_surface.py", "--objects", str(objects)) if emitted else False
    return ok and emitted and linked


def format_table(rows: Iterable[Outcome]) -> str:
    rows = list(rows)
    headers = ["config", "result", "seconds", "build dir", "log"]
    widths = [len(h) for h in headers]
    data: list[list[str]] = []
    for r in rows:
        status = "SKIPPED" if r.ok is None else ("PASS" if r.ok else "FAIL")
        cells = [r.name, status, f"{r.seconds:.1f}", r.build_dir, r.log_path]
        data.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))
    lines: list[str] = []
    lines.append(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append("-+-".join("-" * w for w in widths))
    for cells in data:
        lines.append(" | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)))
    return "\n".join(lines)


def print_tail(log_path: Path, n: int = 40) -> None:
    if not log_path.is_file():
        return
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-n:]
    print(f"----- last {len(tail)} line(s) of {log_path} -----")
    for line in tail:
        print(line)
    print("-" * 40)


def run_one(
    spec: ConfigSpec, jobs: int, slow: bool, abort: threading.Event, keep_going: bool
) -> Outcome:
    config_dir = OUT_ROOT / spec.name
    log = config_dir / "verify.log"
    # Configs lay out their build dir(s) differently (build/, build-guarded/,
    # release-on/release-off/build-portable/build-arch for diag, ...); the config's
    # own directory is the one path guaranteed to hold all of them.
    build_dir_display = str(config_dir.relative_to(ROOT))

    if not keep_going and abort.is_set():
        return Outcome(
            spec.name,
            spec.job,
            None,
            0.0,
            build_dir_display,
            str(log.relative_to(ROOT)),
            "skipped after an earlier failure",
        )

    reason = spec.needs()
    if reason is not None:
        return Outcome(
            spec.name, spec.job, None, 0.0, build_dir_display, str(log.relative_to(ROOT)), reason
        )

    config_dir.mkdir(parents=True, exist_ok=True)
    log_start(
        log, f"=== {spec.name} ({spec.job}) starting {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
    )
    ctx = RunCtx(name=spec.name, dir=config_dir, log=log, jobs=jobs, slow=slow)
    start = time.monotonic()
    try:
        ok = spec.runner(ctx)
    except Exception as exc:  # a config crashing is a FAIL, not a script crash
        log_write(log, f"\n[verify_local] EXCEPTION: {exc!r}\n")
        ok = False
    elapsed = time.monotonic() - start
    if not ok and not keep_going:
        abort.set()
    return Outcome(spec.name, spec.job, ok, elapsed, build_dir_display, str(log.relative_to(ROOT)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", help=f"comma-separated subset of: {','.join(CONFIG_NAMES)}")
    parser.add_argument("--slow", action="store_true", help="also run the long-tail ctest tier")
    parser.add_argument(
        "--list", action="store_true", help="print the config and bundle tables and exit"
    )
    parser.add_argument(
        "--bundle",
        metavar="NAME",
        help=(
            "cross-build one CI test bundle locally and stop before the execution step "
            f"(#277 phase F); one of: {','.join(BUNDLE_NAMES)}"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="total build/ctest parallelism budget (default: os.cpu_count())",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="don't skip not-yet-started configs after a failure",
    )
    parser.add_argument(
        "--like-ci",
        action="store_true",
        help="mirror c-unit.yml's two stages (#307): build every configuration once, "
        "then replay every bundle in one wave with the run-stage gates after it",
    )
    parser.add_argument(
        "--selftest", action="store_true", help="trivially fast dry-run; no real builds"
    )
    args = parser.parse_args(argv)

    if args.list:
        print(f"{'config':<12} {'mirrors':<45} description")
        for c in CONFIGS:
            print(f"{c.name:<12} {c.job:<45} {c.description}")
        print()
        print("cross bundles (--bundle NAME): built here, executed on the target OS")
        print(f"{'bundle':<28} {'triple':<24} {'workflow':<21} cmake")
        for b in BUNDLES:
            print(f"{b.name:<28} {b.triple:<24} {b.workflow:<21} {b.cmake}")
        return 0

    if args.selftest:
        return do_selftest()

    if args.bundle:
        return run_bundle_build(args.bundle, args.jobs or os.cpu_count() or 4)

    if args.like_ci:
        return run_like_ci(args.jobs or os.cpu_count() or 4)

    if args.only:
        requested = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = [x for x in requested if x not in CONFIG_NAMES]
        if unknown:
            print(
                f"error: unknown config(s) {unknown!r}; choose from {CONFIG_NAMES}", file=sys.stderr
            )
            return 2
        selected = [c for c in CONFIGS if c.name in requested]
    else:
        selected = list(CONFIGS)

    total_jobs = args.jobs or os.cpu_count() or 4
    per_config_jobs = max(2, total_jobs // max(1, len(selected)))

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(
        f"verify_local: {len(selected)} config(s), {total_jobs} total job budget "
        f"({per_config_jobs} per config), ninja={'yes' if have_ninja() else 'no'}, "
        f"ccache={'yes' if have_ccache() else 'no'}"
    )
    if not args.slow:
        print(
            f"verify_local: slow tier excluded by default (-E '{SLOW_TEST_REGEX}'); pass --slow to include it"
        )

    abort = threading.Event()
    outcomes: dict[str, Outcome] = {}
    start_all = time.monotonic()
    max_workers = min(len(selected), max(1, total_jobs))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(run_one, spec, per_config_jobs, args.slow, abort, args.keep_going): spec
            for spec in selected
        }
        for fut in as_completed(futures):
            outcome = fut.result()
            outcomes[outcome.name] = outcome
            status = "SKIPPED" if outcome.ok is None else ("PASS" if outcome.ok else "FAIL")
            suffix = f" ({outcome.reason})" if outcome.reason else ""
            print(f"[{outcome.seconds:7.1f}s] {outcome.name:<12} {status}{suffix}")
            if outcome.ok is False:
                print_tail(ROOT / outcome.log_path)
    wall_clock = time.monotonic() - start_all

    ordered = [outcomes[c.name] for c in selected if c.name in outcomes]
    print()
    print(format_table(ordered))
    total_seconds = sum(o.seconds for o in ordered)
    print(
        f"\nwall clock: {wall_clock:.1f}s   sum of per-config seconds: {total_seconds:.1f}s"
        f"   (speedup: {(total_seconds / wall_clock) if wall_clock else 0:.2f}x)"
    )

    failed = [o.name for o in ordered if o.ok is False]
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    return 0


def do_selftest() -> int:
    """Trivially fast dry-run: validate the config table and tool detection without
    running any real build. Used by ci/tests/test_verify_local.py and by hand as a
    smoke test that the script itself still imports and argparses cleanly.
    """
    ok = True
    if len(CONFIG_NAMES) != len(set(CONFIG_NAMES)):
        print("FAIL: duplicate config names")
        ok = False
    for spec in CONFIGS:
        if not spec.name or not spec.job or not spec.description:
            print(f"FAIL: config {spec.name!r} missing metadata")
            ok = False
    rc = subprocess.run(
        [cmake_bin(), "--version"], capture_output=True, text=True, check=False
    ).returncode
    if rc != 0:
        print("FAIL: cmake not runnable")
        ok = False
    if len(BUNDLE_NAMES) != len(set(BUNDLE_NAMES)):
        print("FAIL: duplicate bundle names")
        ok = False
    for bundle in BUNDLES:
        toolchain = ROOT / "cmake" / "toolchains" / f"soldr-{bundle.triple}.cmake"
        if not toolchain.is_file():
            print(f"FAIL: bundle {bundle.name!r} names a missing toolchain file {toolchain}")
            ok = False
    print(f"selftest: {len(CONFIGS)} configs registered: {', '.join(CONFIG_NAMES)}")
    print(f"selftest: {len(BUNDLES)} cross bundles registered: {', '.join(BUNDLE_NAMES)}")
    print(f"selftest: soldr={'yes' if shutil.which('soldr') else 'no'}")
    print(
        f"selftest: ninja={'yes' if have_ninja() else 'no'} ccache={'yes' if have_ccache() else 'no'} clang={'yes' if _need_clang() is None else 'no'}"
    )
    print("selftest: PASS" if ok else "selftest: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
