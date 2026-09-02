#!/usr/bin/env python3
"""Measure page hole purging (`MIMALLOC_PURGE_HOLES`) on a Bun-shaped churn workload.

Reproduces the README's "scavenger and hole purging" chart and table: allocate many
small blocks across a few size classes, keep a scattered 1-in-20 of them alive, free
the rest, then idle for ~10s calling `mi_on_thread_idle()` every 100ms while sampling
this process's resident set size (`/proc/self/status` VmRSS). Run with
`MIMALLOC_PURGE_HOLES=0` and `=1` (the scavenger stays on in both; `MIMALLOC_PURGE_DELAY`
and `MIMALLOC_PURGE_HOLES_MIN_INTERVAL` at their defaults), median of 3 runs each, pinned
to CPUs 0-3.

The workload itself is a small, self-contained C program embedded in this script (kept
out of `test/` on purpose -- it is a benchmark driver, not a correctness test) that links
directly against `mimalloc.h`'s `mi_malloc`/`mi_free`/`mi_on_thread_idle` API, so no
malloc-override machinery is needed.

Usage:
    bench_hole_purging.py --build-dir <dir with libmimalloc.a> --include-dir <mimalloc include>
                           --out-dir .github/assets [--table] [--runs 3] [--seconds 10]

Outputs (under --out-dir):
    hole-purging-rss.csv          -- the median run's (config, t_seconds, rss_mb) samples
    hole-purging-report.json      -- raw mi_purge_holes_stats_t fields + peak RSS, both configs
    hole-purging-rss-{light,dark}.svg   -- the RSS-over-time line chart (default action)
    hole-purging-table-{light,dark}.svg -- the hole-characteristics table (--table)

`--check` (either mode) re-renders from the committed JSON/CSV without re-running the
workload, and exits nonzero if the committed SVGs are stale -- for CI / test use.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# The workload. Embedded rather than living under test/ (CLAUDE.md rule 2/6:
# this is a benchmark driver, not a correctness test, and has no business in
# test/ or CMakeLists.txt).
# ---------------------------------------------------------------------------
CHURN_C_SOURCE = r"""
#include <mimalloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Bun-shaped churn: allocate across a few small size classes, keep a scattered
   1-in-20 alive, free the rest, then idle -- calling mi_on_thread_idle() each
   tick, the only thing that makes hole purging (or the scavenger) run at all. */

typedef struct { size_t size; size_t count; } size_class_t;

static long vm_rss_kb(void) {
  FILE* f = fopen("/proc/self/status", "r");
  if (f == NULL) return -1;
  char line[256];
  long kb = -1;
  while (fgets(line, sizeof(line), f) != NULL) {
    if (strncmp(line, "VmRSS:", 6) == 0) {
      sscanf(line + 6, "%ld", &kb);
      break;
    }
  }
  fclose(f);
  return kb;
}

static void sleep_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
}

static char g_report_buf[1 << 20];
static size_t g_report_len = 0;

static void report_capture(const char* msg, void* arg) {
  (void)arg;
  size_t n = strlen(msg);
  if (g_report_len + n < sizeof(g_report_buf)) {
    memcpy(g_report_buf + g_report_len, msg, n);
    g_report_len += n;
  }
}

