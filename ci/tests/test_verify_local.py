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

import yaml

import verify_local

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

DFLAG_RE = re.compile(r'-D([A-Z_][A-Z0-9_]*)=("\$pprof"|\$pprof|[^\s"\'\)]+)')
SCRIPT_RE = re.compile(r"ci/[A-Za-z0-9_./-]+\.py")
MATRIX_REF_RE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


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
    """Every `run:` block, PLUS the string values of the job's matrix `include:` rows.

    #307 moved c-unit.yml's cmake flags out of `run:` and into a `build` matrix, one row
    per configuration. Reading only `run:` would have made this drift guard silently
    vacuous for exactly the job it most needs to cover -- the one that decides what gets
    built at all -- so the matrix rows are scanned too.
    """
    chunks: list[str] = []
    for step in job.get("steps", []):
        run = step.get("run")
        if isinstance(run, str):
            chunks.append(run)
    strategy = job.get("strategy", {})
    matrix = strategy.get("matrix", {}) if isinstance(strategy, dict) else {}
    include = matrix.get("include", []) if isinstance(matrix, dict) else []
    if isinstance(include, list):
        for row in include:
            if isinstance(row, dict):
                chunks.extend(str(value) for value in row.values() if isinstance(value, str))
    return "\n".join(chunks)


def matrix_rows(job: dict[str, Any]) -> list[dict[str, Any]]:
    """The concrete matrix rows of `job` -- `include:` entries, else the cartesian product
    of the plain axes. Empty when the job has no matrix.

    Needed because a matrixed job may template its cmake flags (asan.yml does:
    `-DCMAKE_BUILD_TYPE=${{ matrix.build_type }}`). Without expansion the drift guard would
    demand the literal token `-DCMAKE_BUILD_TYPE=${{` appear in verify_local.py, i.e. it
    would either fail on a correct mirror or, if silenced, stop checking that job's flags at
    all -- the exact blind spot issue #301 was hiding in.
    """
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return []
    include = matrix.get("include")
    if isinstance(include, list):
        return [row for row in include if isinstance(row, dict)]
    axes = {k: v for k, v in matrix.items() if k != "exclude" and isinstance(v, list)}
    if not axes:
        return []
    rows: list[dict[str, Any]] = [{}]
    for key, values in axes.items():
        rows = [{**row, key: value} for row in rows for value in values]
    return rows


def expand_matrix(text: str, rows: list[dict[str, Any]]) -> list[str]:
    """`text` once per matrix row, with `${{ matrix.<key> }}` substituted. A reference the
    row does not define is left alone, so it surfaces as an obviously-wrong token rather
    than being silently dropped."""
    if not rows:
        return [text]

    def substitute(row: dict[str, Any]) -> str:
        def one(m: re.Match[str]) -> str:
            return str(row[m.group(1)]) if m.group(1) in row else m.group(0)

        return MATRIX_REF_RE.sub(one, text)

    return [substitute(row) for row in rows]


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
#
# c-unit.yml's two musl rows (#273 8a, now rows of the `build` matrix -- #307) build and
# run inside `container: alpine:3.20` -- a different execution model than every other
# config here, which builds directly on this host's own toolchain. verify_local.py has no
# container runner, so those two flags are out of scope for the same reason the "rust" gap
# above is: listed explicitly rather than making the regex or `is_linux_job` blind to
# `container:` in general, so an actually-in-scope row that starts using
# `-DCMAKE_C_FLAGS=...` still fails this test. Covered instead by manual
# `docker run alpine:3.20 ...` verification (docs/ci-gates.md spells out the command) and
# by CI itself.
KNOWN_GAPS: dict[tuple[str, str], set[str]] = {
    ("rust-native.yml", "test"): {"ci/check_crate_package.py"},
    ("c-unit.yml", "build"): {
        "-DMI_LIBC_MUSL=ON",
        "-DCMAKE_C_FLAGS=-ftls-model=local-dynamic",
    },
}


def linux_job_tokens(workflow_name: str) -> dict[str, set[str]]:
    """job name -> expected tokens, for every Linux-runnable job in `workflow_name`."""
    doc = load_workflow(workflow_name)
    jobs = cast(dict[str, Any], doc.get("jobs", {}))
    out: dict[str, set[str]] = {}
    for job_name, job in jobs.items():
        if not is_linux_job(job):
            continue
        tokens: set[str] = set()
        for text in expand_matrix(job_run_text(job), matrix_rows(job)):
            tokens |= expected_tokens(text)
        out[job_name] = tokens
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

    def test_asan_yml_matrix_expands_to_both_build_types(self) -> None:
        """#301: the release-type ASan row is the one that can reach `mi_free_small`'s
        page-from-alignment fast path (CMakeLists only auto-enables MI_OPT_FREE_SMALL when
        MI_DEBUG is off), so losing it would silently un-cover the bug. Assert it is both in
        the workflow and mirrored locally, not just that the matrix parses."""
        tokens = linux_job_tokens("asan.yml")["asan"]
        self.assertIn("-DCMAKE_BUILD_TYPE=Debug", tokens)
        self.assertIn("-DCMAKE_BUILD_TYPE=RelWithDebInfo", tokens)
        self.assertIn("-DMI_TRACK_ASAN=ON", tokens)
        self.assertNotIn("-DCMAKE_BUILD_TYPE=${{", tokens)

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
            "bundle",
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


