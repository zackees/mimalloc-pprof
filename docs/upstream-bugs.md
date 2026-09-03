# Upstream mimalloc bugs found and fixed here

*Part of the [mimalloc-pprof](../README.md) documentation.*

This page exists because the bugs below are **defects in upstream
microsoft/mimalloc**, not in the profiler, and they affect anyone using mimalloc
whether or not they use this fork — bugs 1-3 on Windows/MinGW, bugs 4-5 on every
platform.

Every one was confirmed by building **stock upstream at the same commit with the
same toolchain** and reproducing it there with zero fork changes. Where a fix is
claimed, the measurement is given.

## Bug 1: thread-exit cleanup never runs on MinGW — unbounded memory leak

|  |  |
|---|---|
| **Affects** | upstream **v2 and v3**, Windows/MinGW (GCC) only — MSVC is unaffected |
| **Symptom** | every exiting thread leaks its thread-local heap and all its pages |
| **Status** | **fixed on both lines** — v3 in 0.9.0; v2 on the `v2` branch, not yet in a published 0.8.x |

Memory grew linearly and without bound — about **0.24 GB per iteration** of
`test-stress`, reaching **23.5 GB at 100 iterations**. The process still exited 0,
so it is invisible on a large-memory machine and only surfaces as an
out-of-memory failure on a smaller one.

**Root cause.** Windows' default init mode registers its loader TLS callbacks using
**MSVC-only pragmas** — `#pragma comment(linker, "/INCLUDE:...")` plus
`const_seg`/`data_seg` — which GCC silently ignores. The `.CRT$XL*` entries are
never emitted, so `DLL_THREAD_DETACH` never fires and `_mi_thread_done` never runs.

**The two lines need different fixes.** This is the non-obvious part, and matters if
you are patching upstream yourself:

- **v3** — register the callbacks with GCC section attributes plus a `_tls_used`
  reference. That is sufficient, because v3 keeps its thread state in its own TLS
  slots.
  *Peak RSS 34.64 GB → 0.02 GB; live `theaps` 1857 → 5.*
- **v2** — the same registration is **not sufficient**. v2's default heap lives in a
  `mi_decl_thread` variable, which GCC implements with **emutls** on MinGW, and
  emutls is torn down *before* any PE TLS callback runs. The callback then observes
  an already-empty heap and `_mi_thread_done` early-returns. v2 must use the **FLS**
  path instead, where the callback receives the stored value as an argument.
  *Peak RSS 23.50 GB → 0.28 GB.*

**Why it went unnoticed upstream:** upstream CI has **no MinGW job**, and the only
Windows init mode with a documented non-MSVC path is the deprecated FLS one.

## Bug 2: `mi_heap_new` / `mi_subproc_new` do not bootstrap the library

|  |  |
|---|---|
| **Affects** | upstream **v3** |
| **Symptom** | crash when either is the first mimalloc call in a process |
| **Status** | **fixed** in 0.9.0 |

Either function can be the first mimalloc call a process makes, but neither
initialized the library, so both allocated from a still-`NULL` `subproc->heap_main`.

It presented as Windows-only because on Linux and macOS a library constructor has
always run first. It is **not** debug-only: in a release build the assertions
compile out and the code proceeds to allocate from a NULL heap.

