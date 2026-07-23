#!/usr/bin/env python3
"""Fast, volume-backed Linux build/test loop for Docker Desktop developers."""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

if os.name != "nt":
    import signal

ROOT = Path(__file__).resolve().parents[1]
DOCKER_BUILD = ["clud", "tool", "run", "docker/docker-build.py", "soldr", str(ROOT)]
C_TEST = (
    "cmake -S /src -B /target/c-build -G Ninja -DMI_PPROF=ON "
    "-DCMAKE_C_COMPILER_LAUNCHER=zccache && cmake --build /target/c-build && "
    "ctest --test-dir /target/c-build --output-on-failure -E 'test-stress.*' && "
    "threads=$(nproc); [ \"$threads\" -le 4 ] || threads=4; "
    "/target/c-build/mimalloc-test-stress \"$threads\" 50 50 && "
    "/target/c-build/mimalloc-test-stress-dynamic \"$threads\" 50 50"
)


def soldr_timeout_diagnostics() -> None:
    """Print enough state to investigate a fail-fast development-loop timeout."""
    print("soldr command exceeded 60 seconds; collecting Docker diagnostics:", file=sys.stderr)
    for command in (
        ["docker", "ps", "--filter", "name=clud-docker-build-soldr", "--format", "table {{.Names}}\\t{{.Status}}"],
        ["docker", "stats", "--no-stream"],
        ["docker", "ps", "-a", "--filter", "name=clud-docker-build-soldr"],
    ):
        try:
            subprocess.run(command, cwd=ROOT, check=False, timeout=10)
        except subprocess.TimeoutExpired:
            print(f"diagnostic also timed out: {' '.join(command)}", file=sys.stderr)


def run_tool(args: list[str], *, capture: bool = False,
             timeout_seconds: int | None = None) -> str:
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    popen_args: dict[str, object] = {}
    if os.name == "nt":
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_args["start_new_session"] = True
    process = subprocess.Popen(
        DOCKER_BUILD + args, cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        **popen_args,
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        # `subprocess.run(..., timeout=...)` kills only its direct child on
        # Windows.  `clud tool run` has a Python and Docker child tree; leave
        # none of it behind or its delayed container start races the next run.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           check=False, capture_output=True)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        if output:
            print(output, end="", file=sys.stderr)
        soldr_timeout_diagnostics()
        raise SystemExit("soldr development command timed out after 60 seconds") from error
    if process.returncode:
        if capture and output:
            print(output, end="", file=sys.stderr)
        raise subprocess.CalledProcessError(process.returncode, process.args)
    return output or ""


def c_test(extra: list[str], *, capture: bool = False) -> str:
    command = C_TEST
    if extra:
        command += " " + " ".join(extra)
    # `clud tool run` consumes a standalone `--`; docker-build's `run`
    # parser accepts the command directly as its remainder instead.
    return run_tool(["run", "bash", "-lc", command], capture=capture,
                    timeout_seconds=60)


def rust_test(extra: list[str]) -> None:
    if not (ROOT / "rust" / "Cargo.toml").is_file():
        print("rust workspace not present yet (see #4)")
        return
    command = "cd /src/rust && soldr cargo test"
    if extra:
        command += " " + " ".join(extra)
    run_tool(["run", "bash", "-lc", command], timeout_seconds=60)


def timed(label: str, action):
    start = time.monotonic()
    output = action()
    return label, time.monotonic() - start, output


def container_id() -> str:
    return subprocess.check_output(
        ["docker", "ps", "-q", "-f", "name=^clud-docker-build-soldr-" + project_key() + "$"],
        cwd=ROOT, text=True).strip()


def marker_toggle() -> None:
    alloc = ROOT / "src" / "alloc.c"
    marker = "// dev-loop-bench-marker\n"
    text = alloc.read_text(encoding="utf-8")
    alloc.write_text(text[:-len(marker)] if text.endswith(marker) else text + marker,
                     encoding="utf-8")


def bench(reuse: bool = False) -> None:
    results: list[tuple[str, float, str]] = []
    if not reuse:
        run_tool(["clean"])
    run_tool(["up"])
    initial_container = container_id()
    initial_label = "reused C build + ctest" if reuse else "cold C build + ctest"
    results.append(timed(initial_label, lambda: c_test([], capture=True)))
    for i in range(1, 4):
        results.append(timed(f"warm no-op {i}", lambda: c_test([], capture=True)))
    marker_toggle()
    results.append(timed("single edit", lambda: c_test([], capture=True)))
    if (ROOT / "rust" / "Cargo.toml").is_file():
        rust_test([])
        results.append(timed("rust warm no-op", lambda: run_tool(
            ["run", "bash", "-lc", "cd /src/rust && soldr cargo test"], capture=True,
            timeout_seconds=60)))

    warm = [seconds for name, seconds, _ in results if name.startswith("warm no-op")]
    checks = {
        "warm median <= 60 s": statistics.median(warm) <= 60,
        "single edit <= 60 s": next(seconds for name, seconds, _ in results if name == "single edit") <= 60,
        "ninja reported no work": all("no work to do" in output.lower() for name, _, output in results if name.startswith("warm no-op")),
        "container reused across warm runs": bool(initial_container) and container_id() == initial_container,
        "warm runs did not rebuild the image": all(
            "step 1/" not in output.lower() and "writing image" not in output.lower()
            for name, _, output in results if name.startswith("warm no-op")),
        "compiler launcher is zccache": "zccache" in run_tool(["run", "bash", "-lc", "grep CMAKE_C_COMPILER_LAUNCHER /target/c-build/CMakeCache.txt"], capture=True),
        "source bind is read-only": subprocess.run(["docker", "exec", "clud-docker-build-soldr-" + project_key(), "touch", "/src/.probe"], cwd=ROOT).returncode != 0,
    }
    fs = run_tool(["run", "bash", "-lc", "df -T /target | tail -1"], capture=True).lower()
    checks["target is not a host filesystem"] = not any(kind in fs for kind in ("9p", "fuse", "virtiofs"))
    print("| phase | seconds |\n| --- | ---: |")
    for name, seconds, _ in results:
        print(f"| {name} | {seconds:.2f} |")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    run_tool(["run", "bash", "-lc", "zccache --help 2>&1 | head -20"], capture=False)
    if not all(checks.values()):
        raise SystemExit("dev loop benchmark failed")


def project_key() -> str:
    import hashlib
    return hashlib.blake2b(str(ROOT.resolve()).encode(), digest_size=6).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("c-test", "rust-test"):
        p = sub.add_parser(name)
        p.add_argument("args", nargs=argparse.REMAINDER)
    bench_parser = sub.add_parser("bench")
    bench_parser.add_argument("--reuse", action="store_true",
                              help="keep the current named volumes (restart validation)")
    for name in ("shell", "doctor", "clean"):
        sub.add_parser(name)
    args = parser.parse_args()
    if args.command == "c-test": c_test(args.args)
    elif args.command == "rust-test": rust_test(args.args)
    elif args.command == "bench": bench(args.reuse)
    else: run_tool([args.command])


if __name__ == "__main__":
    main()
