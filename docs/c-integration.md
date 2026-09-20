# C and C++ integration

*Part of the [mimalloc-pprof](../README.md) documentation.*

## Build and install

```sh
cmake -S . -B build -DMI_PPROF=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo
cmake --install build --config RelWithDebInfo --prefix /path/to/prefix
```

**Every observability subsystem is opt-in and OFF by default** (#414). The default
`cmake -S . -B build` builds a plain fast allocator whose `mi_malloc`/`mi_free` are
byte-identical to upstream mimalloc. Name the ones you want:

| Option | Default | What it builds in |
|---|---|---|
| `MI_PPROF` | `OFF` | sampled pprof heap profiling (`include/mimalloc/profile.h`) |
| `MI_MEMEVT` | `OFF` | allocation-change accounting and callbacks (`include/mimalloc/memory-events.h`) |
| `MI_DIAGNOSTICS` | `OFF` | heap snapshot (`mi_heap_snapshot*`) + live-heap JSON dump (`mi_heap_dump_json*`) |
| `MI_DHAT` | `OFF` | the exact DHAT v2 observer (`include/mimalloc/dhat.h`) |
| `MI_OWNER_GATE` | `OFF` | `mi_purge_all` can sweep every thread, at a cost on the fast path |

The public functions of every one of them **remain linkable in every configuration**, as
no-op stubs that report the subsystem off (`mi_prof_start` / `mi_dhat_start` /
`mi_memory_tracking_set_enabled` return `false`, `mi_heap_dump_json` returns `NULL`,
`mi_heap_snapshot*` return `-1`), so a consumer needs no `#ifdef`. The `mi_unwrapped_*`
family and `mi_option_snapshot_on_exit` exist in every build too.

The same defaults apply to a build that never runs this CMake — a direct `src/static.c`
compile, e.g. Bun's — because `include/mimalloc/types.h`'s fallbacks match; define
`MI_PPROF=1` (or the others) on the command line to opt in there.

In a CMake consumer, link the `mimalloc` shared target or the `mimalloc-static`
static target exactly as with upstream mimalloc:

```cmake
add_subdirectory(path/to/mimalloc-pprof)
target_link_libraries(my_app PRIVATE mimalloc-static)
```

> **Do not link two mimalloc implementations into one process.** In particular, a
> Rust binary using the crate's vendored allocator must not also link the root
> CMake library.

## Reallocation, including the zeroing forms

Beyond `mi_realloc`, mimalloc provides variants that Rust's `GlobalAlloc` cannot express
and that are easy to miss:

| Function | What it does |
|---|---|
| `mi_rezalloc(p, n)` | grow/shrink **and zero the newly-exposed tail** |
| `mi_recalloc(p, count, size)` | same, in `calloc`'s element-count form |
| `mi_rezalloc_aligned`, `mi_recalloc_aligned` | aligned variants |
| `mi_expand(p, n)` | grow **in place only** — returns `NULL` rather than moving, leaving `p` valid |

The zeroing forms save a `memset` you would otherwise write by hand. One subtlety worth
knowing: they zero from the block's old **usable** size, not from the size you
originally requested. A block requested at 64 bytes may be usable to 80, so growing it
to 70 is served in place with nothing zeroed. If you need a specific range zeroed,
capture `mi_usable_size` before the call.

All of these are available from Rust as `mimalloc_pprof::{rezalloc, recalloc, expand,
usable_size}`.

## Scavenger and hole purging

Two independent, default-on mechanisms return memory to the OS without waiting for
the next allocation:

| Option | Env var | Default | What it does |
|---|---|---|---|
| `mi_option_scavenger` | `MIMALLOC_SCAVENGER` | `1` | Runs a background thread that purges arena memory on a timer (`mi_option_purge_delay`, `MIMALLOC_PURGE_DELAY`, 100 ms in this fork) instead of only on allocation. `mi_scavenger_stop()` stops it for good; it restarts on demand unless stopped. |
| `mi_option_purge_holes` | `MIMALLOC_PURGE_HOLES` | `1` | At each idle point, discards the free blocks **inside** a still-used page (in OS-page units), so one long-lived object no longer pins the whole page resident. |
| `mi_option_purge_holes_min_interval` | `MIMALLOC_PURGE_HOLES_MIN_INTERVAL` | `100` (ms) | Minimum time between two hole sweeps of the same thread's heaps. |
| `mi_option_purge_holes_eager_zero` | `MIMALLOC_PURGE_HOLES_EAGER_ZERO` | `0` | Debug/test knob: zero a range before discarding it, so a mis-scoped discard corrupts visibly rather than silently on an OS that reclaims lazily. Always on when `MI_DEBUG>1`. Makes discarding more expensive, not cheaper. |
| `mi_option_purge_holes_full_every` | `MIMALLOC_PURGE_HOLES_FULL_EVERY` | `64` | Every Nth hole sweep walks every page instead of skipping ones unchanged since the last sweep; `0` disables the periodic full walk. |

Both mechanisms run at the same idle point: `mi_on_thread_idle()`, called directly
or handed to the scavenger via `mi_on_thread_idle_start()` /
`mi_on_thread_idle_end()` around a blocking kernel call. Query what hole purging
has reclaimed with `mi_purge_holes_stats_get()` (a `mi_purge_holes_stats_t` of
running counters — bytes/blocks purged, discard and reuse syscalls, pages
skipped) or print a per-size-class breakdown of what could **not** be discarded,
and why, with `mi_purge_holes_report()`. See the
[README's measured chart](../README.md#hole-purging-measured)
for numbers, and `include/mimalloc.h` / `doc/mimalloc-doc.h` for the full API
and option reference. Neither mechanism costs the `mi_malloc`/`mi_free` fast
path anything — both are ported from
[oven-sh/mimalloc @ `942b8342`](https://github.com/oven-sh/mimalloc), MIT.

## Build flags for usable stacks

The profiler walks **your application's** frames, so the flags that matter are the ones
on **your** targets — not the ones this library builds itself with.

`MI_PPROF=ON` adds `-fno-omit-frame-pointer` to mimalloc's own translation units
(`CMakeLists.txt`), but that is `PRIVATE` and does not propagate to consumers. Enabling
the profiler does **not** make your code unwindable, and the symptom is truncated or
nonsensical stacks rather than an error:

```cmake
add_subdirectory(path/to/mimalloc-pprof)
target_link_libraries(my_app PRIVATE mimalloc-static)

# Linux/macOS: the profiler walks frame pointers, so your code must keep them.
if(NOT WIN32)
  target_compile_options(my_app PRIVATE -fno-omit-frame-pointer)
endif()
```

Or from the command line: `-fno-omit-frame-pointer` (plus `-g`, or
`-DCMAKE_BUILD_TYPE=RelWithDebInfo`, so the addresses resolve to names). Apply it to
every library you want to see in a profile, not just the top-level executable — an
optimised dependency built without it terminates the stack at its boundary.

**Windows is different, and `/Oy-` is not the answer.** Stack capture there uses the
unwind tables x64 emits regardless of frame-pointer settings; what you need is the
**PDB** next to the binary at analysis time. Keep it even for release builds — the ZIP
that ships with each GitHub release contains no symbols for your code.

The Rust equivalent is
[`-Cforce-frame-pointers=yes`](rust-integration.md#frame-pointers-and-symbols); the two
are the same requirement expressed in each toolchain.

## Full example

```c
#include <mimalloc.h>
#include <mimalloc/profile.h>

int main(void) {
  if (!mi_prof_start(0)) {  /* 0 uses env/default: about 512 KiB */
    return 1;
  }

  /* Run the workload whose live heap you want to inspect. */
  void* allocation = mi_malloc(1024 * 1024);

  if (!mi_prof_dump("heap.prof")) {
    mi_free(allocation);
    mi_prof_stop();
    return 2;
  }

  mi_free(allocation);
  mi_prof_stop();
  return 0;
}
```

`mi_prof_start_ex` provides structured configuration for the sample interval,
sampling seed, cumulative mode, stack depth, profiler-memory budget, and
exit-time dump. The complete versioned API contract is in
[`include/mimalloc/profile.h`](../include/mimalloc/profile.h).

For runtime configuration — environment variables, seeds, embedded-mimalloc
concerns — see the [profiler reference](profiler.md).
