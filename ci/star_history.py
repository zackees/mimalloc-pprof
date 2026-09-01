#!/usr/bin/env python3
"""Render the repository's star-history chart from data we can still read.

GitHub restricted `/repos/{owner}/{repo}/stargazers` to a repo's own admins and
collaborators on 2026-06-30, and states the endpoint returns **empty responses**
during deprecation before it is removed outright.  That is what broke every
third-party star chart (star-history.com, starcharts, and the rest), and it will
eventually break any chart that depends on per-star timestamps -- including one we
render ourselves.

So this script does not trust that endpoint to be there.  It keeps its own series:

  * `stargazers_count` on the repo object is **not** restricted.  It is a single
    integer, it always works, and appending one (date, count) snapshot per day is
    enough to keep a chart growing forever.
  * While the stargazers list is still readable it carries exact per-star
    timestamps, which is strictly better -- so the whole series is rebuilt from it,
    but *only* when the list is complete (`len(list) == stargazers_count`).  A short
    or empty list is the deprecation signal, not data, and must never overwrite good
    history.  This is the failure the daily job would otherwise commit silently.

The committed JSON series is the durable asset; the SVGs are derived from it.

Usage:
    star_history.py [--repo OWNER/NAME] [--out-dir DIR] [--check]

`--check` renders to memory and exits nonzero if the committed files are stale,
for use as a CI assertion rather than an updater.
"""

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional, cast

DEFAULT_REPO = "zackees/mimalloc-pprof"

# Chart geometry, in user units (the SVG scales to whatever width it is given).
WIDTH = 1000
HEIGHT = 360
PAD_LEFT = 64
PAD_RIGHT = 24
PAD_TOP = 48
PAD_BOTTOM = 48


class Point(NamedTuple):
    """One (day, cumulative stars) sample of the series."""

    day: str
    stars: int


class Theme(NamedTuple):
    """Colors for one rendering of the chart."""

    name: str
    background: str
    grid: str
    text: str
    muted: str
    line: str
    fill: str


LIGHT = Theme(
    name="light",
    background="#ffffff",
    grid="#d8dee4",
    text="#1f2328",
    muted="#59636e",
    line="#0969da",
    fill="rgba(9,105,218,0.12)",
)

DARK = Theme(
    name="dark",
    background="#0d1117",
    grid="#30363d",
    text="#e6edf3",
    muted="#8b949e",
    line="#58a6ff",
    fill="rgba(88,166,255,0.15)",
)


def run_gh(args: list[str]) -> str:
    """Run `gh` and return stdout, raising with stderr attached on failure."""
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def fetch_star_count(repo: str) -> int:
    """Read `stargazers_count`, the one star number GitHub still serves to everyone."""
    out = run_gh(["api", f"repos/{repo}", "--jq", ".stargazers_count"]).strip()
    return int(out)


def fetch_starred_at(repo: str) -> list[str]:
    """Return every `starred_at` timestamp, or an empty list if the endpoint is gone.

    A failure here is expected rather than exceptional -- it is what the restriction
    looks like from the outside -- so it degrades to "no timestamps" instead of
    aborting the run.
    """
    try:
        out = run_gh(
            [
                "api",
                "--paginate",
                f"repos/{repo}/stargazers?per_page=100",
                "-H",
                "Accept: application/vnd.github.star+json",
                "--jq",
                ".[].starred_at",
            ]
        )
    except RuntimeError as exc:
        print(f"note: stargazer timestamps unavailable ({exc})", file=sys.stderr)
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def day_of(timestamp: str) -> str:
    """Convert an ISO-8601 `starred_at` value to a YYYY-MM-DD day."""
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.date().isoformat()


def series_from_timestamps(timestamps: list[str]) -> list[Point]:
    """Build an exact cumulative series, one point per star, oldest first."""
    ordered = sorted(timestamps)
    return [Point(day_of(ts), i + 1) for i, ts in enumerate(ordered)]


def load_series(path: Path) -> list[Point]:
    """Read a previously committed series, tolerating a missing file."""
    if not path.exists():
        return []
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON list of points")
    points: list[Point] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: expected each point to be an object")
        fields = cast("dict[str, object]", entry)
        day = fields.get("day")
        stars = fields.get("stars")
        if not isinstance(day, str) or not isinstance(stars, int):
            raise ValueError(f"{path}: each point needs a string day and integer stars")
        points.append(Point(day, stars))
    return points


def dump_series(path: Path, series: list[Point]) -> str:
    """Serialize the series as stable, diff-friendly JSON."""
    payload = [{"day": p.day, "stars": p.stars} for p in series]
    return json.dumps(payload, indent=2) + "\n"


def append_snapshot(series: list[Point], count: int, today: str) -> list[Point]:
    """Append today's count, replacing an existing entry for the same day."""
    trimmed = [p for p in series if p.day != today]
    if trimmed and trimmed[-1].stars == count:
        # Nothing moved since the last recorded day; leave the series untouched so
        # the daily job produces an empty diff instead of a no-op commit.
        return trimmed
    return [*trimmed, Point(today, count)]


