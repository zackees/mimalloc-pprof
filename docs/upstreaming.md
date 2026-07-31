# Upstreaming to microsoft/mimalloc

Three contributions are prepared and validated against current upstream. They are on
`pr/*` branches in this repository, each cut from the matching upstream branch and
containing **only** the upstream-relevant C diff — no profiler code, no fork-specific
files. That is what repo rule 2 (never mix C-core and `rust/` paths in one commit)
exists to make possible.

Tracked in [issue #56](https://github.com/zackees/mimalloc-pprof/issues/56).

## Status

| Branch | Cut from | Contents | Validated |
|---|---|---|---|
| `pr/mingw-thread-exit-dev3` | `upstream/dev3` | v3 TLS-callback registration + `mi_heap_new`/`mi_subproc_new` bootstrap | ctest 6/6; stress RSS bounded |
| `pr/mingw-thread-exit-main` | `upstream/main` | v2 FLS thread-exit fix | ctest 4/4; RSS flat |
| `pr/mingw-ci-dev3` | `upstream/dev3` | MinGW CI job + memory-bound assertion | detector verified both directions |

None have been submitted upstream. Opening the PRs is a deliberate human step —
see [Submitting](#submitting).

## Why these three

### 1. Thread-exit cleanup never runs on MinGW (v2 **and** v3)

Every exiting thread leaks its thread-local heap and all its pages. Memory grows
linearly and without bound — ~0.24 GB per `test-stress` iteration, 23.5 GB at 100
iterations. The process still exits 0, so it is invisible on a large-memory machine
and only surfaces as OOM on a smaller one.

`MI_WIN_INIT_USE_CRT_TLS` (the default) registers its loader TLS callbacks with
**MSVC-only pragmas** — `#pragma comment(linker, "/INCLUDE:...")` plus
`const_seg`/`data_seg` — which GCC silently ignores, so `.CRT$XL*` entries are never
emitted and `DLL_THREAD_DETACH` never fires.

**The two lines need different fixes.** This is the part most likely to trip up a
patch author, and it is called out explicitly in both commit messages:

- **v3** — register the callbacks with GCC section attributes plus a `_tls_used`
  reference. Sufficient, because v3 keeps thread state in its own TLS slots.
- **v2** — that is *not* sufficient. v2's default heap lives in a `mi_decl_thread`
  variable, which GCC implements with **emutls**, and emutls is torn down *before*
  any PE TLS callback runs. The callback then observes an already-empty heap and
  `_mi_thread_done` early-returns. v2 needs the **FLS** path, where the callback
  receives the stored value as an argument.

Verified directly rather than inferred: with GCC section attributes the v2 callback
fires with `default_heap != NULL` but `initialized == 0`, and moving it to the
earlier `.CRT$XLB` slot does not help.

### 2. `mi_heap_new` / `mi_subproc_new` do not bootstrap the library (v3)

Either can be the first mimalloc call in a process, but neither initializes it, so
both allocate from a still-`NULL` `subproc->heap_main`. Presents as Windows-only
because Linux/macOS always run a library constructor first. **Not debug-only** — in
release the assertions compile out and it proceeds to allocate from a NULL heap.

Same bug class as upstream [#1341](https://github.com/microsoft/mimalloc/issues/1341)
(`free(NULL)` before init), which is open.

### 3. The MinGW CI gap — the root cause

`grep -ci "mingw" .github/workflows/test.yaml` returns **0** upstream. MinGW is a real
deployment target (MSYS2 ships mimalloc 3.4.3 unpatched), and the Windows init path
has no non-MSVC branch at all.

Both bugs above are downstream symptoms of this. **This is the highest-value
contribution of the three** — it prevents the next one.

Because a thread-exit leak never crashes, a build-and-run job would not have caught
it, so the proposed job also asserts memory stays bounded: it runs `test-stress` at 15
and 60 iterations and fails if peak RSS grows more than 2.5x across a 4x increase in
work.

## Validation

Every claim above was checked by building **stock upstream at the same commit with the
same toolchain** and reproducing there with zero fork changes — the control that
separates "our bug" from "upstream bug". Windows/MinGW, GCC 12.2.0.

**Patched upstream `dev3`** (applies cleanly to current HEAD, past our pinned base):

```
ctest                       6/6
test-stress-heaps @25       0.66 GB
test-stress-heaps @50       0.74 GB     (bounded; stock fails outright)
```

**Patched upstream `main`** (v2):

```
ctest                       4/4
test-stress @12/25/50       0.18 / 0.21 / 0.29 GB
   stock, same workload     2.58 / 5.29 / 11.47 GB
```

**Proposed CI memory check**, verified in both directions:

```
patched            237 MB -> 324 MB   (1.4x)   passes
stock (leaking)   3689 MB -> 14739 MB (4.0x)   fails, as intended
```

A check that has never been observed to fail proves nothing, which is why the
leaking case was run explicitly.

## Submitting

These branches are cut from upstream history, so they can be pushed to a fork of
`microsoft/mimalloc` and opened as PRs directly. Suggested split:

1. **v3 thread-exit + bootstrap** → PR against `dev3`. Two logical fixes; could be
   split further if a reviewer prefers.
2. **v2 thread-exit (FLS)** → PR against `dev` or `main`, per upstream's stated
   preference for v1-targeted PRs.
3. **MinGW CI** → PR against `dev3`, referencing the other two as the motivation.

Lead with the measurements and the stock-upstream control results; they are what make
the reports self-contained.

## Local reproduction

```sh
git worktree add /tmp/up3 pr/mingw-thread-exit-dev3
cd /tmp/up3
cmake -S . -B b -G Ninja && cmake --build b
ctest --test-dir b
./b/mimalloc-test-stress-heaps.exe 32 50 50
```

Compare against the same commands on stock `upstream/dev3`.
