# Live heap snapshot — reference reader and demo viewer

`mi_heap_snapshot` / `mi_heap_snapshot_to_file` (issue #338, Bun parity) write a compact
binary description of every arena and page — and, with `MI_SNAPSHOT_BLOCKS`, every block
the calling thread owns — for offline analysis. Format version 1 is byte-identical to
oven-sh/mimalloc's, so a file from either allocator opens in either viewer.

| file | role |
|---|---|
| `mi_snapshot.py` | the executable format spec: bytes → `Snapshot(arenas, heaps, pages)`. Importable, no I/O policy, stdlib only. |
| `heapview.py` | demo viewer mirroring `tools/mi-heapview.c`: `summary`, `sizes`, `frag`, `pages`, `blocks`, `arenas`, `diff`. Same columns, same ordering, so the two can be diffed. |
| `demo.c` | a two-thread workload that writes a snapshot through the API. |

## Produce a snapshot

Either from code:

```c
#include <mimalloc.h>
mi_heap_snapshot_to_file("snap.bin", MI_SNAPSHOT_BLOCKS);   // 0 = ok, -1 = write error
```

or let the allocator write one when the process exits — no code change:

```sh
MIMALLOC_SNAPSHOT_ON_EXIT=2 MIMALLOC_SNAPSHOT_PATH=/tmp/snap.bin ./your-program
#                        1 = pages only, 2 = pages + per-block free maps
```

Without `MIMALLOC_SNAPSHOT_PATH` the file is `mimalloc-snapshot.<pid>.bin` in the working
directory.

## Read it

```sh
uv run examples/heap-snapshot/heapview.py /tmp/snap.bin summary
uv run examples/heap-snapshot/heapview.py /tmp/snap.bin sizes --top 10      # where the committed bytes are, by block size
uv run examples/heap-snapshot/heapview.py /tmp/snap.bin frag --top 10       # pages wasting the most committed memory
uv run examples/heap-snapshot/heapview.py /tmp/snap.bin blocks --addr 0x...  # live blocks in the page containing an address
uv run examples/heap-snapshot/heapview.py a.bin diff b.bin                   # what grew between two snapshots
```

`waste` is `committed - used * block_size` per page: memory the OS has backed for that
page that no live block covers. `sizes` ranks by committed bytes, `frag` by waste,
`diff` by |Δcommitted|.

The C tool (`mi-heapview`, built with the library) has one more command, `peek`, which
reads block contents out of a core file; it is deliberately not mirrored here.

`ci/tests/test_heap_snapshot_example.py` runs `heapview.py` on a committed snapshot and
requires its output to equal `mi-heapview`'s for every mirrored command, so the Python
reader, the C viewer and the writer's format cannot drift apart unnoticed.
