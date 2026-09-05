#!/usr/bin/env python3
"""Reference reader for mimalloc's binary heap snapshot (format version 1).

This is the executable format spec for `mi_heap_snapshot` / `mi_heap_snapshot_to_file`
(src/heap-snapshot.c; Bun parity, issue #338). The layout is fixed-width little-endian:

    header : u32 magic 'MIHS' (0x5348494D) | u32 version=1 | u32 ptr_size | u32 slice_size
             u32 flags | u32 reserved | u64 clock_ms | u64 writer_tid | u32 arena_count
    arena  : u32 'ARNA' | u32 idx | u64 base | u64 size_bytes | u32 slice_count
             u32 info_slices | u32 numa_node | u8 pinned | u8 exclusive | u8 pad[2]
             bitmap committed | bitmap free | bitmap purge
    bitmap : u32 chunk_count | u32 chunk_bytes | chunk_count * chunk_bytes bytes
             (chunk_count == 0 encodes a NULL bitmap; bit i of the bitmap = slice i)
    then   : u32 'PAGE' | page... | u64 0 sentinel        (per arena)
    then   : u32 'PAGE' | page... | u64 0                 (writer thread's non-arena pages)
    heap   : u32 'HEAP' | u64 heap_seq | u32 numa_node | u64 exclusive_arena
             u32 'PAGE' | page... | u64 0                 (heap's OS-backed abandoned pages)
    page   : u64 page_start | u64 slice_start | u64 block_size | u32 reserved | u32 capacity
             u32 used | u64 committed | u64 tid | u64 heap_seq | u32 arena_idx (0xFFFFFFFF = none)
             u32 slice_index | u32 slice_count | u8 memkind | u8 page_kind
             u8 abandoned | u8 full | u8 has_freemap | u8 pad[3]
             [ if has_freemap: u32 nbytes | nbytes bytes, bit j = 1 means block j is FREE ]
    footer : u32 ' END' | u64 page_count

The `u64 0` sentinel sits where the next page's `page_start` would be, so a page list is
read by peeking one u64 at a time. Every integer is little-endian regardless of host.

    import mi_snapshot
    snap = mi_snapshot.load("mimalloc-snapshot.1234.bin")
    for page in snap.pages: ...
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = 0x5348494D
VERSION = 1
SEC_ARENA = 0x414E5241
SEC_HEAP = 0x50414548
SEC_PAGE = 0x45474150
SEC_END = 0x444E4520

PAGE_KINDS = {
    0: "small",
    1: "medium",
    2: "large",
    3: "single",
}  # labels as tools/mi-heapview.c prints them


class FormatError(ValueError):
    """The bytes are not a version-1 snapshot, or are truncated."""


@dataclass
class Bitmap:
    chunk_count: int
    chunk_bytes: int
    data: bytes

    def popcount(self, nbits: int) -> int:
        """Set bits among the first `nbits` (slices beyond the arena are never set)."""
        total = 0
        full, rest = divmod(nbits, 8)
        total += sum(bin(b).count("1") for b in self.data[:full])
        if rest and full < len(self.data):
            total += bin(self.data[full] & ((1 << rest) - 1)).count("1")
        return total


@dataclass
class Arena:
    idx: int
    base: int
    size: int
    slice_count: int
    info_slices: int
    numa_node: int
    is_pinned: bool
    is_exclusive: bool
    committed: Bitmap
    free: Bitmap
    purge: Bitmap


@dataclass
class Heap:
    heap_seq: int
    numa_node: int
    exclusive_arena: int


@dataclass
class Page:
    page_start: int
    slice_start: int
    block_size: int
    reserved: int
    capacity: int
    used: int
    committed: int
    thread_id: int
    heap_seq: int
    arena_idx: int  # -1 when not arena-backed
    slice_index: int
    slice_count: int
    memkind: int
    page_kind: int
    is_abandoned: bool
    is_full: bool
    freemap: bytes | None  # None unless the writer recorded per-block state

    @property
    def used_bytes(self) -> int:
        return self.used * self.block_size

    @property
    def waste(self) -> int:
        """Committed bytes not covering a live block (`mi-heapview`'s `waste`)."""
        return max(self.committed - self.used_bytes, 0)

    @property
    def kind(self) -> str:
        return PAGE_KINDS.get(self.page_kind, f"kind{self.page_kind}")

    def block_is_free(self, j: int) -> bool:
        if self.freemap is None:
            raise ValueError("no per-block freemap recorded for this page")
        return bool((self.freemap[j >> 3] >> (j & 7)) & 1)


@dataclass
class Snapshot:
    version: int
    ptr_size: int
    slice_size: int
    flags: int
    clock_ms: int
    writer_tid: int
    arenas: list[Arena] = field(default_factory=list[Arena])
    heaps: list[Heap] = field(default_factory=list[Heap])
    pages: list[Page] = field(default_factory=list[Page])
    footer_page_count: int = 0


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, fmt: str) -> tuple[int, ...]:
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise FormatError(f"truncated at byte {self.pos} (wanted {size} more)")
        out = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return out

    def u8(self) -> int:
        return self.take("<B")[0]

    def u32(self) -> int:
        return self.take("<I")[0]

    def u64(self) -> int:
        return self.take("<Q")[0]

    def blob(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise FormatError(f"truncated blob at byte {self.pos} (wanted {n})")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out


def _bitmap(r: _Reader) -> Bitmap:
    chunk_count = r.u32()
    chunk_bytes = r.u32()
    return Bitmap(chunk_count, chunk_bytes, r.blob(chunk_count * chunk_bytes))


def _page(r: _Reader, page_start: int) -> Page:
    slice_start, block_size = r.u64(), r.u64()
    reserved, capacity, used = r.u32(), r.u32(), r.u32()
    committed, tid, heap_seq = r.u64(), r.u64(), r.u64()
    arena_idx_raw, slice_index, slice_count = r.u32(), r.u32(), r.u32()
    memkind, page_kind, abandoned, full, has_freemap = r.u8(), r.u8(), r.u8(), r.u8(), r.u8()
    for _ in range(3):  # padding
        r.u8()
    freemap = None
    if has_freemap:
        freemap = r.blob(r.u32())
    arena_idx = arena_idx_raw - (1 << 32) if arena_idx_raw >= (1 << 31) else arena_idx_raw
    return Page(
        page_start,
        slice_start,
        block_size,
        reserved,
        capacity,
        used,
        committed,
        tid,
        heap_seq,
        arena_idx,
        slice_index,
        slice_count,
        memkind,
        page_kind,
        bool(abandoned),
        bool(full),
        freemap,
    )


def _page_list(r: _Reader, snap: Snapshot) -> None:
    if r.u32() != SEC_PAGE:
        raise FormatError(f"expected PAGE section at byte {r.pos - 4}")
    while True:
        page_start = r.u64()
        if page_start == 0:
            return
        snap.pages.append(_page(r, page_start))


def parse(data: bytes) -> Snapshot:
    r = _Reader(data)
    if r.u32() != MAGIC:
        raise FormatError("bad magic: not a mimalloc heap snapshot")
    version = r.u32()
    if version != VERSION:
        raise FormatError(f"unsupported snapshot version {version} (reader knows {VERSION})")
    ptr_size, slice_size, flags, _reserved = r.u32(), r.u32(), r.u32(), r.u32()
    clock_ms, writer_tid = r.u64(), r.u64()
    snap = Snapshot(version, ptr_size, slice_size, flags, clock_ms, writer_tid)
    arena_count = r.u32()
    for _ in range(arena_count):
        if r.u32() != SEC_ARENA:
            raise FormatError(f"expected ARNA section at byte {r.pos - 4}")
        idx, base, size = r.u32(), r.u64(), r.u64()
        slice_count, info_slices, numa_raw = r.u32(), r.u32(), r.u32()
        pinned, exclusive = r.u8(), r.u8()
        for _ in range(2):  # padding
            r.u8()
        numa = numa_raw - (1 << 32) if numa_raw >= (1 << 31) else numa_raw
        committed, free, purge = _bitmap(r), _bitmap(r), _bitmap(r)
        snap.arenas.append(
            Arena(
                idx,
                base,
                size,
                slice_count,
                info_slices,
                numa,
                bool(pinned),
                bool(exclusive),
                committed,
                free,
                purge,
            )
        )
        _page_list(r, snap)
    _page_list(r, snap)  # the writer thread's non-arena pages
    while True:
        tag = r.u32()
        if tag == SEC_END:
            break
        if tag != SEC_HEAP:
            raise FormatError(f"unexpected section 0x{tag:08x} at byte {r.pos - 4}")
        heap_seq, numa_raw, excl = r.u64(), r.u32(), r.u64()
        numa = numa_raw - (1 << 32) if numa_raw >= (1 << 31) else numa_raw
        snap.heaps.append(Heap(heap_seq, numa, excl))
        _page_list(r, snap)
    snap.footer_page_count = r.u64()
    if snap.footer_page_count != len(snap.pages):
        raise FormatError(f"footer says {snap.footer_page_count} pages, read {len(snap.pages)}")
    return snap


def load(path: str | Path) -> Snapshot:
    return parse(Path(path).read_bytes())
