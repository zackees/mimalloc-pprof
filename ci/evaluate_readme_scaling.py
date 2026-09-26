#!/usr/bin/env python3
"""Evaluate whether the README's full scaling publication is actually live.

Usage:
  python3 ci/evaluate_readme_scaling.py --site-dir /path/to/benchmark-stats
  python3 ci/evaluate_readme_scaling.py --site-dir /path/to/benchmark-stats \
      --pages-url https://zackees.github.io/mimalloc-pprof/

The site directory must contain the exact files from the benchmark-stats branch,
not a smoke artifact or a partially rendered site. The optional Pages audit
compares every deployed file byte-for-byte with that sealed branch.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

import benchmark_report as report

ROOT = Path(__file__).resolve().parents[1]
RAW_SVG = re.compile(
    r"https://raw\.githubusercontent\.com/zackees/mimalloc-pprof/"
    r"benchmark-stats/([A-Za-z0-9._/-]+\.svg)"
)
MARKDOWN_LINK = re.compile(r"\]\(([^)]+)\)")
HTML_SOURCE = re.compile(r'(?:src|srcset)="([^"]+)"')
EXPECTED_SVGS = frozenset(
    (
        *report.SCALING_PANELS.values(),
        *report.DISTRIBUTION_PANELS.values(),
        report.THREAD_CHURN_PANEL,
    )
)
THREAD_CHURN_ANCHOR = "#thread-churn"
RAW_BASE = "https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/"


def readme_errors(readme: Path) -> list[str]:
    source = readme.read_text(encoding="utf-8")
    errors: list[str] = []
    linked = Counter(RAW_SVG.findall(source))
    missing = EXPECTED_SVGS - linked.keys()
    unexpected = linked.keys() - EXPECTED_SVGS
    if missing or unexpected:
        errors.append(
            f"README scaling image inventory: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )
    repeated = sorted(name for name, count in linked.items() if count != 1)
    if repeated:
        errors.append(f"README scaling images linked more than once: {repeated}")
    if source.count("#requested-size-distributions") != len(report.DISTRIBUTION_PANELS):
        errors.append("README distribution graphics must target the dashboard section")
    if source.count(THREAD_CHURN_ANCHOR) != 1:
        errors.append(
            "README thread-churn graphic must target the dashboard's #thread-churn section"
        )

    for match in (*MARKDOWN_LINK.finditer(source), *HTML_SOURCE.finditer(source)):
        target = match.group(1).strip().split(" ", 1)[0].strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith(("#", "//")):
            continue
        relative = unquote(parsed.path)
        if relative and not (readme.parent / relative).exists():
            line = source.count("\n", 0, match.start()) + 1
            errors.append(f"README:{line}: missing local target {relative}")
    return errors


def publication_errors(site: Path) -> list[str]:
    errors: list[str] = []
    try:
        report.validate_site(site)
    except report.ReportError as error:
        errors.append(f"sealed benchmark-stats site: {error}")

    latest_path = site / "latest.json"
    if not latest_path.is_file():
        return [*errors, "benchmark-stats/latest.json is missing"]
    try:
        latest = report.read_json(latest_path)
        if "scaling" not in latest:
            return [*errors, "latest.json has no scaling report"]
        scaling = report.object_value(latest["scaling"], "latest.scaling")
        if scaling.get("metric_schema_version") != report.SCALING_SCHEMA:
            errors.append("latest.json scaling report is not the current distribution schema")
        patterns = report.list_value(scaling.get("patterns"), "latest.scaling.patterns")
        pattern_ids = {
            report.string_value(
                report.object_value(pattern, "latest.scaling.pattern").get("pattern"),
                "latest.scaling.pattern.id",
            )
            for pattern in patterns
        }
        if pattern_ids != set(report.SCALING_PATTERN_IDS):
            errors.append(
                f"latest.json scaling patterns: missing="
                f"{sorted(set(report.SCALING_PATTERN_IDS) - pattern_ids)}, "
                f"unexpected={sorted(pattern_ids - set(report.SCALING_PATTERN_IDS))}"
            )
        if scaling.get("thread_points") != list(report.SCALING_THREAD_POINTS):
            errors.append("latest.json scaling report does not cover all worker counts")
        if "thread_churn" not in scaling:
            errors.append("latest.json scaling report has no thread-churn side-car (#508)")
        run = report.object_value(scaling.get("run"), "latest.scaling.run")
        if run.get("source_ref") != "refs/heads/main":
            errors.append("latest.json scaling run is not from main")
        if not errors:
            # The strict validator checks every five-allocator cell and its raw
            # 3- or 40-repetition sample count, percentile values, and provenance.
            report.validate_scaling_report(scaling, "latest.scaling")
    except (OSError, ValueError, TypeError, report.ReportError) as error:
        errors.append(f"latest.json scaling validation: {error}")

    index = site / "index.html"
    if index.is_file():
        html = index.read_text(encoding="utf-8")
        if 'id="requested-size-distributions"' not in html:
            errors.append("dashboard is missing the requested-size-distributions anchor")
        if 'id="thread-churn"' not in html:
            errors.append("dashboard is missing the thread-churn anchor")
        for name in sorted(EXPECTED_SVGS):
            if f'src="{name}"' not in html:
                errors.append(f"dashboard is missing the {name} graphic")
    else:
        errors.append("benchmark-stats/index.html is missing")
    return errors


def raw_url_errors(site: Path) -> list[str]:
    """Check the exact raw URLs README embeds, including CDN visibility."""
    errors: list[str] = []
    for name in sorted(EXPECTED_SVGS):
        url = RAW_BASE + name
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                served = response.read(report.FILE_CAPS[name] + 1)
            if served != (site / name).read_bytes():
                errors.append(f"{url}: served bytes differ from sealed publication")
        except (OSError, urllib.error.URLError) as error:
            errors.append(f"{url}: {error}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--site-dir", type=Path, required=True)
    parser.add_argument("--pages-url")
    args = parser.parse_args()

    errors = readme_errors(args.readme) + publication_errors(args.site_dir)
    if not errors and args.pages_url:
        errors.extend(raw_url_errors(args.site_dir))
        try:
            report.audit_pages(args.site_dir, args.pages_url, attempts=1, delay_seconds=0)
        except report.ReportError as error:
            errors.append(f"deployed Pages site: {error}")
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    if errors:
        return 1
    print("PASS README links, full scaling report, sealed artifacts, and dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
