#!/usr/bin/env python3
"""Render the allocator feature-comparison table from its single source of truth.

`docs/allocator-features.json` is the only place a feature row, a per-allocator
status, its note and its source citation are written down. This script turns that
file into the two things a reader actually sees:

  1. A GitHub-Markdown table injected into `README.md` between the
     `<!-- feature-table:start -->` / `<!-- feature-table:end -->` markers.
  2. `.github/assets/allocator-features-{light,dark}.svg` -- the same table rendered
     as an image, with the `mimalloc-pprof` column on a highlighted background.

Why both. GitHub strips CSS (and inline `style=`) from rendered Markdown, so a
Markdown table *cannot* carry the "highlight the first data column" the table exists
to show; an `<img>` can. And an SVG cannot be searched, diffed in review, or read by
a screen reader the way a Markdown table can. So the region carries both, rendered
from one file, and `--check` is what keeps them from drifting apart.

Determinism. Nothing here reads the clock, the git index or the machine: the same
JSON renders the same bytes forever. That is what makes `--check` a gate rather than
a coin flip -- it re-renders and compares byte-for-byte, and fails naming whichever
of the three outputs is stale.

Status values, and why there are four of them:

  yes      the allocator has the feature                                    -> green check
  no       it does not                                                      -> red cross
  partial  it has part of it; the note must say which part                   -> amber bang
  value    the row is a measurement, not a capability (the "% returned"
           row); the note is printed verbatim with no glyph

A `no` or `partial` in the `mimalloc-pprof` column is a gap in this fork, so it must
name a tracking issue -- `ci/tests/test_render_feature_table.py` enforces that
against the real JSON rather than trusting review to catch it.

Usage:
    render_feature_table.py            # rewrite README region + both SVGs
    render_feature_table.py --check    # exit 1 if any of the three is stale
    render_feature_table.py --markdown # print just the Markdown table
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "allocator-features.json"
README = ROOT / "README.md"
ASSETS = ROOT / ".github" / "assets"
LIGHT_SVG = ASSETS / "allocator-features-light.svg"
DARK_SVG = ASSETS / "allocator-features-dark.svg"

START_MARKER = "<!-- feature-table:start -->"
END_MARKER = "<!-- feature-table:end -->"

#: The column whose gaps must carry an issue link, and the column the SVG highlights.
SUBJECT = "mimalloc-pprof"

MARKDOWN_GLYPH = {"yes": "✅", "no": "❌", "partial": "⚠️"}
VALID_STATUS = frozenset(("yes", "no", "partial", "value"))


# ---------------------------------------------------------------------------
# Data model. Hand-validated out of the parsed JSON rather than fed straight to a
# renderer: a typo in a status string should fail here, with the row name, instead
# of rendering a blank cell nobody notices.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocator:
    key: str
    label: str
    #: Second header line, e.g. the version or commit the column describes.
    version: str


@dataclass(frozen=True)
class Cell:
    status: str
    #: Short qualifier rendered next to the glyph. Kept terse -- it has to fit a
    #: table cell in both outputs.
    note: str
    #: A URL or a `path:line` in this tree. Never rendered into README (150 links
    #: would drown the table); it is the audit trail in the JSON.
    source: str


@dataclass(frozen=True)
class Row:
    feature: str
    cells: dict[str, Cell]


@dataclass(frozen=True)
class Group:
    name: str
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class FeatureDoc:
    allocators: tuple[Allocator, ...]
    groups: tuple[Group, ...]
    legend: str
    caption: str

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(a.key for a in self.allocators)

    def all_rows(self) -> list[Row]:
        return [row for group in self.groups for row in group.rows]


class FeatureDataError(ValueError):
    """The JSON parsed but does not describe a renderable table."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FeatureDataError(message)


def _as_str(raw: object, what: str) -> str:
    if not isinstance(raw, str):
        raise FeatureDataError(f"{what}: expected a string, got {type(raw).__name__}")
    return raw