def resolve_series(repo: str, existing: list[Point], today: str) -> list[Point]:
    """Prefer an exact rebuild from timestamps; otherwise extend what we already have."""
    count = fetch_star_count(repo)
    timestamps = fetch_starred_at(repo)

    if timestamps and len(timestamps) == count:
        return series_from_timestamps(timestamps)

    if timestamps:
        print(
            f"warning: stargazer list returned {len(timestamps)} of {count} stars -- "
            "treating it as deprecation output and keeping the committed series",
            file=sys.stderr,
        )
    return append_snapshot(existing, count, today)


def nice_ticks(maximum: int) -> list[int]:
    """Pick readable y-axis ticks from 0 to at least `maximum`."""
    if maximum <= 0:
        return [0, 1]
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000):
        if maximum / step <= 5:
            top = ((maximum + step - 1) // step) * step
            return list(range(0, top + step, step))
    step = 10 ** (len(str(maximum)) - 1)
    top = ((maximum + step - 1) // step) * step
    return list(range(0, top + step, step))


def escape(text: str) -> str:
    """Escape the three characters that matter inside SVG text nodes."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(series: list[Point], repo: str, theme: Theme) -> str:
    """Render the series as a self-contained step chart."""
    if not series:
        raise ValueError("refusing to render an empty chart")

    days = [date.fromisoformat(p.day).toordinal() for p in series]
    first_day, last_day = days[0], days[-1]
    span = max(last_day - first_day, 1)
    ticks = nice_ticks(series[-1].stars)
    top = ticks[-1]

    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM

    def x_of(ordinal: int) -> float:
        return PAD_LEFT + (ordinal - first_day) / span * plot_w

    def y_of(stars: int) -> float:
        return PAD_TOP + plot_h - (stars / top) * plot_h

    # Step-after: stars hold their value until the next star arrives.
    steps: list[str] = [f"M {x_of(days[0]):.2f} {y_of(series[0].stars):.2f}"]
    for ordinal, point in zip(days[1:], series[1:]):
        steps.append(f"L {x_of(ordinal):.2f} {y_of(point.stars):.2f}")
    line_path = " ".join(steps)
    area_path = (
        f"{line_path} L {x_of(last_day):.2f} {PAD_TOP + plot_h:.2f} "
        f"L {x_of(first_day):.2f} {PAD_TOP + plot_h:.2f} Z"
    )

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" '
        f'aria-label="Star history for {escape(repo)}">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{theme.background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<text x="{PAD_LEFT}" y="28" font-size="18" font-weight="600" '
        f'fill="{theme.text}">Star history</text>',
        f'<text x="{WIDTH - PAD_RIGHT}" y="28" font-size="13" text-anchor="end" '
        f'fill="{theme.muted}">{escape(repo)}</text>',
    ]

    for tick in ticks:
        y = y_of(tick)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.2f}" x2="{WIDTH - PAD_RIGHT}" y2="{y:.2f}" '
            f'stroke="{theme.grid}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 10}" y="{y + 4:.2f}" font-size="12" text-anchor="end" '
            f'fill="{theme.muted}">{tick}</text>'
        )

    for ordinal in (first_day, last_day):
        label = date.fromordinal(ordinal).isoformat()
        anchor = "start" if ordinal == first_day else "end"
        parts.append(
            f'<text x="{x_of(ordinal):.2f}" y="{HEIGHT - PAD_BOTTOM + 22:.2f}" font-size="12" '
            f'text-anchor="{anchor}" fill="{theme.muted}">{label}</text>'
        )

    parts.append(f'<path d="{area_path}" fill="{theme.fill}" stroke="none"/>')
    parts.append(
        f'<path d="{line_path}" fill="none" stroke="{theme.line}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    parts.append(
        f'<circle cx="{x_of(last_day):.2f}" cy="{y_of(series[-1].stars):.2f}" r="3.5" '
        f'fill="{theme.line}"/>'
    )
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="OWNER/NAME to chart")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(".github/assets"),
        help="directory holding star-history.json and the rendered SVGs",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit nonzero if the committed files are out of date",
    )
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir
    repo: str = args.repo
    json_path = out_dir / "star-history.json"

    existing = load_series(json_path)
    today = datetime.now(timezone.utc).date().isoformat()
    series = resolve_series(repo, existing, today)

    if not series:
        print(
            "error: no star data available and no committed series to fall back on", file=sys.stderr
        )
        return 1

    artifacts = {
        json_path: dump_series(json_path, series),
        out_dir / "star-history-light.svg": render_svg(series, repo, LIGHT),
        out_dir / "star-history-dark.svg": render_svg(series, repo, DARK),
    }

    if args.check:
        stale = [
            p
            for p, body in artifacts.items()
            if not p.exists() or p.read_text(encoding="utf-8") != body
        ]
        if stale:
            print(
                "error: star-history artifacts are stale: " + ", ".join(str(p) for p in stale),
                file=sys.stderr,
            )
            return 1
        print(f"star-history artifacts are current ({series[-1].stars} stars)")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for path, body in artifacts.items():
        path.write_text(body, encoding="utf-8")
    print(
        f"wrote {len(artifacts)} files for {repo}: {series[-1].stars} stars over {len(series)} points"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
