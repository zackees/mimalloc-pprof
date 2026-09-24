"""Contract checks for the split PR, ci-test, and full validation lanes."""

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
MANIFEST = json.loads((ROOT / "ci" / "release_full_ci_manifest.v1.json").read_text())
FULL_ROOTS = {
    "c-unit.yml": {
        "build",
        "build-bun-objects",
        "build-windows-native",
        "fastpath-identity",
        "isa-baseline-arm64",
    },
    "cross.yml": {"build-linux", "build-cross", "build-win-gnu"},
    "rust-native.yml": {"test", "test-win-gnu", "test-no-pprof"},
    "windows-bundles.yml": {
        "build-windows-gnu",
        "build-rust-windows-gnu",
        "build-windows-msvc",
        "build-rust-windows-msvc",
    },
}


def workflow(filename: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))


def events(document: dict[Any, Any]) -> dict[Any, Any]:
    # YAML 1.1 treats GitHub Actions' `on` key as a boolean.
    return document["on"] if "on" in document else document[True]


def test_full_workflows_recompute_on_labels_even_for_docs_only_prs() -> None:
    assert set(MANIFEST["workflows"]) == set(FULL_ROOTS) | {"macos-bundles.yml"}
    assert sum(len(spec["jobs"]) for spec in MANIFEST["workflows"].values()) == 65
    for filename in MANIFEST["workflows"]:
        document = workflow(filename)
        pr = events(document)["pull_request"]
        assert "paths" not in pr, filename
        assert {"opened", "synchronize", "reopened", "labeled", "unlabeled"} <= set(pr["types"]), (
            filename
        )
        assert "workflow_dispatch" in events(document), filename


def test_minimal_skips_full_roots_and_ci_test_adds_the_c_dag() -> None:
    for filename, roots in FULL_ROOTS.items():
        jobs = workflow(filename)["jobs"]
        for name in roots:
            gate = jobs[name]["if"]
            assert "github.event_name == 'workflow_dispatch'" in gate, (filename, name)
            assert "contains(github.event.pull_request.labels.*.name, 'ci-full')" in gate, (
                filename,
                name,
            )
            assert "github.event_name == 'push'" not in gate, (filename, name)
            assert ("'ci-test'" in gate) == (filename == "c-unit.yml"), (filename, name)


def test_macos_selective_and_full_lanes_keep_distinct_inputs() -> None:
    jobs = workflow("macos-bundles.yml")["jobs"]
    build = jobs["build-macos"]
    assert "decide" in build["needs"]
    gate = build["env"]["RUN_MACOS_BUILD"]
    assert "needs.decide.outputs.run == 'true'" in gate
    assert "contains(github.event.pull_request.labels.*.name, 'ci-full')" in gate
    assert "'ci-test'" not in gate
    assert "if" not in build  # matrix check names must exist even in minimal mode
    assert all("RUN_MACOS_BUILD" in step.get("if", "") for step in build["steps"])
    assert any("minimal-mode skip" in step.get("name", "") for step in build["steps"])
    assert (
        "contains(github.event.pull_request.labels.*.name, 'ci-full')"
        in jobs["build-rust-apple"]["if"]
    )
    assert "inputs.ci-mode == 'full'" in jobs["run-macos-native-full"]["if"]
    assert "github.event_name == 'push'" not in jobs["run-macos-native-full"]["if"]


def test_ci_test_includes_nonempty_extra_sanitizer_and_fuzz_work() -> None:
    for filename, root in (
        ("asan.yml", "asan"),
        ("fuzz.yml", "fuzz"),
        ("purge-teardown-regression.yml", "build"),
    ):
        document = workflow(filename)
        pr = events(document)["pull_request"]
        assert "paths" not in pr, filename
        assert {"labeled", "unlabeled"} <= set(pr["types"]), filename
        gate = document["jobs"][root]["if"]
        assert "'ci-test'" in gate and "'ci-full'" in gate, filename
        assert "github.event_name == 'push'" not in gate, filename