def _as_dict(raw: object, what: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise FeatureDataError(f"{what}: expected an object, got {type(raw).__name__}")
    items = cast("dict[object, object]", raw)
    return {_as_str(key, f"{what}: key"): value for key, value in items.items()}


def _as_list(raw: object, what: str) -> list[object]:
    if not isinstance(raw, list):
        raise FeatureDataError(f"{what}: expected an array, got {type(raw).__name__}")
    return cast("list[object]", raw)


def parse_doc(raw_text: str) -> FeatureDoc:
    """Parse and validate the feature JSON. Raises FeatureDataError with the offending
    row's name, so a bad edit fails with something a human can act on."""
    top = _as_dict(json.loads(raw_text), "document")

    allocators: list[Allocator] = []
    for index, entry in enumerate(_as_list(top.get("allocators"), "allocators")):
        obj = _as_dict(entry, f"allocators[{index}]")
        allocators.append(
            Allocator(
                key=_as_str(obj.get("key"), f"allocators[{index}].key"),
                label=_as_str(obj.get("label"), f"allocators[{index}].label"),
                version=_as_str(obj.get("version"), f"allocators[{index}].version"),
            )
        )
    _require(len(allocators) >= 2, "allocators: need at least two columns to compare")
    _require(
        allocators[0].key == SUBJECT,
        f"allocators: {SUBJECT!r} must be the first data column (it is the highlighted one)",
    )
    keys = [a.key for a in allocators]
    _require(len(keys) == len(set(keys)), "allocators: duplicate key")

    groups: list[Group] = []
    for g_index, g_entry in enumerate(_as_list(top.get("groups"), "groups")):
        g_obj = _as_dict(g_entry, f"groups[{g_index}]")
        name = _as_str(g_obj.get("name"), f"groups[{g_index}].name")
        rows: list[Row] = []
        for r_index, r_entry in enumerate(_as_list(g_obj.get("rows"), f"groups[{g_index}].rows")):
            r_obj = _as_dict(r_entry, f"{name}[{r_index}]")
            feature = _as_str(r_obj.get("feature"), f"{name}[{r_index}].feature")
            _require(
                len(feature) * FEATURE_CHAR_PX <= MAX_FEATURE_PX,
                f"feature name {feature!r} is too wide for the SVG's first column "
                f"({len(feature) * FEATURE_CHAR_PX:.0f}px > {MAX_FEATURE_PX:.0f}px)",
            )
            cells_obj = _as_dict(r_obj.get("cells"), f"{name!r} row {feature!r}: cells")
            cells: dict[str, Cell] = {}
            for key in keys:
                _require(key in cells_obj, f"row {feature!r} has no cell for {key!r}")
                c_obj = _as_dict(cells_obj[key], f"row {feature!r} cell {key!r}")
                status = _as_str(c_obj.get("status"), f"row {feature!r} cell {key!r}: status")
                _require(
                    status in VALID_STATUS,
                    f"row {feature!r} cell {key!r}: status {status!r} is not one of "
                    f"{sorted(VALID_STATUS)}",
                )
                note = _as_str(c_obj.get("note", ""), f"row {feature!r} cell {key!r}: note")
                _require(
                    note_width(note) <= MAX_NOTE_PX,
                    f"row {feature!r} cell {key!r}: note {note!r} is too wide for the SVG "
                    f"column ({note_width(note):.0f}px > {MAX_NOTE_PX:.0f}px) and would run "
                    "into the next one -- shorten it",
                )
                cells[key] = Cell(
                    status=status,
                    note=note,
                    source=_as_str(c_obj.get("source"), f"row {feature!r} cell {key!r}: source"),
                )
            extra = set(cells_obj) - set(keys)
            _require(
                not extra, f"row {feature!r}: cell(s) for unknown allocator(s) {sorted(extra)}"
            )
            rows.append(Row(feature=feature, cells=cells))
        _require(bool(rows), f"group {name!r} has no rows")
        groups.append(Group(name=name, rows=tuple(rows)))
    _require(bool(groups), "groups: the table has no groups")

    features = [row.feature for group in groups for row in group.rows]
    _require(len(features) == len(set(features)), "two rows share one feature name")

    return FeatureDoc(
        allocators=tuple(allocators),
        groups=tuple(groups),
        legend=_as_str(top.get("legend"), "legend"),
        caption=_as_str(top.get("caption"), "caption"),
    )


def load_doc(path: Path = DATA) -> FeatureDoc:
    return parse_doc(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


#: An issue reference in a note (`#335`) becomes a link in the Markdown output when the
#: cell's source is that issue. The SVG leaves it as text -- GitHub serves the image
#: through an <img>, where an <a> would not be clickable anyway.
ISSUE_SOURCE_RE = re.compile(r"^https://github\.com/zackees/mimalloc-pprof/issues/(\d+)$")


def markdown_cell(cell: Cell) -> str:
    if cell.status == "value":
        return cell.note or "—"
    note = cell.note
    match = ISSUE_SOURCE_RE.match(cell.source.strip())
    if match is not None:
        number = match.group(1)
        note = note.replace(f"#{number}", f"[#{number}]({cell.source.strip()})")
    return f"{MARKDOWN_GLYPH[cell.status]} {note}".strip()


def render_markdown(doc: FeatureDoc) -> str:
    """One table, groups as bold spanning-ish rows. GitHub has no rowspan in Markdown,
    so a group is a bold label row with empty data cells -- which reads as a section
    break and survives Markdown-to-anything conversion."""
    width = len(doc.allocators)
    header = ["Feature"] + [
        (f"**{a.label}**" if a.key == SUBJECT else a.label) for a in doc.allocators
    ]
    sub = [""] + [a.version for a in doc.allocators]
    lines = [
        "| " + " | ".join(header) + " |",
        "|---" + "|:--" * width + "|",
        "| " + " | ".join(sub) + " |",
    ]
    for group in doc.groups:
        lines.append(f"| **{group.name}** |" + " |" * width)
        for row in group.rows:
            cells = [markdown_cell(row.cells[a.key]) for a in doc.allocators]
            lines.append("| " + " | ".join([row.feature, *cells]) + " |")
    return "\n".join(lines)


def render_readme_region(doc: FeatureDoc) -> str:
    """Everything between the markers: the highlighted image, then the Markdown table
    (searchable, screen-readable, diffable), then the legend."""
    alt = (
        "Feature comparison of mimalloc-pprof, upstream mimalloc v3, Bun's mimalloc and "
        "jemalloc across memory return, profiling, robustness, platform support and "
        "allocator design"
    )
    return "\n".join(
        [
            START_MARKER,
            "",
            "<picture>",
            '  <source media="(prefers-color-scheme: dark)" '
            'srcset=".github/assets/allocator-features-dark.svg" />',
            '  <source media="(prefers-color-scheme: light)" '
            'srcset=".github/assets/allocator-features-light.svg" />',
            f'  <img alt="{alt}" src=".github/assets/allocator-features-light.svg" width="100%" />',
            "</picture>",
            "",
            render_markdown(doc),
            "",
            doc.legend,
            "",
            END_MARKER,
        ]
    )


def splice_readme(readme_text: str, region: str) -> str:
    start = readme_text.find(START_MARKER)
    end = readme_text.find(END_MARKER)
    if start < 0 or end < 0:
        raise FeatureDataError(
            f"README.md is missing the {START_MARKER} / {END_MARKER} markers; "
            "the renderer owns everything between them and will not guess where to put it"
        )
    if end < start:
        raise FeatureDataError(f"README.md has {END_MARKER} before {START_MARKER}")
    return readme_text[:start] + region + readme_text[end + len(END_MARKER) :]


# ---------------------------------------------------------------------------
# SVG. Same ink tokens as ci/bench_hole_purging_allocators.py's table, so the two
# images in the README look like one family.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    grid: str
    text: str
    muted: str
    #: Background wash behind the mimalloc-pprof column -- the whole point of the SVG.
    highlight: str
    highlight_rule: str
    yes: str
    no: str
    partial: str


LIGHT = Theme(
    name="light",
    background="#ffffff",
    grid="#d8dee4",
    text="#1f2328",
    muted="#59636e",
    highlight="#ddebfb",
    highlight_rule="#2a78d6",
    yes="#1a7f47",
    no="#cf222e",
    partial="#bf8700",
)
DARK = Theme(
    name="dark",
    background="#0d1117",
    grid="#30363d",
    text="#e6edf3",
    muted="#8b949e",
    highlight="#152741",
    highlight_rule="#3987e5",
    yes="#3fb950",
    no="#f85149",
    partial="#d29922",
)

WIDTH = 1284
FEATURE_X = 24
COL_START = 420
COL_W = 210
ROW_H = 24
GROUP_H = 30
HEADER_H = 46
TITLE_H = 72
FONT_PX = 12
#: Rough advance width of one character of the 11px note text. An SVG has no layout
#: engine, so this is the only thing keeping a long note out of the next column.
NOTE_CHAR_PX = 5.4
#: Ditto for the 12px feature column.
FEATURE_CHAR_PX = 6.3
GLYPH_R = 7.0
#: How wide a glyph-plus-note pair may get before it touches the neighbouring column.
#: Deliberately generous, and checked at parse time -- an SVG that overflows renders
#: perfectly happily and only looks wrong, which is the kind of breakage nobody
#: notices until it is on the front page.
MAX_NOTE_PX = COL_W - 12 - 2 * GLYPH_R - 4
MAX_FEATURE_PX = COL_START - FEATURE_X - 16


def escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def column_center(index: int) -> float:
    return COL_START + COL_W * index + COL_W / 2


def note_width(text: str) -> float:
    return len(text) * NOTE_CHAR_PX


def glyph_svg(cx: float, cy: float, status: str, theme: Theme) -> list[str]:
    """A drawn glyph, not an emoji. GitHub serves the SVG through an <img>, so a ✅ in
    a <text> renders in whatever colour-emoji font the viewer happens to have -- or in
    no font at all, as a tofu box. A circle plus a stroked path always renders."""
    colour = {"yes": theme.yes, "no": theme.no, "partial": theme.partial}[status]
    parts = [f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{GLYPH_R}" fill="{colour}"/>']
    if status == "yes":
        parts.append(
            f'<path d="M {cx - 3.4:.1f} {cy + 0.2:.1f} l 2.2 2.4 l 4.2 -5.0" fill="none" '
            f'stroke="#ffffff" stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round"/>'
        )
    elif status == "no":
        parts.append(
            f'<path d="M {cx - 3.0:.1f} {cy - 3.0:.1f} l 6.0 6.0 M {cx + 3.0:.1f} '
            f'{cy - 3.0:.1f} l -6.0 6.0" fill="none" stroke="#ffffff" stroke-width="1.7" '
            f'stroke-linecap="round"/>'
        )
    else:
        parts.append(
            f'<path d="M {cx:.1f} {cy - 3.6:.1f} l 0 4.4" fill="none" stroke="#ffffff" '
            f'stroke-width="1.8" stroke-linecap="round"/>'
        )
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy + 3.4:.1f}" r="1.1" fill="#ffffff"/>')
    return parts


def cell_svg(index: int, baseline: float, cell: Cell, theme: Theme) -> list[str]:
    """Glyph, then the note to its right, the pair centred in the column."""
    cx = column_center(index)
    cy = baseline - 4
    if cell.status == "value":
        return [
            f'<text x="{cx:.1f}" y="{baseline:.1f}" font-size="11" text-anchor="middle" '
            f'fill="{theme.text}">{escape(cell.note or "—")}</text>'
        ]
    note = cell.note
    if not note:
        return glyph_svg(cx, cy, cell.status, theme)
    total = 2 * GLYPH_R + 4 + note_width(note)
    left = cx - total / 2
    parts = glyph_svg(left + GLYPH_R, cy, cell.status, theme)
    parts.append(
        f'<text x="{left + 2 * GLYPH_R + 4:.1f}" y="{baseline:.1f}" font-size="11" '
        f'fill="{theme.muted}">{escape(note)}</text>'
    )
    return parts


#: Character budget for one caption line, from the 11px note metric and the page width.
CAPTION_CHARS = int((WIDTH - 2 * FEATURE_X) / NOTE_CHAR_PX)


def wrap_caption(text: str) -> list[str]:
    """Greedy word wrap. An SVG <text> does not wrap, so a long caption silently runs
    off the right edge of the image -- which is exactly what the first render did."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > CAPTION_CHARS:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def title_height(doc: FeatureDoc) -> int:
    return TITLE_H + 15 * (len(wrap_caption(doc.caption)) - 1)


def svg_height(doc: FeatureDoc) -> int:
    body = sum(GROUP_H + ROW_H * len(g.rows) for g in doc.groups)
    return title_height(doc) + HEADER_H + body + 46


def render_svg(doc: FeatureDoc, theme: Theme) -> str:
    height = svg_height(doc)
    top_h = title_height(doc)
    body_top = top_h + HEADER_H
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" role="img" '
        f'aria-label="Allocator feature comparison; the mimalloc-pprof column is '
        f'highlighted">',
        f'<rect width="{WIDTH}" height="{height}" fill="{theme.background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
    ]

    # The highlight wash runs the full height of the header + body, which is what makes
    # the subject column read as the subject rather than as just another column.
    parts.append(
        f'<rect x="{COL_START}" y="{top_h - 22}" width="{COL_W}" '
        f'height="{height - top_h - 24}" fill="{theme.highlight}"/>'
    )
    parts.append(
        f'<rect x="{COL_START}" y="{top_h - 22}" width="{COL_W}" height="3" '
        f'fill="{theme.highlight_rule}"/>'
    )

    parts.append(
        f'<text x="{FEATURE_X}" y="26" font-size="17" font-weight="600" fill="{theme.text}">'
        "Allocator feature comparison</text>"
    )
    for index, line in enumerate(wrap_caption(doc.caption)):
        parts.append(
            f'<text x="{FEATURE_X}" y="{46 + index * 15}" font-size="11" '
            f'fill="{theme.muted}">{escape(line)}</text>'
        )

    for index, allocator in enumerate(doc.allocators):
        cx = column_center(index)
        weight = "700" if allocator.key == SUBJECT else "600"
        ink = theme.text if allocator.key == SUBJECT else theme.muted
        parts.append(
            f'<text x="{cx:.1f}" y="{top_h + 6}" font-size="12.5" font-weight="{weight}" '
            f'text-anchor="middle" fill="{ink}">{escape(allocator.label)}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{top_h + 21}" font-size="10" text-anchor="middle" '
            f'fill="{theme.muted}">{escape(allocator.version)}</text>'
        )
    parts.append(
        f'<line x1="{FEATURE_X}" y1="{body_top}" x2="{WIDTH - FEATURE_X}" y2="{body_top}" '
        f'stroke="{theme.grid}" stroke-width="1"/>'
    )

    top = float(body_top)
    for group in doc.groups:
        parts.append(
            f'<text x="{FEATURE_X}" y="{top + 21:.1f}" font-size="12" font-weight="700" '
            f'fill="{theme.text}" letter-spacing="0.4">{escape(group.name.upper())}</text>'
        )
        top += GROUP_H
        for r_index, row in enumerate(group.rows):
            baseline = top + ROW_H - 8
            if r_index % 2 == 1:
                parts.append(
                    f'<rect x="{FEATURE_X - 8}" y="{top:.1f}" '
                    f'width="{WIDTH - 2 * (FEATURE_X - 8)}" height="{ROW_H}" '
                    f'fill="{theme.grid}" opacity="0.22"/>'
                )
            parts.append(
                f'<text x="{FEATURE_X}" y="{baseline:.1f}" font-size="{FONT_PX}" '
                f'fill="{theme.text}">{escape(row.feature)}</text>'
            )
            for index, allocator in enumerate(doc.allocators):
                parts.extend(cell_svg(index, baseline, row.cells[allocator.key], theme))
            top += ROW_H

    legend_y = top + 24
    for index, (status, label) in enumerate(
        (("yes", "has it"), ("no", "does not"), ("partial", "partly -- see the note"))
    ):
        x = FEATURE_X + index * 190
        parts.extend(glyph_svg(x + GLYPH_R, legend_y - 4, status, theme))
        parts.append(
            f'<text x="{x + 2 * GLYPH_R + 5:.1f}" y="{legend_y:.1f}" font-size="11" '
            f'fill="{theme.muted}">{escape(label)}</text>'
        )
    parts.append(
        f'<text x="{FEATURE_X + 3 * 190:.1f}" y="{legend_y:.1f}" font-size="11" '
        f'fill="{theme.muted}">Every cell cites a source in '
        f"docs/allocator-features.json</text>"
    )
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def outputs(doc: FeatureDoc, readme_text: str) -> list[tuple[Path, str]]:
    return [
        (README, splice_readme(readme_text, render_readme_region(doc))),
        (LIGHT_SVG, render_svg(doc, LIGHT)),
        (DARK_SVG, render_svg(doc, DARK)),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if README or either SVG is stale",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print the Markdown table to stdout and exit",
    )
    args = parser.parse_args(argv)

    try:
        doc = load_doc()
    except FeatureDataError as exc:
        print(f"docs/allocator-features.json: {exc}", file=sys.stderr)
        return 2

    if args.markdown:
        print(render_markdown(doc))
        return 0

    readme_text = README.read_text(encoding="utf-8")
    try:
        rendered = outputs(doc, readme_text)
    except FeatureDataError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        stale = [
            path
            for path, body in rendered
            if not path.exists() or path.read_text(encoding="utf-8") != body
        ]
        if stale:
            for path in stale:
                print(f"stale: {path.relative_to(ROOT)}", file=sys.stderr)
            print(
                "\ndocs/allocator-features.json and its rendered outputs disagree. "
                "Run `uv run ci/render_feature_table.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"up to date: README.md region, {LIGHT_SVG.name}, {DARK_SVG.name}")
        return 0

    for path, body in rendered:
        path.write_text(body, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