Upstream issue [#1341](https://github.com/microsoft/mimalloc/issues/1341)
(`free(NULL)` before initialization) is the same bug class.

## Bug 3: `test-stress.c` dereferences unchecked allocations

|  |  |
|---|---|
| **Affects** | upstream **v2 and v3** test suites |
| **Status** | **not fixed here** — upstream test code |

`data = custom_realloc(...)` and `mi_heap_new()` are both used without a NULL
check, so any allocation failure becomes an opaque segfault far from its cause.
This is what made bug 1 present as a mysterious crash rather than an obvious
out-of-memory.

## Bug 4: `mi_free_small` reads the user's block as a page when `MI_PADDING` is on

|  |  |
|---|---|
| **Affects** | upstream **v3** at `6def7be9` — the base this fork pins, which is also Bun's mimalloc merge-base. **Gone at `dev3` HEAD** (`34fbd7e7`), see *Upstream's own fix* below |
| **Symptom** | NULL-pointer SEGV in `mi_arenas_page_free_ex` on `free()` of a 1009..1024 byte block |
| **Status** | **fixed here** — one line in `src/arena.c` |

Reported as [issue #301](https://github.com/zackees/mimalloc-pprof/issues/301): a gcc
AddressSanitizer build of `test-api` crashed at `free_small1`. It is neither
gcc-specific nor ASan-specific — `RelWithDebInfo -DMI_PADDING=1` crashes identically
under gcc *and* clang, with no sanitizer involved.

Confirmed on **stock upstream `6def7be9`, zero fork changes**: `cmake -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_C_FLAGS=-DMI_PADDING=1` then `mimalloc-test-api` segfaults at `free_small1`.

**Root cause.** `MI_OPT_FREE_SMALL` makes `mi_free_small(p)` locate the page as
`_mi_align_down_ptr(p, MI_SMALL_PAGE_SIZE)` instead of consulting the page map, which
is only valid while the page metadata sits in front of the slice.
`mi_arenas_page_alloc_fresh` decided that with the raw `MI_SMALL_SIZE_MAX`:

<!-- doc-snippet: skip (source excerpt, not a program) -->
```c
if (block_size > MI_SMALL_SIZE_MAX) { /* meta goes in the separate pages_meta array */ }
```

But the *entry* condition for the small path is the **requested** size, not the block
size, and `MI_PADDING` makes the two diverge: `mi_good_size(1024) == 1280`. So a
1009..1024 byte request still took the small path, its page meta went to `pages_meta`,
and `mi_free_small` then read the caller's own (zeroed) block as an `mi_page_t` —
`mi_page_all_free()` on garbage, then a NULL `page->heap` dereference.

The correct bound is the one `mi_free_small`'s own assertion already states,
`mi_good_size(MI_SMALL_SIZE_MAX)`. Without padding that is exactly `MI_SMALL_SIZE_MAX`,
so the fix is a no-op in ordinary release builds.

**Why nobody hit it.** `MI_PADDING` is implied by `MI_TRACK_ASAN`, `MI_TRACK_VALGRIND`,
`MI_TRACK_ETW` and `MI_SECURE>=3`, and CMake auto-enables `MI_OPT_FREE_SMALL` **only
when `MI_DEBUG` is off**. The two therefore coexist only in a release-type sanitizer or
secure build — and upstream's (and this fork's) ASan job built `Debug`.

**Upstream's own fix.** At `dev3` HEAD (`34fbd7e7`) the option is relabelled
*"Deprecated"*, the release auto-enable is gone, and the aligned-down lookup is validated
against a new `page->self` pointer (`mi_ptr_page_is_valid_ex` in `free.c`). Stock
`34fbd7e7` passes `test-api` 50/50 with `-DMI_OPT_FREE_SMALL=ON -DMI_PADDING=1`, so there
is nothing to send upstream — but everything pinned at `6def7be9`, Bun included, still has
this.

## Bug 5: `mi_realloc` frees an interior pointer when the padding does not decode

|  |  |
|---|---|
| **Affects** | upstream **v3** at `6def7be9`, any `MI_PADDING` non-debug build. **Gone at `dev3` HEAD** (`34fbd7e7`) |
| **Symptom** | upstream's own `mi_urealloc_invalid` test fails; an interior pointer is pushed onto the page free list |
| **Status** | **fixed here** — `src/alloc.c` |

Found in the same configuration as bug 4, and independent of it. Confirmed on **stock
upstream `6def7be9`, zero fork changes**: the same `RelWithDebInfo -DMI_PADDING=1` build
reports `FAILED: mi_urealloc_invalid`. Stock `34fbd7e7` does not.

With `MI_PADDING` on, `mi_page_usable_size_of` returns **0** when the padding canary
does not decode — free.c says so itself: *"size can be zero if the padding is
corrupted"*. That means `p` is not a block start, or the block was overrun.
`mi_theap_realloc_zero_ex` took the 0 at face value, treated the block as zero bytes
long, allocated a replacement, copied nothing into it, and **freed `p`** — an interior
pointer onto the page's free list.

The fix returns `NULL` without freeing `p`, which is both the contract stated at the top
of that function (*"returning NULL always indicates an error, and `p` will not have been
freed"*) and the answer a `MI_DEBUG` build already gives via `mi_validate_ptr_page`.
No valid block ever reports usable size 0 — the padding path bumps a zero-byte request
to `sizeof(void*)` — verified by exhaustively probing `mi_malloc`/`mi_zalloc`/
`mi_calloc`/`mi_realloc` over 0..4096 bytes and `mi_*_aligned`/`mi_malloc_aligned_at`
over 0..2048 bytes × alignments 1..8192, in padding and non-padding, debug and release
builds: no allocation reports 0.

## What this means for you

- **Using upstream mimalloc on Windows/MinGW?** Bug 1 applies to you and is worth
  carrying a patch for.
- **Pinned to upstream v3 around `6def7be9`** (as Bun is) **and building with a
  sanitizer, Valgrind, ETW tracking or `MI_SECURE>=3` in a release build?** Bugs 4 and 5
  apply to you. Upstream `dev3` HEAD has moved past both.
- **Using this fork?** All of the above are handled — v3 on `main`, and the v2 line
  carries its own variant of the bug 1 fix.

## How this is kept fixed

Both leaks passed the entire existing test suite, because every test only asked
*"did it crash?"* and never *"did memory stay bounded?"*.

[`test/test-degenerate.c`](../test/test-degenerate.c) closes that gap. It creates and
joins 184 threads and asserts the engine's live `threads` counter comes back down
and RSS has not climbed. It is verified in **both** directions — with the fix
reverted it fails with `threads.current=184`; with the fix in place it reads `1`.
A regression test that has never been observed to fail proves nothing.

It also drives patterns the stress tests do not: sawtooth, fragmentation-then-large,
a full size-class sweep with ±1 boundary probes, realloc ping-pong across the
small/large boundary, huge-allocation churn, and degenerate arguments
(zero-size, `free(NULL)`, alignments, `SIZE_MAX`, `calloc` overflow).

The full set of regression and correctness gates that guard these fixes on every PR
is documented in [CI gates](ci-gates.md).
