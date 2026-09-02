"""Drift guard for ci/verify_local.py against the workflow files it mirrors.

verify_local.py hand-copies cmake flags/env and script invocations out of
`.github/workflows/{c-unit,rust-native,python-lint,asan}.yml` rather than parsing them
at run time (parsing them at run time would make a workflow edit *silently* change
local behavior instead of failing loudly). That copy can go stale, so this test parses
the workflow files itself and asserts every `-D...` cmake flag and `ci/*.py` script
reference used by a Linux-runnable job's `run:` steps is still present, verbatim,
somewhere in verify_local.py's source -- if a workflow job's flags change and nobody
updates the script, this fails.
"""

from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

import verify_local

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

DFLAG_RE = re.compile(r'-D([A-Z_][A-Z0-9_]*)=("\$pprof"|\$pprof|[^\s"\'\)]+)')
SCRIPT_RE = re.compile(r"ci/[A-Za-z0-9_./-]+\.py")


def load_workflow(name: str) -> dict[str, Any]:
    with (WORKFLOWS / name).open(encoding="utf-8") as f:
        return cast(dict[str, Any], yaml.safe_load(f))


def is_linux_job(job: dict[str, Any]) -> bool:
    """True if this job runs (at least partly) on ubuntu-latest -- a bare `runs-on:
    ubuntu-latest`, or a `runs-on: ${{ matrix.os }}` whose matrix includes it. Jobs
    pinned to windows-latest/macos-latest/msys2 or ubuntu-24.04-arm-only are excluded:
    verify_local.py only covers what this Linux dev box can actually run.
    """
    runs_on = job.get("runs-on")
    if runs_on == "ubuntu-latest":
        return True
    if isinstance(runs_on, str) and "matrix.os" in runs_on:
        strategy = job.get("strategy", {})
        matrix = strategy.get("matrix", {}) if isinstance(strategy, dict) else {}
        os_list = matrix.get("os", []) if isinstance(matrix, dict) else []
        return "ubuntu-latest" in os_list
    return False


def job_run_text(job: dict[str, Any]) -> str:
    chunks: list[str] = []
    for step in job.get("steps", []):
        run = step.get("run")
        if isinstance(run, str):
            chunks.append(run)
    return "\n".join(chunks)


def expected_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for name, value in DFLAG_RE.findall(text):
        if value in ('"$pprof"', "$pprof"):
            tokens.add(f"-D{name}=ON")
            tokens.add(f"-D{name}=OFF")
        else:
            tokens.add(f"-D{name}={value.strip(chr(34)).strip(chr(39))}")
    tokens.update(SCRIPT_RE.findall(text))
    return tokens


# Deliberately out of scope for the local mirror (per the work order that specced
# verify_local.py): the "rust" config runs exactly `cargo run -p xtask -- check &&
# cargo test --workspace`, skipping the soldr cache wrapper, `cargo publish --dry-run`,
# and the package-contents check for speed. Listed explicitly, rather than loosened in
# the regex, so a *different* future gap still fails this test.
KNOWN_GAPS: dict[tuple[str, str], set[str]] = {
    ("rust-native.yml", "test"): {"ci/check_crate_package.py"},
}


def linux_job_tokens(workflow_name: str) -> dict[str, set[str]]:
    """job name -> expected tokens, for every Linux-runnable job in `workflow_name`."""
    doc = load_workflow(workflow_name)
    jobs = cast(dict[str, Any], doc.get("jobs", {}))
    out: dict[str, set[str]] = {}
    for job_name, job in jobs.items():
        if not is_linux_job(job):
            continue
        out[job_name] = expected_tokens(job_run_text(job))
    return out


class VerifyLocalDriftTests(unittest.TestCase):
    """Every Linux job's cmake -D flags / ci/*.py references appear in verify_local.py."""

    source: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "ci" / "verify_local.py").read_text(encoding="utf-8")

    def assert_all_present(self, workflow: str) -> None:
        jobs = linux_job_tokens(workflow)
        self.assertTrue(jobs, f"no Linux-runnable jobs found in {workflow} -- parser is broken")
        for job_name, tokens in jobs.items():
            gaps = KNOWN_GAPS.get((workflow, job_name), set())
            for token in tokens - gaps:
                self.assertIn(
                    token,
                    self.source,
                    f"{workflow}:{job_name} uses {token!r}, not found in ci/verify_local.py "
                    "-- update the matching config's runner",
                )

    def test_c_unit_yml(self) -> None:
        self.assert_all_present("c-unit.yml")

    def test_rust_native_yml(self) -> None:
        self.assert_all_present("rust-native.yml")

    def test_python_lint_yml(self) -> None:
        self.assert_all_present("python-lint.yml")

    def test_asan_yml(self) -> None:
        self.assert_all_present("asan.yml")

    def test_config_table_names_are_unique(self) -> None:
        names = verify_local.CONFIG_NAMES
        self.assertEqual(len(names), len(set(names)))

    def test_only_list_from_the_work_order_is_exactly_the_config_table(self) -> None:
        # The --only names verify_local.py was specced with (see its own --list output).
        expected = [
            "release",
            "off",
            "debug-full",
            "guarded",
            "shared",
            "memory-gate",
            "diag",
            "rust",
            "lint",
            "asan",
        ]
        self.assertEqual(verify_local.CONFIG_NAMES, expected)


class SelftestTests(unittest.TestCase):
    def test_selftest_is_fast_and_clean(self) -> None:
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "verify_local.py"), "--selftest"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(
            elapsed, 15, "self-test should be a trivially fast dry-run, not a real build"
        )

    def test_list_exits_zero_without_building(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "verify_local.py"), "--list"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in verify_local.CONFIG_NAMES:
            self.assertIn(name, result.stdout)

    def test_unknown_only_config_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "verify_local.py"), "--only", "not-a-real-config"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