# --------------------------------------------------------------------------------------
# #277 phase F: the --bundle name table vs. the bundle workflows' build matrices.
# --------------------------------------------------------------------------------------

BUNDLE_WORKFLOW_JOBS: list[tuple[str, str]] = [
    ("macos-bundles.yml", "build-macos"),
    ("windows-bundles.yml", "build-windows-gnu"),
    ("windows-bundles.yml", "build-windows-msvc"),
]

TOOLCHAIN_RE = re.compile(r"cmake/toolchains/soldr-([A-Za-z0-9_.-]+)\.cmake")


def matrix_bundles(workflow_name: str, job_name: str) -> list[dict[str, Any]]:
    """The `include:` rows of one bundle-building job's matrix, plus the triple.

    The macOS matrix carries `triple:` per row; the two Windows jobs do not (they are
    single-target jobs, so the triple is spelled once, in the toolchain path their steps
    pass to cmake). Reading it out of the run text rather than hardcoding it here means a
    job that is repointed at a different toolchain fails this test.
    """
    doc = load_workflow(workflow_name)
    job = cast(dict[str, Any], doc["jobs"][job_name])
    rows = cast(list[dict[str, Any]], job["strategy"]["matrix"]["include"])
    text = job_run_text(job)
    job_triples = {t for t in TOOLCHAIN_RE.findall(text) if "${{" not in t}
    out: list[dict[str, Any]] = []
    for row in rows:
        triple = row.get("triple")
        if triple is None:
            assert len(job_triples) == 1, (
                f"{workflow_name}:{job_name} has no matrix `triple:` and its steps name "
                f"{sorted(job_triples)} toolchains -- cannot infer one target"
            )
            triple = next(iter(job_triples))
        out.append(
            {
                "bundle": row["bundle"],
                "triple": triple,
                "cmake": row["cmake"],
                "workflow": workflow_name,
                "job": job_name,
                "run_text": text,
            }
        )
    return out


def all_matrix_bundles() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for workflow_name, job_name in BUNDLE_WORKFLOW_JOBS:
        rows.extend(matrix_bundles(workflow_name, job_name))
    return rows


class BundleTableDriftTests(unittest.TestCase):
    """`verify_local.py --bundle NAME` must offer exactly the CI matrices' bundles.

    Same contract as the config table above and for the same reason: the flags are copied
    by hand so that a workflow edit fails loudly here instead of silently changing what
    `--bundle` builds. A name that exists in CI but not here is a lane a developer cannot
    reproduce locally; a name that exists here but not in CI builds something CI does not.
    """

    def test_names_match_the_workflow_matrices_exactly(self) -> None:
        expected = [row["bundle"] for row in all_matrix_bundles()]
        self.assertEqual(sorted(verify_local.BUNDLE_NAMES), sorted(expected))
        self.assertEqual(len(expected), len(set(expected)))

    def test_triple_and_cmake_flags_match_verbatim(self) -> None:
        by_name = {b.name: b for b in verify_local.BUNDLES}
        for row in all_matrix_bundles():
            spec = by_name[row["bundle"]]
            self.assertEqual(
                spec.triple,
                row["triple"],
                f"{row['workflow']}:{row['job']} builds {row['bundle']} for "
                f"{row['triple']}, verify_local.py says {spec.triple}",
            )
            self.assertEqual(
                spec.cmake,
                row["cmake"],
                f"{row['workflow']}:{row['job']}'s {row['bundle']} cmake flags drifted "
                "from ci/verify_local.py",
            )
            self.assertEqual(spec.workflow, row["workflow"])
            self.assertEqual(spec.job, row["job"])

    def test_bundle_tests_arguments_match_the_workflow_step(self) -> None:
        # The lane-specific ci/bundle_tests.py flags matter as much as the cmake ones:
        # without --dll-search-dir the win-gnu bundle silently omits libgcc_s_seh-1.dll.
        for row in all_matrix_bundles():
            spec = next(b for b in verify_local.BUNDLES if b.name == row["bundle"])
            for arg in spec.bundle_args:
                self.assertIn(
                    arg,
                    row["run_text"],
                    f"ci/verify_local.py passes {arg!r} to bundle_tests.py for "
                    f"{row['bundle']}, but {row['workflow']}:{row['job']} does not",
                )

    def test_every_bundle_names_a_toolchain_file_that_exists(self) -> None:
        for spec in verify_local.BUNDLES:
            toolchain = ROOT / "cmake" / "toolchains" / f"soldr-{spec.triple}.cmake"
            self.assertTrue(toolchain.is_file(), f"{spec.name} names a missing {toolchain}")

    def test_list_prints_the_bundle_table(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "verify_local.py"), "--list"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in verify_local.BUNDLE_NAMES:
            self.assertIn(name, result.stdout)

    def test_unknown_bundle_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "verify_local.py"), "--bundle", "not-a-bundle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown bundle", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
