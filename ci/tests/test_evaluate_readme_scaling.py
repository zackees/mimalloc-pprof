"""RED/GREEN controls for the README scaling-publication evaluation."""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark_report as report
import evaluate_readme_scaling as evaluate


def test_readme_rejects_missing_image_and_local_target(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[missing](docs/nope.md)\n", encoding="utf-8")
    errors = evaluate.readme_errors(readme)
    assert any("image inventory" in error for error in errors)
    assert any("missing local target docs/nope.md" in error for error in errors)


def test_readme_accepts_complete_image_inventory(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    lines = [
        f"![{name}](https://raw.githubusercontent.com/zackees/mimalloc-pprof/"
        f"benchmark-stats/{name})"
        + (
            "(https://zackees.github.io/mimalloc-pprof/#requested-size-distributions)"
            if name in report.DISTRIBUTION_PANELS.values()
            else "(https://zackees.github.io/mimalloc-pprof/#thread-churn)"
            if name == report.THREAD_CHURN_PANEL
            else ""
        )
        for name in sorted(evaluate.EXPECTED_SVGS)
    ]
    (tmp_path / "local.svg").write_text("<svg/>", encoding="utf-8")
    readme.write_text("\n".join([*lines, "![local](local.svg)"]) + "\n", encoding="utf-8")
    assert evaluate.readme_errors(readme) == []


def test_publication_rejects_old_lineage_and_missing_dashboard(tmp_path: Path) -> None:
    (tmp_path / "latest.json").write_text(
        json.dumps(
            {
                "scaling": {
                    "metric_schema_version": report.LEGACY_SCALING_SCHEMA,
                    "patterns": [
                        {"pattern": pattern} for pattern in report.LEGACY_SCALING_PATTERN_IDS
                    ],
                    "thread_points": list(report.SCALING_THREAD_POINTS),
                    "run": {"source_ref": "refs/heads/main"},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text('<h2 id="scaling">Old</h2>', encoding="utf-8")
    with patch.object(report, "validate_site"):
        errors = evaluate.publication_errors(tmp_path)
    assert any("current distribution schema" in error for error in errors)
    assert any("power-of-two-large" in error for error in errors)
    assert any("requested-size-distributions anchor" in error for error in errors)
    assert any("thread-churn side-car" in error for error in errors)
    assert any("thread-churn anchor" in error for error in errors)


def test_readme_requires_the_thread_churn_chart(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    lines = [
        f"![{name}](https://raw.githubusercontent.com/zackees/mimalloc-pprof/"
        f"benchmark-stats/{name})"
        + (
            "(https://zackees.github.io/mimalloc-pprof/#requested-size-distributions)"
            if name in report.DISTRIBUTION_PANELS.values()
            else ""
        )
        for name in sorted(evaluate.EXPECTED_SVGS - {report.THREAD_CHURN_PANEL})
    ]
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
    errors = evaluate.readme_errors(readme)
    assert any(report.THREAD_CHURN_PANEL in error for error in errors)
    assert any("#thread-churn" in error for error in errors)


def test_publication_calls_strict_validator_for_full_lineage(tmp_path: Path) -> None:
    scaling = {
        "metric_schema_version": report.SCALING_SCHEMA,
        "patterns": [{"pattern": pattern} for pattern in report.SCALING_PATTERN_IDS],
        "thread_points": list(report.SCALING_THREAD_POINTS),
        "run": {"source_ref": "refs/heads/main"},
        "thread_churn": {},
    }
    (tmp_path / "latest.json").write_text(json.dumps({"scaling": scaling}), encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<h2 id="requested-size-distributions"></h2><h2 id="thread-churn"></h2>'
        + "".join(f'<img src="{name}">' for name in sorted(evaluate.EXPECTED_SVGS)),
        encoding="utf-8",
    )
    with (
        patch.object(report, "validate_site"),
        patch.object(report, "validate_scaling_report") as strict,
    ):
        assert evaluate.publication_errors(tmp_path) == []
    strict.assert_called_once_with(scaling, "latest.scaling")


def test_raw_url_audit_rejects_a_broken_or_stale_image(tmp_path: Path) -> None:
    for name in evaluate.EXPECTED_SVGS:
        (tmp_path / name).write_bytes(b"<svg/>")

    payload = b"<svg/>"

    def served(_url: str, *, timeout: int) -> BytesIO:
        assert timeout > 0
        return BytesIO(payload)

    with patch.object(evaluate.urllib.request, "urlopen", side_effect=served):
        assert evaluate.raw_url_errors(tmp_path) == []
    payload = b"stale"
    with patch.object(evaluate.urllib.request, "urlopen", side_effect=served):
        errors = evaluate.raw_url_errors(tmp_path)
    assert len(errors) == len(evaluate.EXPECTED_SVGS)
    assert all("served bytes differ" in error for error in errors)
