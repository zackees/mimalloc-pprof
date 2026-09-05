#!/usr/bin/env python3
"""Demo viewer for mimalloc heap snapshots -- a Python mirror of tools/mi-heapview.c.

Same commands, same columns, same ordering rules as the C tool, so the two can be diffed
(ci/tests/test_heap_snapshot_example.py does exactly that on a committed snapshot):

    heapview.py <snapshot> summary
    heapview.py <snapshot> sizes   [--top N] [--by-tid|--by-heap]
    heapview.py <snapshot> frag    [--top N] [--min-waste BYTES]
    heapview.py <snapshot> pages   [--top N] [--size BYTES] [--sort waste|addr|used] [--min-waste BYTES]
    heapview.py <snapshot> blocks  --addr 0xADDR
    heapview.py <snapshot> arenas
    heapview.py <snapshot> diff <snapshot2> [--top N] [--by-tid|--by-heap]

`peek` (reads block bytes out of a core file) is deliberately not mirrored; use the C tool.
"""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mi_snapshot
from mi_snapshot import Page, Snapshot


def _tid_label(tid: int) -> str:
    """`hv_tid_label` without the optional `<snapshot>.meta.json` thread names."""
    return f"0x{tid:x} (abandoned)" if tid <= 4 else f"0x{tid:x}"


def _group_key(page: Page, by: str) -> int:
    if by == "tid":
        return page.thread_id
    if by == "heap":
        return page.heap_seq
    return 0


def aggregate(snap: Snapshot, by: str) -> list[dict[str, int]]:
    """`hv_aggregate`: one row per (block_size, group), in first-seen order."""
    bins: OrderedDict[tuple[int, int], dict[str, int]] = OrderedDict()
    for p in snap.pages:
        key = (p.block_size, _group_key(p, by))
        row = bins.setdefault(
            key,
            {
                "block_size": p.block_size,
                "group": key[1],
                "pages": 0,
                "used": 0,
                "cap": 0,
                "committed": 0,
            },
        )
        row["pages"] += 1
        row["used"] += p.used
        row["cap"] += p.capacity
        row["committed"] += p.committed
    return list(bins.values())


def cmd_summary(snap: Snapshot) -> None:
    reserved = sum(a.size for a in snap.arenas)
    committed = sum(a.committed.popcount(a.slice_count) * snap.slice_size for a in snap.arenas)
    purgeable = sum(a.purge.popcount(a.slice_count) * snap.slice_size for a in snap.arenas)
    pc = sum(p.committed for p in snap.pages)
    pu = sum(p.used_bytes for p in snap.pages)
    pr = sum(p.reserved * p.block_size for p in snap.pages)
    frag = max(pc - pu, 0)
    print(
        f"snapshot   version={snap.version}  ptr={snap.ptr_size}  slice={snap.slice_size}  flags=0x{snap.flags:x}"
    )
    print(
        f"arenas     {len(snap.arenas)}   reserved={reserved}   committed={committed}   purgeable={purgeable}"
    )
    print(f"heaps      {len(snap.heaps)}")
    print(
        f"pages      {len(snap.pages)}   (abandoned={sum(p.is_abandoned for p in snap.pages)} full={sum(p.is_full for p in snap.pages)})"
    )
    print(f"page-mem   committed={pc}   used={pu}   reserved={pr}")
    pct = 100.0 * frag / pc if pc else 0.0
    print(f"waste      internal-frag={frag}   ({pct:.1f}% of committed pages)")


def cmd_sizes(snap: Snapshot, top: int, by: str) -> None:
    rows = sorted(aggregate(snap, by), key=lambda r: -r["committed"])
    if top > 0:
        rows = rows[:top]
    if by != "none":
        gcol = "thread" if by == "tid" else "heap"
        print(
            f"{'block_size':<12} {gcol:<28} {'pages':>8} {'used_blks':>12} {'used_bytes':>14} {'committed':>14} {'frag%':>7}"
        )
    else:
        print(
            f"{'block_size':<12} {'pages':>8} {'used_blks':>12} {'cap_blks':>12} {'used_bytes':>14} {'committed':>14} {'frag%':>7}"
        )
    for r in rows:
        used_b = r["used"] * r["block_size"]
        frag = 100.0 * (r["committed"] - used_b) / r["committed"] if r["committed"] else 0.0
        if by == "tid":
            print(
                f"{r['block_size']:<12} {_tid_label(r['group']):<28} {r['pages']:>8} {r['used']:>12} {used_b:>14} {r['committed']:>14} {frag:>6.1f}%"
            )
        elif by == "heap":
            print(
                f"{r['block_size']:<12} heap#{r['group']:<22} {r['pages']:>8} {r['used']:>12} {used_b:>14} {r['committed']:>14} {frag:>6.1f}%"
            )
        else:
            print(
                f"{r['block_size']:<12} {r['pages']:>8} {r['used']:>12} {r['cap']:>12} {used_b:>14} {r['committed']:>14} {frag:>6.1f}%"
            )


def _print_pages(pages: list[Page]) -> None:
    print(
        f"{'page_start':<18} {'block_sz':>10} {'used':>6} {'cap':>6} {'rsvd':>6} {'committed':>12} {'waste':>12} {'kind':>5} {'arn':>4} {'ab':>3} tid"
    )
    for p in pages:
        print(
            f"0x{p.page_start:016x} {p.block_size:>10} {p.used:>6} {p.capacity:>6} {p.reserved:>6} "
            f"{p.committed:>12} {p.waste:>12} {p.kind:>5} {p.arena_idx:>4} {'y' if p.is_abandoned else 'n':>3} 0x{p.thread_id:x}"
        )


