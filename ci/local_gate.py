#!/usr/bin/env python3
"""mimalloc-pprof's local gate (zackees/ci.yml#166 GATE-001..004, #190 GATE-008, #198 GATE-010).

One command that is the remote `python-lint.yml` `lint` job, so a PR passes it on its first push:

    uv run --no-project --python 3.13 python ci/local_gate.py              # every lane
    uv run --no-project --python 3.13 python ci/local_gate.py --lane lint  # = python-lint.yml:lint
    uv run --no-project --python 3.13 python ci/local_gate.py --list

Do not call this directly before pushing; call it through the attesting wrapper, which runs it on
a clean tree and stamps HEAD with a tree-bound `Local-Gate:` trailer plus one `Ci-Attestation:`
trailer per gate in ci-attestations.yml (see local-gate.toml):

    uvx --from git+https://github.com/zackees/ci.yml@<CI_LINT_REF> ci-lint local-gate run

The remote `lint` job runs exactly `ci/local_gate.py --lane lint` and nothing else (GATE-001), so
CHECKS below is the single source of truth for it, and ci/verify_local.py's `lint` config calls
this script too. On an attested, in-policy PR head, python-lint.yml's `ci-mode` job skips the
remote `lint` job (GATE-008/010); pushes to main always run it.

Every tool runs from one pinned `uv run --with` environment (TOOLS), never the ambient PATH.
`check_usdt_probes.py --require` needs <sys/sdt.h> (Debian/Ubuntu: systemtap-sdt-dev). On a host
without it in the default include path, point CPATH at it, e.g. on NixOS:
`CPATH=$(nix build --no-link --print-out-paths nixpkgs#libsystemtap)/include`.

Checks run in parallel, output goes to files (never a pipe), and a failing check's output is
printed after the timing table. Exit 1 when any check fails.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The zackees/ci.yml commit whose ci_lint this repository uses; python-lint.yml's `ci-mode` job
# checks out the same SHA (ci/tests/test_local_gate.py keeps the two in step).
CI_LINT_REF = "0d0c5545b0f574f31c70fae2f961863984a13eae"
LANES = ("lint",)
# One pinned tool environment: the versions python-lint.yml used to `pip install`.
TOOLS = (
    "uv",
    "run",
    "--no-project",
    "--python",
    "3.13",
    "--with",
    "ruff==0.12.10",
    "--with",
    "pyright==1.1.411",
    "--with",
    "pyyaml==6.0.2",
    "--with",
    "pytest==8.3.4",
    "--with",
    "pytest-xdist==3.6.1",
)
PY = (*TOOLS, "python")


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    # Text the combined output must contain (for scripts whose exit code is not the verdict).
    expect: str = ""
    # Started first, so the long check overlaps the short ones.
    slow: bool = False


@dataclass(frozen=True)
class Outcome:
    check: Check
    ok: bool
    secs: float
    log: Path


def script(name: str, *args: str) -> tuple[str, ...]:
    return (*PY, f"ci/{name}", *args)


def selftest(name: str) -> Check:
    return Check(f"{name} --selftest", script(name, "--selftest"))


CHECKS: tuple[Check, ...] = (
    # pytest-xdist: serial, the suite was ~2m50s of the old job's ~3m30s (zackees/mimalloc-pprof#599).
    Check(
        "pytest ci/tests",
        (*PY, "-m", "pytest", "ci/tests", "-q", "-n", "auto", "--dist", "worksteal"),
        slow=True,
    ),
    Check("pyright", (*TOOLS, "pyright"), slow=True),
    Check("ruff check", (*TOOLS, "ruff", "check", "ci/")),
    Check("ruff format", (*TOOLS, "ruff", "format", "--check", "ci/")),
    selftest("check_scaling_typed_renderer.py"),
    Check("check_scaling_typed_renderer.py", script("check_scaling_typed_renderer.py")),
    # The linters must not have changed behavior: parser fixtures for both architectures.
    selftest("check_isa_baseline.py"),
    selftest("check_internal_state.py"),
    selftest("check_rust_surface.py"),
    Check("check_rust_surface.py", script("check_rust_surface.py")),
    # A gate script that imports cleanly but crashes on argv is still broken.
    Check("memory_gate.py usage", script("memory_gate.py"), expect="Exit codes"),
    Check("check_isa_baseline.py --help", script("check_isa_baseline.py", "--help")),
    Check("check_release_equivalence.py --help", script("check_release_equivalence.py", "--help")),
    # Benchmark publication policy checkers.
    selftest("check_benchmark_workflow.py"),
    selftest("check_benchmark_memory_workflow.py"),
    selftest("check_benchmark_latency_workflow.py"),
    selftest("check_benchmark_scaling_workflow.py"),
    selftest("check_large_span_diagnostic_workflow.py"),
    selftest("check_benchmark_pprof_tax_workflow.py"),
    selftest("build_pprof_tax_configurations.py"),
    selftest("check_scaling_parity.py"),
    # #277 phase B2: no workflow may schedule onto a native macOS runner.
    Check("lint_no_macos_runners.py", script("lint_no_macos_runners.py")),
    selftest("check_no_diagnostic_suppression.py"),
    Check("check_no_diagnostic_suppression.py", script("check_no_diagnostic_suppression.py")),
    # #573: page geometry has one writer; the size-class-edge structs stay in budget.
    selftest("check_page_geometry_writes.py"),
    Check("check_page_geometry_writes.py", script("check_page_geometry_writes.py")),
    selftest("check_struct_sizes.py"),
    Check("check_struct_sizes.py", script("check_struct_sizes.py")),
    # #573 A5: the USDT probes are emitted when asked for, and only then (needs <sys/sdt.h>).
    selftest("check_usdt_probes.py"),
    Check("check_usdt_probes.py --require", script("check_usdt_probes.py", "--require")),
    selftest("scaling_failure_alert.py"),
    selftest("check_macro_case.py"),
    Check("check_macro_case.py", script("check_macro_case.py")),
    # #491: the release-time bound may only go down (base fetched first, see main()).
    selftest("check_release_ratchet.py"),
    Check(
        "check_release_ratchet.py --base",
        script("check_release_ratchet.py", "--base", "FETCH_HEAD"),
    ),
    selftest("check_macos_labels.py"),
    selftest("macos_lane_decide.py"),
    selftest("memory_gate_lane_decide.py"),
    # The committed chart SVGs and the README feature table re-render from committed data.
    Check("bench_hole_purging.py --check", script("bench_hole_purging.py", "--check")),
    Check(
        "bench_hole_purging.py --check --table",
        script("bench_hole_purging.py", "--check", "--table"),
    ),
    Check(
        "bench_hole_purging_allocators.py --check",
        script("bench_hole_purging_allocators.py", "--check"),
    ),
    Check(
        "bench_hole_purging_allocators.py --check --table",
        script("bench_hole_purging_allocators.py", "--check", "--table"),
    ),
    Check(
        "bench_hole_purging_allocators.py --check --busy-threads",
        script("bench_hole_purging_allocators.py", "--check", "--busy-threads"),
    ),
    Check(
        "bench_arena_reclaim.py --check",
        script(
            "bench_arena_reclaim.py",
            "--check",
            "--data",
            ".github/assets/arena-reclaim-linux-pr.json",
        ),
    ),
    Check("render_feature_table.py --check", script("render_feature_table.py", "--check")),
)


def run_check(check: Check, logs: Path, index: int) -> Outcome:
    log = logs / f"{index:02d}.log"
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as out:
        rc = subprocess.call(check.argv, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
    secs = time.monotonic() - start
    ok = rc == 0
    if check.expect:
        ok = check.expect in log.read_text(encoding="utf-8", errors="replace")
    return Outcome(check, ok, secs, log)


def fetch_base() -> int:
    """Fetch the ratchet's base into FETCH_HEAD: the PR base in CI, else main."""
    base = os.environ.get("GITHUB_BASE_REF") or "main"
    return subprocess.call(["git", "fetch", "--quiet", "--depth=1", "origin", base], cwd=ROOT)


