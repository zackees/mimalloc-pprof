#!/usr/bin/env python3
"""Peak-resident-byte attribution for the large-class rows (#575 Step 0, #422).

Untimed and deterministic enough to run locally: it builds the static library with
MI_DIAGNOSTICS=ON and MI_STAT=1, links ci/perf_ab.c against it, runs a row with
PERF_AB_HOLES_REPORT=1 (every worker, holding its live slots, prints the per-page resident split
that `mi_purge_holes_report()` collects with `mincore`), and sums the workers into buckets. It
measures no time. Perf *timing* stays with the perf-ab workflow.

    uv run ci/attribution_probe.py [--rows persistent sparse] [--reps 3] [--json out.json]

Each row runs twice per rep: the shipped default and a THP-off diagnostic arm
(MIMALLOC_ALLOW_THP=0). THP off is a diagnostic here, not a candidate (#575 Decisions).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIB = 1 << 20
KIB = 1 << 10
# name: perf_ab.c argv after the executable (threads generations min max ops pause table sizes slots)
ROWS: dict[str, list[str]] = {
    "persistent": ["8", "1", str(96 * KIB), str(512 * KIB), "400000", "0", "0", "uniform", "8"],
    "sparse": ["8", "1", str(64 * KIB), str(4 * MIB), "25000", "0", "0", "log", "8"],
}
# arm: (environment, extra compile-time defines). THP off and no-repurpose are diagnostics only.
ARMS: dict[str, tuple[dict[str, str], str]] = {
    "thp-default": ({}, ""),
    "thp-off": ({"MIMALLOC_ALLOW_THP": "0"}, ""),
    # #572 repurposing off (a zero per-tick budget): does the resident memory of empty pages come from it?
    "no-repurpose": ({}, "MI_LARGE_REPURPOSE_PER_TICK=0"),
}
CLASSES = ("live", "free_formed", "unformed", "slack")


def build(tmp: Path, defs: str) -> Path:
    bdir = tmp / f"build-{abs(hash(defs))}"
    flags = [
        "-DCMAKE_BUILD_TYPE=Release", "-DMI_BUILD_SHARED=OFF", "-DMI_BUILD_OBJECT=OFF",
        "-DMI_BUILD_TESTS=OFF", "-DMI_OVERRIDE=OFF", "-DMI_PPROF=OFF", "-DMI_MEMEVT=OFF",
        "-DMI_DIAGNOSTICS=ON", "-DMI_DHAT=OFF", "-DMI_OWNER_GATE=OFF", "-DMI_EXTRA_CPPDEFS=" + ";".join(d for d in ("MI_STAT=1", defs) if d),
    ]
    subprocess.run(["cmake", "-S", str(ROOT), "-B", str(bdir), *flags], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(bdir), "-j"], check=True, capture_output=True)
    exe = bdir / "perf_ab"
    subprocess.run(
        ["cc", "-O2", f"-I{ROOT / 'include'}", str(ROOT / "ci/perf_ab.c"), str(bdir / "libmimalloc.a"),
         "-o", str(exe), "-lpthread"],
        check=True,
    )
    return exe


def kv(line: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}


def mb(text: str, pattern: str) -> float:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else 0.0


def parse(stdout: str, stderr: str) -> dict:
    peak = int(stdout.split()[4])
    blocks = re.split(r"\n(?==== worker )", "\n" + stderr)
    workers = []
    for b in blocks:
        head = re.match(r"\s*=== worker (\d+) of \d+: (\d+) live slots, (\d+) live requested bytes", b)
        if not head:
            continue
        tot = kv(next(ln for ln in b.splitlines() if ln.startswith("ATTR total")))
        bins = [kv(ln) for ln in b.splitlines() if ln.startswith("ATTR bin")]
        smaps = re.search(r"Rss (\d+) kB, Anonymous (\d+) kB, AnonHugePages (\d+) kB", b)
        workers.append({
            "index": int(head.group(1)), "slots": int(head.group(2)), "requested": int(head.group(3)),
            "total": tot, "bins": bins,
            "rss_kb": int(smaps.group(1)), "thp_kb": int(smaps.group(3)),
            "arena": {
                "in_use": mb(b, r"resident \(mincore\): in use ([\d.]+) MB"),
                "fresh": mb(b, r"fresh ([\d.]+) MB, free_dirty"),
                "free_dirty": mb(b, r"free_dirty ([\d.]+) MB, queued"),
                "queued": mb(b, r"queued ([\d.]+) MB, aged"),
                "aged": mb(b, r"aged ([\d.]+) MB\n"),
            },
        })
    workers.sort(key=lambda w: w["index"])
    return {"peak_rss": peak, "workers": workers}


def buckets(run: dict) -> dict[str, float]:
    """Bytes per bucket for one run; the snapshot RSS is the smaps Rss of the last reporter."""
    ws = run["workers"]
    b: dict[str, float] = {}
    live_block = sum(w["total"]["used_live"] for w in ws)
    requested = sum(w["requested"] for w in ws)
    # requested exceeds the visible live blocks when some live blocks sit in singleton pages the walk
    # does not visit (sparse-large-buffers); those are live bytes too
    b["live requested (all pages)"] = requested
    b["block rounding (live blocks)"] = max(live_block - requested, 0)
    b["formed free, partly used pages"] = sum(w["total"]["used_free_formed"] for w in ws)
    b["formed free, empty pages"] = sum(w["total"]["empty_free_formed"] for w in ws)
    b["unformed tail, partly used pages"] = sum(w["total"]["used_unformed"] for w in ws)
    b["unformed, empty pages"] = sum(w["total"]["empty_unformed"] for w in ws)
    b["page-geometry slack (past reserved*bs, header)"] = sum(
        w["total"]["used_slack"] + w["total"]["empty_slack"] for w in ws)
    a = ws[-1]["arena"]
    pages_resident = sum(
        w["total"][f"{u}_{c}"] for w in ws for u in ("used", "empty") for c in CLASSES)
    hidden_live = max(requested - live_block, 0)
    b["arena in-use resident, in no visited page (unexplained)"] = max(
        a["in_use"] * MIB - pages_resident - hidden_live, 0)
    b["free slices: queued for purge (resident)"] = a["queued"] * MIB
    b["free slices: aged + dirty + fresh (resident)"] = (a["aged"] + a["free_dirty"] + a["fresh"]) * MIB
    rss = ws[-1]["rss_kb"] * KIB
    known = sum(b.values())
    b["outside the arena (meta, stacks, binary)"] = max(rss - known, 0)
    b["_rss"] = rss
    b["_thp"] = ws[-1]["thp_kb"] * KIB
    b["_peak"] = run["peak_rss"]
    b["_requested"] = requested
    b["_formed_empty"] = sum(r["formed_empty"] for w in ws for r in w["bins"])
    b["_reserved_empty"] = sum(r["reserved_empty"] for w in ws for r in w["bins"])
    b["_formed_used"] = sum(r["formed_used"] for w in ws for r in w["bins"])
    b["_reserved_used"] = sum(r["reserved_used"] for w in ws for r in w["bins"])
    b["_pages_extent"] = sum(w["total"]["extent"] for w in ws)
    return b


def bin_counts(run: dict) -> dict[int, tuple[int, int]]:
    out: dict[int, list[int]] = {}
    for w in run["workers"]:
        for r in w["bins"]:
            e = out.setdefault(r["block_size"], [0, 0])
            e[0] += r["pages"]
            e[1] += r["empty"]
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", nargs="+", default=list(ROWS), choices=list(ROWS))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    result: dict = {}
    with tempfile.TemporaryDirectory() as t:
        exes = {defs: build(Path(t), defs) for defs in {d for _, d in ARMS.values()}}
        for row in args.rows:
            for arm, (env, defs) in ARMS.items():
                exe = exes[defs]
                runs = []
                for _ in range(args.reps):
                    p = subprocess.run(
                        [str(exe), *ROWS[row], "500"], capture_output=True, text=True, check=True,
                        env={"PERF_AB_HOLES_REPORT": "1", **env, "PATH": "/usr/bin:/bin"},
                    )
                    runs.append(parse(p.stdout, p.stderr))
                keys = list(buckets(runs[0]))
                med = {k: statistics.median(buckets(r)[k] for r in runs) for k in keys}
                result[f"{row}/{arm}"] = {
                    "median": med, "bins": {str(k): v for k, v in bin_counts(runs[len(runs) // 2]).items()},
                    "runs": [buckets(r) for r in runs],
                }
                rss = med["_rss"]
                print(f"\n## {row}/8 {arm}: snapshot RSS {rss / MIB:.1f} MiB, peak {med['_peak'] / MIB:.1f} MiB, "
                      f"requested live {med['_requested'] / MIB:.1f} MiB, AnonHugePages {med['_thp'] / MIB:.1f} MiB")
                print("| bucket | MiB | per worker MiB | % of RSS |\n|---|---:|---:|---:|")
                for k in keys:
                    if not k.startswith("_"):
                        print(f"| {k} | {med[k] / MIB:.1f} | {med[k] / MIB / 8:.2f} | {100 * med[k] / rss:.1f} |")
                print(f"pages with a live block: formed {med['_formed_used'] / MIB:.1f} of {med['_reserved_used'] / MIB:.1f} MiB reserved; "
                      f"empty pages: formed {med['_formed_empty'] / MIB:.1f} of {med['_reserved_empty'] / MIB:.1f} MiB reserved")
                print("bins (block size: pages/empty): " + ", ".join(
                    f"{k // KIB}K: {a}/{b}" for k, (a, b) in bin_counts(runs[len(runs) // 2]).items()))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