def cmd_pages(snap: Snapshot, top: int, size: int, sort: str, min_waste: int) -> None:
    pages = [p for p in snap.pages if (not size or p.block_size == size) and p.waste >= min_waste]
    if sort == "addr":
        pages.sort(key=lambda p: p.page_start)
    elif sort == "used":
        pages.sort(key=lambda p: -p.used_bytes)
    else:
        pages.sort(key=lambda p: -p.waste)
    _print_pages(pages[:top] if top > 0 else pages)


def cmd_frag(snap: Snapshot, top: int, min_waste: int) -> None:
    cmd_pages(
        snap, top if top else 30, 0, "waste", min_waste
    )  # the C tool defaults `frag` to 30 rows


def cmd_blocks(snap: Snapshot, addr: int) -> None:
    for p in snap.pages:
        end = p.page_start + p.reserved * p.block_size
        if p.page_start <= addr < end:
            print(
                f"page 0x{p.page_start:x}  block_size={p.block_size}  used={p.used}/{p.capacity}  kind={p.kind}  arena={p.arena_idx}"
            )
            if p.freemap is None:
                print(
                    "(no per-block freemap recorded for this page; snapshot was taken without MI_SNAPSHOT_BLOCKS, or page was owned by another thread)"
                )
                return
            print(f"{'addr':<18} {'state':<6}")
            for j in range(p.capacity):
                if not p.block_is_free(j):
                    baddr = p.page_start + j * p.block_size
                    print(
                        f"0x{baddr:016x} used{'  <-- query' if baddr <= addr < baddr + p.block_size else ''}"
                    )
            return
    print(f"no page contains 0x{addr:x}")


def cmd_arenas(snap: Snapshot) -> None:
    print(
        f"{'idx':<4} {'base':<18} {'size':>12} {'slices':>8} {'committed':>12} {'free':>12} {'purgeable':>12} {'numa':>5}"
    )
    for a in snap.arenas:
        c = a.committed.popcount(a.slice_count) * snap.slice_size
        f = a.free.popcount(a.slice_count) * snap.slice_size
        pu = a.purge.popcount(a.slice_count) * snap.slice_size
        flags = (" pinned" if a.is_pinned else "") + (" excl" if a.is_exclusive else "")
        print(
            f"{a.idx:<4} 0x{a.base:016x} {a.size:>12} {a.slice_count:>8} {c:>12} {f:>12} {pu:>12} {a.numa_node:>5}{flags}"
        )


def cmd_diff(a: Snapshot, b: Snapshot, top: int, by: str) -> None:
    rows: OrderedDict[tuple[int, int], dict[str, int]] = OrderedDict()
    for sign, snap in ((1, b), (-1, a)):
        for r in aggregate(snap, by):
            d = rows.setdefault(
                (r["block_size"], r["group"]), {"d_used": 0, "d_committed": 0, "d_pages": 0}
            )
            d["d_used"] += sign * r["used"]
            d["d_committed"] += sign * r["committed"]
            d["d_pages"] += sign * r["pages"]
    ordered = sorted(rows.items(), key=lambda kv: -abs(kv[1]["d_committed"]))
    if top > 0:
        ordered = ordered[:top]
    gcol = "thread" if by == "tid" else ("heap" if by == "heap" else "")
    # printf pads by BYTES and "Δ" is two of them, so the C header is one column narrower
    # than it looks; Δcommitted is printed as a sign character followed by a %13s number.
    print(f"{'block_size':<12} {gcol:<28} {'Δblocks':>11} {'Δpages':>11} {'Δcommitted':>13}")
    for (bs, g), d in ordered:
        if d["d_used"] == 0 and d["d_committed"] == 0 and d["d_pages"] == 0:
            continue
        label = _tid_label(g) if by == "tid" else (f"heap#{g}" if by == "heap" else "")
        dc = d["d_committed"]
        sign = "-" if dc < 0 else "+"
        print(f"{bs:<12} {label:<28} {d['d_used']:>+12} {d['d_pages']:>+12} {sign}{abs(dc):>13}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("snapshot", type=Path)
    ap.add_argument(
        "cmd", choices=["summary", "sizes", "frag", "pages", "blocks", "arenas", "diff"]
    )
    ap.add_argument("snapshot2", nargs="?", type=Path)
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--sort", choices=["waste", "addr", "used"], default="waste")
    ap.add_argument("--min-waste", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0)
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--by-tid", action="store_true")
    grp.add_argument("--by-heap", action="store_true")
    args = ap.parse_args(argv)
    by = "tid" if args.by_tid else ("heap" if args.by_heap else "none")
    try:
        snap = mi_snapshot.load(args.snapshot)
    except (OSError, mi_snapshot.FormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "summary":
        cmd_summary(snap)
    elif args.cmd == "sizes":
        cmd_sizes(snap, args.top, by)
    elif args.cmd == "frag":
        cmd_frag(snap, args.top, args.min_waste)
    elif args.cmd == "pages":
        cmd_pages(snap, args.top, args.size, args.sort, args.min_waste)
    elif args.cmd == "blocks":
        if not args.addr:
            ap.error("blocks needs --addr")
        cmd_blocks(snap, args.addr)
    elif args.cmd == "arenas":
        cmd_arenas(snap)
    elif args.cmd == "diff":
        if args.snapshot2 is None:
            ap.error("diff needs a second snapshot")
        cmd_diff(snap, mi_snapshot.load(args.snapshot2), args.top, by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
