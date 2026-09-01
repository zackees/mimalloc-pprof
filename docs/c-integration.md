# C and C++ integration

*Part of the [mimalloc-pprof](../README.md) documentation.*

## Build and install

```sh
cmake -S . -B build -DMI_PPROF=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo
cmake --install build --config RelWithDebInfo --prefix /path/to/prefix
```

Use `MI_PPROF=OFF` to omit the profiler implementation and its allocation hooks.
The public profiler functions remain linkable as no-op stubs, and the
memory-events API remains fully available.

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
