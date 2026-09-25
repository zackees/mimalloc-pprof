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


def dashboard_link(name: str) -> str:
    if name in report.DISTRIBUTION_PANELS.values():
        return "https://zackees.github.io/mimalloc-pprof/#requested-size-distributions"
    if name == report.CHURN_PANEL:
        return "https://zackees.github.io/mimalloc-pprof/#thread-churn"
    if name == report.LARSON_RSS_PANEL:
        return "https://zackees.github.io/mimalloc-pprof/#scaling"
    return ""


def readme_image(name: str) -> str:
    """One README image line; a clickable one wraps the image in a link, as README.md does."""
    image = f"![{name}]({evaluate.RAW_BASE}{name})"
    destination = dashboard_link(name)
    return f"[{image}]({destination})" if destination else image


def complete_readme(tmp_path: Path, *, skip: str | None = None) -> Path:
    readme = tmp_path / "README.md"
    lines = [readme_image(name) for name in sorted(evaluate.EXPECTED_SVGS) if name != skip]
    (tmp_path / "local.svg").write_text("<svg/>", encoding="utf-8")
    readme.write_text("\n".join([*lines, "![local](local.svg)"]) + "\n", encoding="utf-8")
    return readme


def test_readme_accepts_complete_image_inventory(tmp_path: Path) -> None:
    assert report.CHURN_PANEL in evaluate.EXPECTED_SVGS
    assert evaluate.readme_errors(complete_readme(tmp_path)) == []


def test_readme_rejects_missing_churn_chart(tmp_path: Path) -> None:
    errors = evaluate.readme_errors(complete_readme(tmp_path, skip=report.CHURN_PANEL))
    assert any(report.CHURN_PANEL in error for error in errors)
    assert any("thread-churn graphic" in error for error in errors)


def test_readme_accepts_and_requires_the_larson_rss_chart(tmp_path: Path) -> None:
    # #506: the larson peak-RSS overlay is part of the published inventory.
    assert report.LARSON_RSS_PANEL in evaluate.EXPECTED_SVGS
    assert evaluate.readme_errors(complete_readme(tmp_path)) == []
    errors = evaluate.readme_errors(complete_readme(tmp_path, skip=report.LARSON_RSS_PANEL))
    assert any(report.LARSON_RSS_PANEL in error for error in errors)
    assert any("larson peak-RSS graphic" in error for error in errors)


def test_readme_rejects_a_larson_rss_chart_that_skips_the_scaling_section(
    tmp_path: Path,
) -> None:
    readme = complete_readme(tmp_path)
    source = readme.read_text(encoding="utf-8")
    linked = readme_image(report.LARSON_RSS_PANEL)
    assert linked in source
    unlinked = f"![{report.LARSON_RSS_PANEL}]({evaluate.RAW_BASE}{report.LARSON_RSS_PANEL})"
    readme.write_text(source.replace(linked, unlinked), encoding="utf-8")
    errors = evaluate.readme_errors(readme)
    assert errors == ["README larson peak-RSS graphic must target the dashboard scaling section"]


def full_lineage(*, churn: bool) -> dict[str, object]:
    scaling: dict[str, object] = {
        "metric_schema_version": report.SCALING_SCHEMA,
        "patterns": [{"pattern": pattern} for pattern in report.SCALING_PATTERN_IDS],
        "thread_points": list(report.SCALING_THREAD_POINTS),
        "run": {"source_ref": "refs/heads/main"},
    }
    if churn:
        scaling["churn"] = {"metric_schema_version": report.SCALING_CHURN_SCHEMA}
    return scaling


def full_dashboard(*, churn_anchor: bool) -> str:
    return (
        '<h2 id="requested-size-distributions"></h2>'
        + ('<h2 id="thread-churn"></h2>' if churn_anchor else "")
        + "".join(f'<img src="{name}">' for name in sorted(evaluate.EXPECTED_SVGS))
    )


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


def test_publication_calls_strict_validator_for_full_lineage(tmp_path: Path) -> None:
    scaling = full_lineage(churn=True)
    (tmp_path / "latest.json").write_text(json.dumps({"scaling": scaling}), encoding="utf-8")
    (tmp_path / "index.html").write_text(full_dashboard(churn_anchor=True), encoding="utf-8")
    with (
        patch.object(report, "validate_site"),
        patch.object(report, "validate_scaling_report") as strict,
    ):
        assert evaluate.publication_errors(tmp_path) == []
    strict.assert_called_once_with(scaling, "latest.scaling")


def test_publication_requires_churn_side_car(tmp_path: Path) -> None:
    (tmp_path / "latest.json").write_text(
        json.dumps({"scaling": full_lineage(churn=False)}), encoding="utf-8"
    )
    (tmp_path / "index.html").write_text(full_dashboard(churn_anchor=True), encoding="utf-8")
    with patch.object(report, "validate_site"), patch.object(report, "validate_scaling_report"):
        errors = evaluate.publication_errors(tmp_path)
    assert "latest.json scaling report has no thread-churn side-car" in errors

    (tmp_path / "latest.json").write_text(
        json.dumps({"scaling": full_lineage(churn=True)}), encoding="utf-8"
    )
    (tmp_path / "index.html").write_text(full_dashboard(churn_anchor=False), encoding="utf-8")
    with patch.object(report, "validate_site"), patch.object(report, "validate_scaling_report"):
        errors = evaluate.publication_errors(tmp_path)
    assert errors == ["dashboard is missing the thread-churn anchor"]


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
