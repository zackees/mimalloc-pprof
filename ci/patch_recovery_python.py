#!/usr/bin/env python3
"""Make python-build-standalone's libpython loadable inside macOS Recovery.

Recovery ships a cut-down userland. `libpython3.12.dylib` from
python-build-standalone hard-links two ncurses libraries by absolute path:

    /usr/lib/libncurses.5.4.dylib   -- present in Recovery
    /usr/lib/libpanel.5.4.dylib     -- NOT present

so the interpreter aborts before main():

    dyld[450]: Library not loaded: /usr/lib/libpanel.5.4.dylib
      Reason: tried: '/usr/lib/libpanel.5.4.dylib' (no such file, no dyld cache)

This rewrites that one LC_LOAD_DYLIB path to a library Recovery does have.
libpython does carry undefined `panel_*` symbols, but they belong to the
_curses_panel extension; nothing on the path this lane uses
(run_test_bundle.py and memory_gate.py are stdlib-only) imports curses, so they
are never bound. Verified: the patched interpreter reports
"Python 3.12.11 (main, Jun 12 2025) [Clang 20.1.4]" inside Recovery and runs
the whole test bundle.

The replacement path must be no LONGER than the original, because a load
command's size is fixed; libSystem is both shorter and guaranteed present.

    python3 ci/patch_recovery_python.py <libpython3.12.dylib>
"""
import struct
import sys

OLD = b"/usr/lib/libpanel.5.4.dylib"
NEW = b"/usr/lib/libSystem.B.dylib"
# LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_LOAD_UPWARD_DYLIB
DYLIB_CMDS = (0x0C, 0x18, 0x1F, 0x20)


def patch(path):
    assert len(NEW) <= len(OLD), "replacement path must fit the existing load command"
    data = bytearray(open(path, "rb").read())
    magic, _, _, _, ncmds, _, _, _ = struct.unpack_from("<8I", data, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit("not a 64-bit little-endian Mach-O: %s (magic %#x)" % (path, magic))

    off, patched = 32, 0
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd in DYLIB_CMDS:
            (nameoff,) = struct.unpack_from("<I", data, off + 8)
            start = off + nameoff
            end = data.index(b"\x00", start)
            if bytes(data[start:end]) == OLD:
                # Clear the whole name field first: the old path is longer, and a
                # trailing tail would otherwise survive past the new NUL.
                data[start:off + cmdsize] = b"\x00" * (cmdsize - nameoff)
                data[start:start + len(NEW)] = NEW
                patched += 1
        off += cmdsize

    if patched != 1:
        raise SystemExit("expected exactly 1 %s load command, found %d"
                         % (OLD.decode(), patched))
    open(path, "wb").write(bytes(data))
    print("patched %s: %s -> %s" % (path, OLD.decode(), NEW.decode()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    patch(sys.argv[1])
