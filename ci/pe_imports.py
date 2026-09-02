#!/usr/bin/env -S uv run --script
"""Print the DLL names in a PE executable's import table, in import-table order.

Issue #277 phase C. The order is the point: the Windows loader initialises statically
imported modules in it, and mimalloc's override needs `mimalloc.dll` -- which pulls in
`mimalloc-redirect.dll` -- to be initialised before the CRT it patches. `ld`'s PE linker
script emits the descriptors under `SORT(*)(.idata$2)`, sorted by the *input file path as
spelled on the link line*, so the order is a property of how CMake names the import
library and is worth asserting on every build.

There is no tool for this on a stock `windows-latest`: `dumpbin` is MSVC-only, and the
job's objdump arrives with MSYS2, which is installed later (and would be the wrong thing
to depend on anyway, since the bundle is meant to run with no toolchain present). The
format needed here is small enough to read directly.

    uv run ci/pe_imports.py <file.exe|file.dll>

Prints one DLL name per line. Exits non-zero with a message on anything it cannot parse --
a silent empty list would make "mimalloc.dll is first" vacuously true.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

#: IMAGE_DIRECTORY_ENTRY_IMPORT.
_IMPORT_DIRECTORY_INDEX = 1
#: sizeof(IMAGE_IMPORT_DESCRIPTOR); the array is terminated by an all-zero entry.
_DESCRIPTOR_SIZE = 20
#: Offset of the `Name` RVA within IMAGE_IMPORT_DESCRIPTOR.
_DESCRIPTOR_NAME_OFFSET = 12


class PEError(Exception):
    """A file this reader will not guess at. Always says what it expected."""


def _u16(data: bytes, offset: int) -> int:
    return cast("int", struct.unpack_from("<H", data, offset)[0])


def _u32(data: bytes, offset: int) -> int:
    return cast("int", struct.unpack_from("<I", data, offset)[0])


class _Sections:
    """RVA -> file offset, via the section table."""

    def __init__(self, entries: Sequence[tuple[int, int, int, int]]) -> None:
        self._entries = list(entries)

    def offset(self, rva: int) -> int:
        for virtual_address, virtual_size, raw_size, raw_pointer in self._entries:
            # Use the larger of the two sizes: a section whose virtual size exceeds its
            # raw size is normal (bss-like tails), and one whose raw size is rounded up
            # past its virtual size is also normal. Either way the RVA has to land inside
            # the raw bytes for us to read it.
            span = max(virtual_size, raw_size)
            if virtual_address <= rva < virtual_address + span:
                delta = rva - virtual_address
                if delta >= raw_size:
                    raise PEError(f"RVA 0x{rva:x} falls outside the section's raw data")
                return raw_pointer + delta
        raise PEError(f"RVA 0x{rva:x} is not inside any section")


def _parse_headers(data: bytes) -> tuple[int, _Sections]:
    """Return (import directory RVA, section map)."""
    if len(data) < 64 or data[:2] != b"MZ":
        raise PEError("not a PE file (no MZ signature)")
    pe_offset = _u32(data, 0x3C)
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PEError("not a PE file (no PE\\0\\0 signature)")
    coff = pe_offset + 4
    section_count = _u16(data, coff + 2)
    optional_size = _u16(data, coff + 16)
    optional = coff + 20
    if optional + 2 > len(data):
        raise PEError("truncated before the optional header")
    magic = _u16(data, optional)
    if magic == 0x20B:  # PE32+
        directories_offset = optional + 112
    elif magic == 0x10B:  # PE32
        directories_offset = optional + 96
    else:
        raise PEError(f"unknown optional header magic 0x{magic:x}")

    directory_count = _u32(data, directories_offset - 4)
    if directory_count <= _IMPORT_DIRECTORY_INDEX:
        raise PEError("the optional header has no import data directory")
    entry = directories_offset + _IMPORT_DIRECTORY_INDEX * 8
    import_rva = _u32(data, entry)
    import_size = _u32(data, entry + 4)
    if import_rva == 0 or import_size == 0:
        raise PEError("the import data directory is empty (the file imports nothing)")

    sections_offset = optional + optional_size
    entries: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        header = sections_offset + index * 40
        if header + 40 > len(data):
            raise PEError("truncated section table")
        entries.append(
            (
                _u32(data, header + 12),
                _u32(data, header + 8),
                _u32(data, header + 16),
                _u32(data, header + 20),
            )
        )
    return import_rva, _Sections(entries)


def _read_cstring(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        raise PEError("unterminated string in the import table")
    return data[offset:end].decode("ascii", "replace")


def imported_dlls(path: Path) -> list[str]:
    data = path.read_bytes()
    import_rva, sections = _parse_headers(data)
    offset = sections.offset(import_rva)
    names: list[str] = []
    while True:
        if offset + _DESCRIPTOR_SIZE > len(data):
            raise PEError("the import descriptor array runs off the end of the file")
        descriptor = data[offset : offset + _DESCRIPTOR_SIZE]
        if descriptor == b"\0" * _DESCRIPTOR_SIZE:
            break
        name_rva = _u32(descriptor, _DESCRIPTOR_NAME_OFFSET)
        if name_rva == 0:
            break
        names.append(_read_cstring(data, sections.offset(name_rva)))
        offset += _DESCRIPTOR_SIZE
    if not names:
        raise PEError("the import table has no entries")
    return names


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("image", type=Path, help="a .exe or .dll")
    args = parser.parse_args(argv)
    image = Path(cast("Path", args.image))
    try:
        names = imported_dlls(image)
    except (PEError, OSError, struct.error) as exc:
        print(f"pe_imports: {image}: {exc}", file=sys.stderr)
        return 1
    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
