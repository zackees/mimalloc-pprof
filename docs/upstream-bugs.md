# Upstream mimalloc bugs found and fixed here

*Part of the [mimalloc-pprof](../README.md) documentation.*

This page exists because the bugs below are **defects in upstream
microsoft/mimalloc**, not in the profiler, and they affect anyone using mimalloc on
Windows/MinGW whether or not they use this fork.

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

## What this means for you

- **Using upstream mimalloc on Windows/MinGW?** Bug 1 applies to you and is worth
  carrying a patch for.
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
