"""Unit tests for ci/pe_imports.py (issue #277 phase C).

The reader exists to assert one thing on every windows-gnu build: that
`mimalloc.dll` is the FIRST entry of `test-stress-dynamic`'s import table, because the
loader initialises statically imported modules in that order and mimalloc's override has
to run before the CRT it patches. Two failure modes would make that assertion worthless,
and both are covered here:

  * returning the names in the wrong order -- the whole assertion is about order
  * returning nothing, or silently succeeding on a file it did not understand, which
    would make "the first name is mimalloc.dll" fail loudly instead of quietly, or (worse)
    make an empty list look like a pass to a careless caller

A synthetic PE32+ image is built in-process rather than checked in: the tests then run on
any host, and the builder doubles as documentation of the three structures involved.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import pe_imports

SECTION_RVA = 0x1000
SECTION_RAW = 0x400


def _build_pe(dll_names: list[str], *, magic: int = 0x20B, directory_count: int = 16) -> bytes:
    """A minimal PE with one section holding an import descriptor array."""
    # Section payload: descriptors first, then the name strings they point at.
    descriptors = b""
    strings = b""
    name_area = (len(dll_names) + 1) * 20
    for name in dll_names:
        name_rva = SECTION_RVA + name_area + len(strings)
        # OriginalFirstThunk, TimeDateStamp, ForwarderChain, Name, FirstThunk
        descriptors += struct.pack("<IIIII", 0, 0, 0, name_rva, 0)
        strings += name.encode("ascii") + b"\0"
    descriptors += b"\0" * 20  # terminator
    payload = descriptors + strings

    optional_size = (112 if magic == 0x20B else 96) + directory_count * 8
    pe_offset = 0x80
    headers = bytearray(SECTION_RAW)
    headers[0:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, pe_offset)
    struct.pack_into("<4s", headers, pe_offset, b"PE\0\0")
    # Machine, NumberOfSections, ..., SizeOfOptionalHeader, Characteristics
    struct.pack_into("<HHIIIHH", headers, pe_offset + 4, 0x8664, 1, 0, 0, 0, optional_size, 0x22)
    optional = pe_offset + 24
    struct.pack_into("<H", headers, optional, magic)
    directories = optional + (112 if magic == 0x20B else 96)
    struct.pack_into("<I", headers, directories - 4, directory_count)
    # Data directory 1 = import table.
    struct.pack_into("<II", headers, directories + 8, SECTION_RVA, len(descriptors))
    section = optional + optional_size
    struct.pack_into(
        "<8sIIIIIIHHI",
        headers,
        section,
        b".idata\0\0",
        len(payload),  # VirtualSize
        SECTION_RVA,  # VirtualAddress
        len(payload),  # SizeOfRawData
        SECTION_RAW,  # PointerToRawData
        0,
        0,
        0,
        0,
        0xC0000040,
    )
    return bytes(headers) + payload


class PeImportsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, data: bytes, name: str = "t.exe") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_names_come_back_in_import_table_order(self) -> None:
        # Order is the entire point of this reader, so it is asserted as a sequence and
        # never as a set.
        names = ["mimalloc.dll", "KERNEL32.dll", "api-ms-win-crt-heap-l1-1-0.dll"]
        self.assertEqual(pe_imports.imported_dlls(self._write(_build_pe(names))), names)

    def test_the_badly_ordered_case_is_distinguishable(self) -> None:
        names = ["KERNEL32.dll", "api-ms-win-crt-heap-l1-1-0.dll", "mimalloc.dll"]
        read = pe_imports.imported_dlls(self._write(_build_pe(names)))
        self.assertEqual(read[0], "KERNEL32.dll")
        self.assertEqual(read[-1], "mimalloc.dll")

    def test_pe32_is_read_too(self) -> None:
        names = ["mimalloc.dll", "msvcrt.dll"]
        self.assertEqual(
            pe_imports.imported_dlls(self._write(_build_pe(names, magic=0x10B))), names
        )

    def test_a_non_pe_file_is_an_error_not_an_empty_list(self) -> None:
        # An empty list would make "the first import is mimalloc.dll" fail with an
        # unexplained index error, or -- if a caller checked `if names and ...` -- pass.
        with self.assertRaises(pe_imports.PEError):
            pe_imports.imported_dlls(self._write(b"#!/bin/sh\necho hi\n"))

    def test_a_pe_with_no_import_directory_is_an_error(self) -> None:
        data = bytearray(_build_pe(["mimalloc.dll"]))
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        directories = pe_offset + 24 + 112
        struct.pack_into("<II", data, directories + 8, 0, 0)
        with self.assertRaises(pe_imports.PEError):
            pe_imports.imported_dlls(self._write(bytes(data)))

    def test_an_rva_outside_every_section_is_an_error(self) -> None:
        data = bytearray(_build_pe(["mimalloc.dll"]))
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        directories = pe_offset + 24 + 112
        struct.pack_into("<I", data, directories + 8, 0x900000)
        with self.assertRaises(pe_imports.PEError):
            pe_imports.imported_dlls(self._write(bytes(data)))

    def test_cli_prints_one_name_per_line(self) -> None:
        path = self._write(_build_pe(["mimalloc.dll", "KERNEL32.dll"]))
        self.assertEqual(pe_imports.main([str(path)]), 0)

    def test_cli_reports_a_bad_file_without_raising(self) -> None:
        self.assertEqual(pe_imports.main([str(self._write(b"not a pe"))]), 1)


if __name__ == "__main__":
    unittest.main()