def warm_tools() -> int:
    """Resolve the pinned environment once, so the parallel checks do not race to build it."""
    return subprocess.call([*PY, "-c", "import pytest, yaml, xdist"], cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="mimalloc-pprof local gate")
    parser.add_argument("--lane", choices=LANES, help="run one lane (default: every lane)")
    parser.add_argument("--list", action="store_true", help="list the checks and exit")
    args = parser.parse_args()
    if args.list:
        for check in CHECKS:
            print(f"lint: {check.name}")
        return 0
    if fetch_base() != 0:
        print("local_gate: git fetch of the ratchet base failed", file=sys.stderr)
        return 1
    if warm_tools() != 0:
        print("local_gate: could not build the pinned tool environment", file=sys.stderr)
        return 1
    ordered = sorted(enumerate(CHECKS), key=lambda item: not item[1].slow)
    outcomes: list[Outcome] = []
    with tempfile.TemporaryDirectory(prefix="local-gate-") as tmp:
        logs = Path(tmp)
        with ThreadPoolExecutor(max_workers=max(4, (os.cpu_count() or 4) // 2)) as pool:
            futures = [pool.submit(run_check, check, logs, index) for index, check in ordered]
            for future in as_completed(futures):
                outcome = future.result()
                outcomes.append(outcome)
                print(
                    f"{'ok  ' if outcome.ok else 'FAIL'} {outcome.secs:6.1f}s  {outcome.check.name}",
                    flush=True,
                )
        failed = [o for o in outcomes if not o.ok]
        for outcome in failed:
            print(f"\n===== FAILED: {outcome.check.name}\n$ {' '.join(outcome.check.argv)}")
            print(outcome.log.read_text(encoding="utf-8", errors="replace"))
    print(f"\nlocal gate: {len(outcomes) - len(failed)}/{len(outcomes)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