int main(int argc, char** argv) {
  int seconds = (argc > 1) ? atoi(argv[1]) : 10;
  int tick_ms = 100;

  static const size_class_t classes[] = {
    { 512,  150000 },
    { 1024, 100000 },
    { 2048,  50000 },
  };
  const size_t n_classes = sizeof(classes) / sizeof(classes[0]);

  size_t total = 0;
  for (size_t c = 0; c < n_classes; c++) total += classes[c].count;

  void** blocks = (void**)malloc(total * sizeof(void*));
  if (blocks == NULL) { fprintf(stderr, "out of memory allocating block table\n"); return 1; }

  /* Allocate every block, size classes interleaved so survivors land scattered
     across pages of every class rather than clustered by allocation order. */
  size_t idx = 0;
  size_t remaining[3];
  for (size_t c = 0; c < n_classes; c++) remaining[c] = classes[c].count;
  size_t class_cursor = 0;
  while (idx < total) {
    size_t tries = 0;
    while (remaining[class_cursor] == 0 && tries < n_classes) {
      class_cursor = (class_cursor + 1) % n_classes;
      tries++;
    }
    blocks[idx] = mi_malloc(classes[class_cursor].size);
    if (blocks[idx] != NULL) memset(blocks[idx], 0xAB, classes[class_cursor].size);
    remaining[class_cursor]--;
    class_cursor = (class_cursor + 1) % n_classes;
    idx++;
  }

  /* Keep 1-in-20 alive (scattered survivors); free the rest. */
  for (size_t i = 0; i < total; i++) {
    if (i % 20 != 0) {
      mi_free(blocks[i]);
      blocks[i] = NULL;
    }
  }

  long baseline_kb = vm_rss_kb();
  fprintf(stderr, "# allocated %zu blocks, freed %zu, baseline VmRSS %ld kB\n",
          total, total - (total + 19) / 20, baseline_kb);

  /* Idle loop: this is the only thing that makes purge_holes (or the plain
     scavenger) do anything at all. */
  long elapsed_ms = 0;
  while (elapsed_ms <= seconds * 1000) {
    long kb = vm_rss_kb();
    printf("CSV,%ld,%ld\n", elapsed_ms, kb);
    fflush(stdout);
    sleep_ms(tick_ms);
    mi_on_thread_idle();
    elapsed_ms += tick_ms;
  }

  mi_purge_holes_stats_t st;
  mi_purge_holes_stats_get(&st);
  printf(
    "STATS_JSON:{"
    "\"purged_bytes\":%zu,\"purged_blocks\":%zu,\"purged_bytes_total\":%zu,"
    "\"discard_calls\":%zu,\"reuse_calls\":%zu,\"pages_freed\":%zu,"
    "\"ineligible_pages\":%zu,\"ineligible_bytes\":%zu,\"ineligible_free_bytes\":%zu,"
    "\"unformed_bytes\":%zu,\"unformed_bytes_total\":%zu,"
    "\"unformed_discard_calls\":%zu,\"unformed_reuse_calls\":%zu,"
    "\"pages_skipped\":%zu,\"blocks_visited\":%zu,\"full_sweeps\":%zu}\n",
    st.purged_bytes, st.purged_blocks, st.purged_bytes_total,
    st.discard_calls, st.reuse_calls, st.pages_freed,
    st.ineligible_pages, st.ineligible_bytes, st.ineligible_free_bytes,
    st.unformed_bytes, st.unformed_bytes_total,
    st.unformed_discard_calls, st.unformed_reuse_calls,
    st.pages_skipped, st.blocks_visited, st.full_sweeps);

  mi_register_output(report_capture, NULL);
  mi_purge_holes_report();
  mi_register_output(NULL, NULL);
  printf("REPORT_BEGIN\n");
  fwrite(g_report_buf, 1, g_report_len, stdout);
  printf("\nREPORT_END\n");

  /* Keep survivors reachable until here so they cannot be freed early by an
     optimizer that thinks the table is dead; then release everything. */
  for (size_t i = 0; i < total; i += 20) mi_free(blocks[i]);
  free(blocks);
  return 0;
}
"""


class Sample(NamedTuple):
    t_ms: int
    rss_kb: int


@dataclass
class RunResult:
    samples: list[Sample]
    stats: dict
    report_text: str

    @property
    def mean_rss_kb_tail(self) -> float:
        """Mean RSS over the last 3s of the idle window -- the run-ranking scalar."""
        if not self.samples:
            return 0.0
        cutoff = self.samples[-1].t_ms - 3000
        tail = [s.rss_kb for s in self.samples if s.t_ms >= cutoff]
        return statistics.mean(tail) if tail else self.samples[-1].rss_kb


def compile_workload(include_dir: Path, lib_path: Path, work_dir: Path) -> Path:
    src = work_dir / "hole_purging_churn.c"
    src.write_text(CHURN_C_SOURCE, encoding="utf-8")
    exe = work_dir / "hole_purging_churn"
    cmd = [
        "cc", "-O2", "-g", "-I", str(include_dir), str(src), str(lib_path),
        "-lpthread", "-lrt", "-latomic", "-o", str(exe),
    ]
    subprocess.run(cmd, check=True)
    return exe


def run_once(exe: Path, holes_on: bool, seconds: int) -> RunResult:
    env = {
        "MIMALLOC_PURGE_HOLES": "1" if holes_on else "0",
        "PATH": "/usr/bin:/bin",
    }
    import os
    env["PATH"] = os.environ.get("PATH", env["PATH"])
    cmd = ["taskset", "-c", "0-3", str(exe), str(seconds)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
    samples: list[Sample] = []
    stats: dict = {}
    report_lines: list[str] = []
    in_report = False
    for line in proc.stdout.splitlines():
        if line.startswith("CSV,"):
            _, t_ms, rss_kb = line.split(",")
            samples.append(Sample(int(t_ms), int(rss_kb)))
        elif line.startswith("STATS_JSON:"):
            stats = json.loads(line[len("STATS_JSON:"):])
        elif line == "REPORT_BEGIN":
            in_report = True
        elif line == "REPORT_END":
            in_report = False
        elif in_report:
            report_lines.append(line)
    return RunResult(samples=samples, stats=stats, report_text="\n".join(report_lines))


def median_run(runs: list[RunResult]) -> RunResult:
    ranked = sorted(runs, key=lambda r: r.mean_rss_kb_tail)
    return ranked[len(ranked) // 2]


def measure(exe: Path, seconds: int, runs: int) -> dict[str, RunResult]:
    results: dict[str, RunResult] = {}
    for holes_on, label in ((False, "off"), (True, "on")):
        attempts = [run_once(exe, holes_on, seconds) for _ in range(runs)]
        picked = median_run(attempts)
        expect_purged = picked.stats.get("purged_bytes_total", 0)
        if holes_on and expect_purged <= 0:
            raise SystemExit(
                f"refusing to publish a no-op measurement: holes=on but "
                f"purged_bytes_total={expect_purged} (median of {runs} runs)"
            )
        if not holes_on and expect_purged != 0:
            raise SystemExit(
                f"refusing to publish: holes=off but purged_bytes_total="
                f"{expect_purged} != 0 (median of {runs} runs) -- MIMALLOC_PURGE_HOLES"
                " was not honored"
            )
        results[label] = picked
    return results


# ---------------------------------------------------------------------------
# Chart rendering (line chart), following ci/star_history.py's self-contained
# SVG style: no external assets, viewBox, ink-colored text, recessive grid.
# ---------------------------------------------------------------------------

WIDTH = 1000
HEIGHT = 420
PAD_LEFT = 64
PAD_RIGHT = 140
PAD_TOP = 56
PAD_BOTTOM = 48

# Palette skill's validated categorical slots 1 (blue) and 2 (orange). `node` was not
# available in this environment to re-run validate_palette.js, so these are used
# verbatim from references/palette.md rather than re-derived.
COLOR_OFF_LIGHT = "#eb6834"  # slot 2, orange -- "hole purging off"
COLOR_ON_LIGHT = "#2a78d6"   # slot 1, blue   -- "hole purging on"
COLOR_OFF_DARK = "#d95926"
COLOR_ON_DARK = "#3987e5"


@dataclass
class Theme:
    name: str
    background: str
    grid: str
    text: str
    muted: str
    off_line: str
    on_line: str


LIGHT = Theme("light", "#ffffff", "#d8dee4", "#1f2328", "#59636e", COLOR_OFF_LIGHT, COLOR_ON_LIGHT)
DARK = Theme("dark", "#0d1117", "#30363d", "#e6edf3", "#8b949e", COLOR_OFF_DARK, COLOR_ON_DARK)


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def nice_ticks(maximum: float) -> list[float]:
    if maximum <= 0:
        return [0, 1]
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        if maximum / step <= 6:
            top = ((maximum // step) + 1) * step
            return [i * step for i in range(int(top / step) + 1)]
    step = 500
    top = ((maximum // step) + 1) * step
    return [i * step for i in range(int(top / step) + 1)]


def render_line_chart(
    off_samples: list[Sample], on_samples: list[Sample], theme: Theme,
    source_line: str,
) -> str:
    max_t = max(off_samples[-1].t_ms, on_samples[-1].t_ms)
    max_rss = max(max(s.rss_kb for s in off_samples), max(s.rss_kb for s in on_samples)) / 1024.0
    ticks = nice_ticks(max_rss)
    top = ticks[-1]

    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM

    def x_of(t_ms: int) -> float:
        return PAD_LEFT + (t_ms / max_t) * plot_w

    def y_of(rss_mb: float) -> float:
        return PAD_TOP + plot_h - (rss_mb / top) * plot_h

    def path_of(samples: list[Sample]) -> str:
        pts = [f"M {x_of(samples[0].t_ms):.2f} {y_of(samples[0].rss_kb / 1024.0):.2f}"]
        for s in samples[1:]:
            pts.append(f"L {x_of(s.t_ms):.2f} {y_of(s.rss_kb / 1024.0):.2f}")
        return " ".join(pts)

    off_path = path_of(off_samples)
    on_path = path_of(on_samples)
    off_end_y = y_of(off_samples[-1].rss_kb / 1024.0)
    on_end_y = y_of(on_samples[-1].rss_kb / 1024.0)
    # Nudge end labels apart if they would collide (<14px apart).
    if abs(off_end_y - on_end_y) < 14:
        mid = (off_end_y + on_end_y) / 2
        if off_end_y <= on_end_y:
            off_end_y, on_end_y = mid - 7, mid + 7
        else:
            off_end_y, on_end_y = mid + 7, mid - 7

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" '
        f'aria-label="Resident memory: churn workload, hole purging off vs on">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{theme.background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<text x="{PAD_LEFT}" y="26" font-size="17" font-weight="600" fill="{theme.text}">'
        'Churn workload: resident memory after idle, hole purging off vs on</text>',
        f'<text x="{PAD_LEFT}" y="44" font-size="12" fill="{theme.muted}">'
        f'{escape(source_line)}</text>',
    ]

    for tick in ticks:
        y = y_of(tick)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.2f}" x2="{WIDTH - PAD_RIGHT}" y2="{y:.2f}" '
            f'stroke="{theme.grid}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 10}" y="{y + 4:.2f}" font-size="12" text-anchor="end" '
            f'fill="{theme.muted}">{int(tick)}</text>'
        )
    parts.append(
        f'<text x="{PAD_LEFT - 44}" y="{PAD_TOP - 8:.2f}" font-size="11" '
        f'fill="{theme.muted}">MB</text>'
    )

    for t_ms in (0, max_t):
        label = f"{t_ms / 1000:.0f}s"
        anchor = "start" if t_ms == 0 else "end"
        parts.append(
            f'<text x="{x_of(t_ms):.2f}" y="{HEIGHT - PAD_BOTTOM + 22:.2f}" font-size="12" '
            f'text-anchor="{anchor}" fill="{theme.muted}">{label}</text>'
        )

    parts.append(f'<path d="{off_path}" fill="none" stroke="{theme.off_line}" stroke-width="2" '
                  'stroke-linejoin="round" stroke-linecap="round"/>')
    parts.append(f'<path d="{on_path}" fill="none" stroke="{theme.on_line}" stroke-width="2" '
                  'stroke-linejoin="round" stroke-linecap="round"/>')

    # Direct end-of-line labels.
    parts.append(
        f'<text x="{x_of(off_samples[-1].t_ms) + 6:.2f}" y="{off_end_y + 4:.2f}" font-size="12" '
        f'fill="{theme.text}">hole purging off</text>'
    )
    parts.append(
        f'<text x="{x_of(on_samples[-1].t_ms) + 6:.2f}" y="{on_end_y + 4:.2f}" font-size="12" '
        f'fill="{theme.text}">hole purging on</text>'
    )

    # Legend (top-right), in addition to the direct labels.
    leg_x = WIDTH - PAD_RIGHT - 150
    leg_y = 20
    parts.append(f'<line x1="{leg_x}" y1="{leg_y}" x2="{leg_x + 20}" y2="{leg_y}" '
                 f'stroke="{theme.off_line}" stroke-width="2"/>')
    parts.append(f'<text x="{leg_x + 26}" y="{leg_y + 4}" font-size="12" fill="{theme.muted}">off</text>')
    parts.append(f'<line x1="{leg_x + 70}" y1="{leg_y}" x2="{leg_x + 90}" y2="{leg_y}" '
                 f'stroke="{theme.on_line}" stroke-width="2"/>')
    parts.append(f'<text x="{leg_x + 96}" y="{leg_y + 4}" font-size="12" fill="{theme.muted}">on</text>')

    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Table rendering (hole characteristics), matching the line chart's ink
# tokens / fonts / recessive rules.
# ---------------------------------------------------------------------------

TABLE_ROWS: list[tuple[str, str]] = [
    ("discard_calls", "discard syscalls (madvise/MEM_RESET)"),
    ("reuse_calls", "reuse syscalls (a hole handed back)"),
    ("purged_bytes_total", "bytes ever discarded"),
    ("pages_freed", "pages fully freed back to the arena"),
    ("ineligible_pages", "pages ineligible for discard (last sweep)"),
    ("ineligible_bytes", "bytes held by those pages (last sweep)"),
    ("unformed_bytes_total", "unformed-tail bytes discarded"),
    ("unformed_discard_calls", "unformed-tail discard syscalls"),
    ("pages_skipped", "pages skipped (nothing changed since last sweep)"),
    ("blocks_visited", "free-list blocks the sweep walked"),
    ("full_sweeps", "sweeps that walked every page"),
]

TABLE_WIDTH = 1000
ROW_H = 26
HEADER_H = 34
TITLE_H = 56
COL_LABEL_X = 24
COL_OFF_X = 620
COL_ON_X = 780
COL_DELTA_X = 940


def fmt_int(v: int) -> str:
    return f"{v:,}"


# (label, off value string, on value string) -- rendered above the counter rows,
# same two value columns, no delta (a delta of two percentages reads as noise).
def memory_summary_rows(off_summary: dict, on_summary: dict) -> list[tuple[str, str, str]]:
    off_peak, on_peak = off_summary["peak_rss_mb"], on_summary["peak_rss_mb"]
    off_after, on_after = off_summary["tail_mean_rss_mb"], on_summary["tail_mean_rss_mb"]
    off_pct = (off_peak - off_after) / off_peak * 100 if off_peak else 0.0
    on_pct = (on_peak - on_after) / on_peak * 100 if on_peak else 0.0
    return [
        ("peak RSS, before idle", f"{off_peak:.1f} MB", f"{on_peak:.1f} MB"),
        ("RSS after idle (median of 3)", f"{off_after:.1f} MB", f"{on_after:.1f} MB"),
        ("% returned (peak to after-idle)", f"{off_pct:.0f}%", f"{on_pct:.0f}%"),
    ]


def render_table_svg(
    off_stats: dict, on_stats: dict, off_summary: dict, on_summary: dict,
    theme: Theme, source_line: str,
) -> str:
    counter_rows = [(field, label) for field, label in TABLE_ROWS if field in off_stats and field in on_stats]
    mem_rows = memory_summary_rows(off_summary, on_summary)
    n_rows = len(mem_rows) + len(counter_rows)
    sep_gap = ROW_H // 2
    height = TITLE_H + HEADER_H + ROW_H * n_rows + sep_gap + 20

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TABLE_WIDTH} {height}" '
        f'width="{TABLE_WIDTH}" height="{height}" role="img" '
        f'aria-label="Hole purging characteristics, churn workload">',
        f'<rect width="{TABLE_WIDTH}" height="{height}" fill="{theme.background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<text x="{COL_LABEL_X}" y="26" font-size="17" font-weight="600" fill="{theme.text}">'
        'Hole purging characteristics: churn workload</text>',
        f'<text x="{COL_LABEL_X}" y="44" font-size="12" fill="{theme.muted}">'
        f'{escape(source_line)}</text>',
    ]

    header_y = TITLE_H + 14
    parts.append(f'<text x="{COL_LABEL_X}" y="{header_y}" font-size="12" font-weight="600" '
                 f'fill="{theme.muted}">measurement</text>')
    parts.append(f'<text x="{COL_OFF_X}" y="{header_y}" font-size="12" font-weight="600" '
                 f'text-anchor="end" fill="{theme.muted}">off</text>')
    parts.append(f'<text x="{COL_ON_X}" y="{header_y}" font-size="12" font-weight="600" '
                 f'text-anchor="end" fill="{theme.muted}">on</text>')
    parts.append(f'<text x="{COL_DELTA_X}" y="{header_y}" font-size="12" font-weight="600" '
                 f'text-anchor="end" fill="{theme.muted}">delta</text>')
    rule_y = TITLE_H + HEADER_H
    parts.append(f'<line x1="{COL_LABEL_X}" y1="{rule_y}" x2="{TABLE_WIDTH - COL_LABEL_X}" y2="{rule_y}" '
                 f'stroke="{theme.grid}" stroke-width="1"/>')

    # `top`: the y-coordinate of this row's top edge, growing downward. A single
    # cursor threaded through both blocks so the separator's gap is the only place
    # row height changes.
    top = rule_y
    zebra_index = 0

    def draw_row(label: str, off_str: str, on_str: str, delta_str: str) -> None:
        nonlocal top, zebra_index
        bottom = top + ROW_H
        text_y = bottom - 8
        if zebra_index % 2 == 1:
            parts.append(f'<rect x="{COL_LABEL_X - 8}" y="{top}" '
                         f'width="{TABLE_WIDTH - 2 * (COL_LABEL_X - 8)}" height="{ROW_H}" '
                         f'fill="{theme.grid}" opacity="0.25"/>')
        parts.append(f'<text x="{COL_LABEL_X}" y="{text_y}" font-size="12" fill="{theme.text}">'
                     f'{escape(label)}</text>')
        parts.append(f'<text x="{COL_OFF_X}" y="{text_y}" font-size="12" text-anchor="end" '
                     f'fill="{theme.text}">{off_str}</text>')
        parts.append(f'<text x="{COL_ON_X}" y="{text_y}" font-size="12" text-anchor="end" '
                     f'fill="{theme.text}">{on_str}</text>')
        parts.append(f'<text x="{COL_DELTA_X}" y="{text_y}" font-size="12" text-anchor="end" '
                     f'fill="{theme.muted}">{delta_str}</text>')
        top = bottom
        zebra_index += 1

    for label, off_str, on_str in mem_rows:
        draw_row(label, off_str, on_str, "—")  # em dash: no delta for a percentage row

    top += sep_gap
    sep_y = top - sep_gap // 2
    parts.append(f'<line x1="{COL_LABEL_X}" y1="{sep_y}" x2="{TABLE_WIDTH - COL_LABEL_X}" y2="{sep_y}" '
                 f'stroke="{theme.grid}" stroke-width="1" stroke-dasharray="2,3"/>')
    zebra_index = 0

    for field, label in counter_rows:
        off_v, on_v = off_stats[field], on_stats[field]
        delta = on_v - off_v
        draw_row(label, fmt_int(off_v), fmt_int(on_v), f"{delta:+,}" if delta != 0 else "0")

    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def probe_machine() -> tuple[str, str]:
    """Return (cpu model, kernel release) of the machine actually running the benchmark."""
    import platform
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return cpu or platform.machine(), platform.release()


def format_source_line(commit: str, cpu: str, kernel: str) -> str:
    return f"measured at {commit}, {cpu}, {kernel}, taskset -c 0-3, median of 3"


def build_source_line(commit: str) -> str:
    """Source line for a FRESH measurement -- probes this machine live.

    Do not use this for --check/--from-data: the committed JSON's own `cpu`/`kernel`
    fields must be read back instead (format_source_line), or the caption drifts
    from the SVG every time this runs on a different machine or after a rebase.
    """
    cpu, kernel = probe_machine()
    return format_source_line(commit, cpu, kernel)


def write_csv(path: Path, off: RunResult, on: RunResult) -> None:
    lines = ["config,t_seconds,rss_mb"]
    for label, run in (("off", off), ("on", on)):
        for s in run.samples:
            lines.append(f"{label},{s.t_ms / 1000:.1f},{s.rss_kb / 1024.0:.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_csv(path: Path) -> dict[str, list[Sample]]:
    out: dict[str, list[Sample]] = {"off": [], "on": []}
    lines = path.read_text(encoding="utf-8").splitlines()[1:]
    for line in lines:
        label, t_s, rss_mb = line.split(",")
        out[label].append(Sample(int(round(float(t_s) * 1000)), int(round(float(rss_mb) * 1024))))
    return out


def write_report_json(path: Path, off: RunResult, on: RunResult, commit: str, cpu: str, kernel: str) -> None:
    payload = {
        "commit": commit,
        "cpu": cpu,
        "kernel": kernel,
        "off": {
            "stats": off.stats,
            "peak_rss_mb": max(s.rss_kb for s in off.samples) / 1024.0,
            "tail_mean_rss_mb": off.mean_rss_kb_tail / 1024.0,
            "report_text": off.report_text,
        },
        "on": {
            "stats": on.stats,
            "peak_rss_mb": max(s.rss_kb for s in on.samples) / 1024.0,
            "tail_mean_rss_mb": on.mean_rss_kb_tail / 1024.0,
            "report_text": on.report_text,
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_report_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short=8", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-dir", type=Path, help="dir containing libmimalloc.a (Release, MI_PPROF=ON)")
    parser.add_argument("--include-dir", type=Path, default=REPO_ROOT / "include")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / ".github" / "assets")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--table", action="store_true", help="render the characteristics table instead of the line chart")
    parser.add_argument("--check", action="store_true", help="re-render from committed data; fail if stale; do not re-run the workload")
    parser.add_argument("--from-data", action="store_true", help="render from the already-committed CSV/JSON instead of re-measuring")
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "hole-purging-rss.csv"
    json_path = out_dir / "hole-purging-report.json"

    if args.check or args.from_data:
        if not csv_path.exists() or not json_path.exists():
            print(f"error: {csv_path} / {json_path} missing; run without --check first", file=sys.stderr)
            return 1
        series = load_csv(csv_path)
        report = load_report_json(json_path)
        # Read the committed machine/commit fields back rather than re-probing this
        # machine -- otherwise the caption drifts from the SVG on every rebase or on
        # a different box, and --check could never pass anywhere but where it was made.
        source_line = format_source_line(report["commit"], report["cpu"], report["kernel"])
        off_samples, on_samples = series["off"], series["on"]
        off_stats, on_stats = report["off"]["stats"], report["on"]["stats"]
        off_summary, on_summary = report["off"], report["on"]
    else:
        if args.build_dir is None:
            print("error: --build-dir is required unless --check/--from-data", file=sys.stderr)
            return 1
        lib_path = args.build_dir / "libmimalloc.a"
        if not lib_path.exists():
            print(f"error: {lib_path} not found", file=sys.stderr)
            return 1
        commit = git_commit(REPO_ROOT)
        cpu, kernel = probe_machine()
        source_line = format_source_line(commit, cpu, kernel)
        with tempfile.TemporaryDirectory(prefix="hole-purging-bench-") as tmp:
            exe = compile_workload(args.include_dir, lib_path, Path(tmp))
            results = measure(exe, args.seconds, args.runs)
        off, on = results["off"], results["on"]
        write_csv(csv_path, off, on)
        write_report_json(json_path, off, on, commit, cpu, kernel)
        off_samples, on_samples = off.samples, on.samples
        off_stats, on_stats = off.stats, on.stats
        off_summary = {
            "peak_rss_mb": max(s.rss_kb for s in off_samples) / 1024.0,
            "tail_mean_rss_mb": off.mean_rss_kb_tail / 1024.0,
        }
        on_summary = {
            "peak_rss_mb": max(s.rss_kb for s in on_samples) / 1024.0,
            "tail_mean_rss_mb": on.mean_rss_kb_tail / 1024.0,
        }
        print(
            f"off: peak {off_summary['peak_rss_mb']:.1f} MB, "
            f"tail-mean {off_summary['tail_mean_rss_mb']:.1f} MB; "
            f"on: peak {on_summary['peak_rss_mb']:.1f} MB, "
            f"tail-mean {on_summary['tail_mean_rss_mb']:.1f} MB "
            f"(purged {on_stats.get('purged_bytes_total', 0)/1e6:.1f} MB)"
        )

    if args.table:
        light_path = out_dir / "hole-purging-table-light.svg"
        dark_path = out_dir / "hole-purging-table-dark.svg"
        light_svg = render_table_svg(off_stats, on_stats, off_summary, on_summary, LIGHT, source_line)
        dark_svg = render_table_svg(off_stats, on_stats, off_summary, on_summary, DARK, source_line)
    else:
        light_path = out_dir / "hole-purging-rss-light.svg"
        dark_path = out_dir / "hole-purging-rss-dark.svg"
        light_svg = render_line_chart(off_samples, on_samples, LIGHT, source_line)
        dark_svg = render_line_chart(off_samples, on_samples, DARK, source_line)

    if args.check:
        stale = []
        for p, body in ((light_path, light_svg), (dark_path, dark_svg)):
            if not p.exists() or p.read_text(encoding="utf-8") != body:
                stale.append(p)
        if stale:
            print("error: stale: " + ", ".join(str(p) for p in stale), file=sys.stderr)
            return 1
        print("hole-purging artifacts are current")
        return 0

    light_path.write_text(light_svg, encoding="utf-8")
    dark_path.write_text(dark_svg, encoding="utf-8")
    print(f"wrote {light_path} ({light_path.stat().st_size} B), {dark_path} ({dark_path.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
