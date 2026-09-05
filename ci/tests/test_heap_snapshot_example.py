"""The Python heap-snapshot reader/viewer must agree with the C viewer, byte for byte.

Issue #338. `ci/tests/fixtures/heap-snapshot/test-snapshot.bin` was written by
`mimalloc-test-snapshot` on Linux x86_64; the `mi-heapview-*.txt` files next to it are
`tools/mi-heapview.c`'s output on that file. `examples/heap-snapshot/heapview.py` is
documented as a mirror of the C tool, so its output on the same input must be identical.
Regenerate the fixtures with the C tool if the *viewer* deliberately changes; a change
in the *writer's* format must bump the version and is caught by `test-snapshot-exit`.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "heap-snapshot"
SNAPSHOT = FIXTURES / "test-snapshot.bin"
EXAMPLE = ROOT / "examples" / "heap-snapshot"
HEAPVIEW = EXAMPLE / "heapview.py"

CASES = {
    "mi-heapview-summary.txt": ["summary"],
    "mi-heapview-sizes.txt": ["sizes"],
    "mi-heapview-sizes-by-tid.txt": ["sizes", "--by-tid"],
    "mi-heapview-frag.txt": ["frag"],
    "mi-heapview-pages-addr.txt": ["pages", "--sort", "addr"],
    "mi-heapview-arenas.txt": ["arenas"],
    "mi-heapview-diff.txt": ["diff", "SNAPSHOT2"],
}
SNAPSHOT2 = FIXTURES / "test-snapshot-2.bin"


def run_viewer(*args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(HEAPVIEW), str(SNAPSHOT), *args],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class HeapSnapshotExampleTests(unittest.TestCase):
    def test_reader_parses_the_fixture(self) -> None:
        sys.path.insert(0, str(EXAMPLE))
        import mi_snapshot

        snap = mi_snapshot.load(SNAPSHOT)
        self.assertEqual(snap.version, 1)
        self.assertEqual(snap.ptr_size, 8)
        self.assertEqual(len(snap.arenas), 1)
        self.assertGreater(len(snap.pages), 50)
        self.assertEqual(snap.footer_page_count, len(snap.pages))
        # MI_SNAPSHOT_BLOCKS was set: the writer thread's pages carry free maps
        self.assertTrue(any(p.freemap is not None for p in snap.pages))

    def test_python_viewer_matches_c_viewer(self) -> None:
        for fixture, args in CASES.items():
            with self.subTest(command=" ".join(args)):
                expected = (FIXTURES / fixture).read_text(encoding="utf-8")
                argv = [str(SNAPSHOT2) if a == "SNAPSHOT2" else a for a in args]
                self.assertEqual(run_viewer(*argv), expected)

    def test_bad_input_is_a_clean_error(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(HEAPVIEW), str(HEAPVIEW), "summary"],
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bad magic", proc.stderr)


if __name__ == "__main__":
    unittest.main()
