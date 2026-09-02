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

## Upstream adoption status (verified 2026-08-02)

Checked against `upstream/main`, `upstream/dev` and `upstream/dev3` directly rather
than from memory. Two of the three fixes this document proposes are **already upstream**;
one is filed; and one new defect was found in the process.

**v2 thread-exit (FLS) — ADOPTED, with attribution.** `upstream/main` and `upstream/dev`
both carry it at `src/prim/windows/prim.c:764`:

<!-- doc-snippet: skip (patch excerpt, not a program) -->
```c
#if defined(__GNUC__) && !defined(_MSC_VER)  /* mingw */
  #define MI_WIN_INIT_USE_FLS  1  /* needed for v1/v2, see <https://github.com/zackees/mimalloc-pprof/pull/48> */
```

So the "report the v2 fix upstream" task is complete — it was adopted before we got
round to filing it. No PR needed; do not re-send it.

**v3 thread-exit — adopted, then broken by a typo.** `60c4f031` took it and credits
[#56](https://github.com/zackees/mimalloc-pprof/issues/56), but guards on `__GCC__`,
which no compiler defines. Filed as
[microsoft/mimalloc#1349](https://github.com/microsoft/mimalloc/pull/1349) with a 44×
thread-churn measurement.

**The `__GCC__` typo is NOT dev3-only.** It is also present on `upstream/main` and
`upstream/dev` at lines ~879 and ~976, in the `MI_WIN_INIT_USE_*` blocks. On the v2 line
the practical impact is smaller — MinGW now takes the FLS path above, so those branches
are not reached — but they are still dead code that would silently do nothing for anyone
who forces `MI_WIN_INIT_USE_CRT_TLS`, and the same one-token fix applies. Worth folding
into #1349's discussion rather than opening a second PR, since it is the same defect and
the same reviewer.

**MinGW CI job — still not proposed, and now known to be urgent.** Upstream has no MinGW
coverage, which is the root cause of all of the above: nobody there builds the
configuration in which these break.

Measured what such a job would actually report, by building `upstream/dev3` tip
(`1f06f694`) unmodified with MinGW-w64 GCC 12.2, `-DCMAKE_BUILD_TYPE=Debug
-DMI_DEBUG_FULL=ON` — the same configuration upstream's own `test.yaml` runs for its
`basic` matrix entry:

```
67% tests passed, 2 tests failed out of 6

The following tests FAILED:
   3 - test-stress-heaps     (Exit code 0xc0000409)
   4 - test-stress-subprocs  (Exit code 0xc0000409)
```

Configure and build are clean; the failures are at runtime. `0xc0000409` is Windows
fail-fast (`STATUS_STACK_BUFFER_OVERRUN`), i.e. a hard abort rather than an assertion.

This matters for how the CI proposal is framed. A MinGW job added to upstream today
would be **red on arrival**, so it cannot be pitched as "add coverage for completeness" —
it has to go in together with, or after, the fixes. It is also independent corroboration
that the `mi_heap_new` / `mi_subproc_new` bootstrap fix above is still needed upstream:
those are exactly the two tests it addresses, and they are exactly the two that fail.

Order to propose in: bootstrap fix first, then the MinGW job, so the job lands green.

**Bootstrap fix: FILED as [microsoft/mimalloc#1351](https://github.com/microsoft/mimalloc/pull/1351)**
(2026-08-02), cut from `upstream/dev3` tip. Two `mi_thread_init()` calls, in
`mi_heap_new_in_arena` and `mi_subproc_new`. Verified on that tree in upstream's own
`basic` configuration: **2/6 failing before, 6/6 after**. The PR also flags #1349 and
offers the MinGW job as a follow-up, explicitly noting it would be red today and so
should land after the fixes.

**MinGW CI job: still unfiled**, and now correctly sequenced behind #1351.

**Update (2026-09-01, #266 pin bump):** the `__GCC__`/`__GNUC__` detection typo described above is fixed upstream as of our new pin (`6def7be9`). It was still present at `1f06f694` (the tip snapshot measured above) but upstream landed
`4cca633e`/`b5fdee4a` ("fix mingw detection: __GCC__ -> __GNUC__") and then
`1cf88691` ("use __MINGW32__ to detect mingw ... instead of __GNUC__") somewhere
in `1f06f694..6def7be9`. `src/prim/windows/prim.c` at `6def7be9` already reads
`#elif defined(__MINGW32__)` in all three `MI_WIN_INIT_USE_*` blocks -- a real,
correct macro (defined by MinGW-w64 GCC and by clang targeting `*-w64-mingw32`),
not the old typo. We do not carry a patch for this anymore; our overlay took
upstream's file verbatim at the bump (#266). The historical measurements above
(against `1f06f694`) remain accurate for their time and are left as-is for the
record; they no longer describe our current pin.

**New upstream bug found by the same `1cf88691` narrowing (2026-09-02, #266 round 2):
`_mi_auto_process_init` (src/init.c) runs twice on win-gnu, not zero times.** Its own
comment says "Called once by the process loader ... before main is called", and
`mi_process_init()` inside it is do-once guarded -- but nothing else in the function
was. Root cause: `MI_PRIM_HAS_PROCESS_ATTACH` (src/prim/windows/prim.c) is defined only
for the `MI_WIN_INIT_USE_TLS_DLLMAIN`/`MI_SHARED_LIB` paths; the plain `__MINGW32__`
TLS-callback path `1cf88691` switched MinGW onto does not define it. Without that
macro, `src/prim/prim.c`'s `MI_PRIM_HAS_PROCESS_ATTACH`-gated code (~line 40) still
falls through to a GCC `__attribute__((constructor))` that ALSO calls
`_mi_auto_process_init` -- so on win-gnu the function runs once via the registered
`mi_tls_attach` TLS callback's `DLL_PROCESS_ATTACH` and once via the constructor.
Observed as `mi_add_stderr_output`'s `mi_out_default == NULL` assertion firing on every
win-gnu `MI_DEBUG_FULL` test (C ctest-debug-full-win-gnu) and on rust-native's
win-gnu Rust test (bench-harness's `planted_control`) -- same assertion, same file, two
unrelated consumers, confirming it's a real double-invocation and not test-specific.
At `bcee5a88` (our previous pin) MinGW used `MI_WIN_INIT_USE_FLS`, which DOES define
`MI_PRIM_HAS_PROCESS_ATTACH`, so this was unreachable before the bump.
Fixed on our side by wrapping `_mi_auto_process_init`'s body in `mi_atomic_do_once`
(defense in depth: `mi_add_stderr_output` was also made idempotent). Not reproducible
locally (no MinGW cross-compiler in this environment) to directly confirm the two-call
path; reasoned from source and the two independent consumers hitting the identical
assertion. **`pr/*` candidate**: either define `MI_PRIM_HAS_PROCESS_ATTACH` for the
plain-`__MINGW32__` TLS-callback path too (matching what `MI_WIN_INIT_USE_FLS` did), or
wrap `_mi_auto_process_init`'s body in a do-once guard upstream, the way ours now is.
Needs a MinGW box to verify before filing.

## Posture on upstream PR #1266 (competing profiler)

Resolves #128 F2. Re-check the dates below before cutting a `pr/*` branch that touches
profiler files; everything else here is stable.

**What it is.** [microsoft/mimalloc#1266](https://github.com/microsoft/mimalloc/pull/1266),
*"feat: enable memory profiling"* by Daniel Schwartz-Narbonne (Datadog), opened
2026-04-16. It adds `include/mimalloc/profile.h`, `src/profile.c`, and
`test/test-profile.c` — **our exact three filenames** — plus alloc/free hooks and
sampled records hung off `page->metadata` behind a page flag. Our technique, arrived at
independently.

**Status: never triaged, not rejected.** #128 asked whether it was stalled, rejected, or
awaiting revision. It is none of those:

- The only comment on the PR itself is the CLA bot. Zero review comments.
- On the parent issue [#1070](https://github.com/microsoft/mimalloc/issues/1070) the
  author asked daanx directly twice — 2026-04-21 (*"ready for review"*) and 2026-05-11
  (*"is the approach ... one you'd be interested in exploring?"*). Neither was answered.
- daanx's last word on profiling anywhere in that thread is **2025-06-14**.

So there is no upstream decision to react to, and no evidence of one coming soon. Do not
plan around #1266 landing.

**The collision is smaller than it looks.** #1266 targets `dev-main` and modifies
`src/segment.c`, which does not exist on v3. It cannot merge to the v3 line as written.
Our `pr/*` branches are cut from `dev3`. A file-level collision only materialises if
upstream merges it on v2 *and* someone ports it forward.

**Two constraints worth honouring anyway**, because they are what upstream and the
requesters actually said:

1. *daanx, 2025-06-14:* there is already tracing support via `mimalloc-track.h`
   (valgrind/asan) and Windows perf counters — *"If we add USDT it would be good if we
   could reuse the same mechanism? Or at least impact the code as little as possible."*
   Our hooks are `#if MI_PPROF`-guarded and a few lines per site (repo rule 6). **Lead
   with the diff size**, not the feature list.
2. *brancz, 2025-06-15:* the ask is sampling **inside** the allocator every X bytes, so
   the profiler need not unwind on every allocation. That is exactly what we do, and it
   is the requirement neither USDT draft satisfies.

**Decision: differentiate, do not coordinate.** Three approaches now exist — #1266
(in-process callbacks, v2), lucab's USDT probes
([lucab/mimalloc#1](https://github.com/lucab/mimalloc/pull/1), explicitly abandoned by
its author on 2026-04-23), and ours. Ours is the only one that is v3-native, sampled
in-allocator, and works on Windows — and #110 established Windows as the real
differentiator, since every alternative here is Linux/eBPF-shaped. Upstreaming stays
focused on the **bug fixes** above, which are uncontested and reviewable on their own.
Profiler upstreaming is a separate, later conversation.

**Do not rename our files defensively.** Renaming to dodge a collision that may never
happen costs us the fork's own history and every issue reference. If #1266 ever lands on
a line we target, rename then.

We are already visible in the thread: zackees posted the fork on #1070 on 2026-07-29.
The convergence itself is the signal worth remembering — Datadog, Bun, and this fork
independently reached `page->metadata` plus a flag.

## Candidate: mingw links `mimalloc.dll` last in the import table (#277 phase C)

Not yet submitted; recorded here with the measurement so it does not have to be
rediscovered.

**Symptom.** On MinGW, `mimalloc-test-stress-dynamic.exe` imports `mimalloc.dll` *last*.
The Windows loader initialises statically imported modules in import-table order, so
`mimalloc-redirect.dll` — which `mimalloc.dll` pulls in, and which imports only
`ntdll.dll` — runs after the CRT it is meant to patch, and `mi_is_redirected()` stays
false. `MIMALLOC_VERBOSE=1` prints no `malloc is redirected.` line. Upstream knows the
symptom: the `MI_MINGW_UCRT64` branch of `CMakeLists.txt` says "mingw always links
mimalloc after system libraries" and post-processes the exe with `bin/minject.exe`.

**Cause.** `ld`'s PE linker script emits the import descriptors under
`SORT(*)(.idata$2)`, sorted by the **input file path as spelled on the link line** —
archive path first, member name only as a tie-break within one archive. CMake names the
import library relative to the build directory (`libmimalloc.dll.a`) while every CRT
archive arrives as an absolute sysroot path, and `/` (0x2f) sorts before `l`. Linking the
same object against **byte-identical** archives, changing only the spelling:

```
libmimalloc.dll.a     ->  KERNEL32.dll, api-ms-win-crt-*.dll, ..., mimalloc.dll
./libmimalloc.dll.a   ->  mimalloc.dll, KERNEL32.dll, api-ms-win-crt-*.dll, ...
```

This is also the real explanation for the msvcrt/UCRT split: under msvcrt the CRT is
`libmsvcrt.a` and `mimalloc.dll` happens to sort ahead of `msvcrt.dll`; under UCRT it is
`api-ms-win-crt-*`, which sorts earlier. The CRT only changes what mimalloc is sorted
against.

**Fix we carry** (`CMakeLists.txt`, one `if(MINGW)` block on the
`mimalloc-test-stress-dynamic` target): spell the same file `./…`, in the libraries
position, via `$<TARGET_LINKER_FILE_NAME:mimalloc>`. Verified cross-built (soldr
mingw-w64-gcc 15.3.0) for Release, Debug + `MI_DEBUG_FULL` and the shared-only config:
`mimalloc.dll` / `mimalloc-debug.dll` is import #0 in all three, exactly one descriptor.
It is what upstream's own "try to link with the mimalloc library earlier on the command
line" hint asks for, it applies to the native MSYS2 lane too, and it needs no `minject`.
Upstreamable as-is; it touches one guarded block and no C.

**It is necessary but not sufficient — the other half is inside `mimalloc.dll`.** The
loader initialises a module's dependencies in *that module's* import-descriptor order, so
`mimalloc.dll` must import `mimalloc-redirect.dll` before its own `api-ms-win-crt-*`, or
`ucrtbase.dll`'s DllMain has already run by the time the redirection module gets control.
It then refuses outright: `LdrGetDllHandle("ucrtbase.dll")` followed by a test of
`LDR_DATA_TABLE_ENTRY.Flags & 0x00080000` (`LDRP_PROCESS_ATTACH_CALLED`), disassembled at
`0x180003720` in `bin/mimalloc-redirect.dll` v1.3.3, and it reports
`mimalloc-redirect.dll seems to be initialized after ucrtbase.dll` through the
`const char**` that `mi_allocator_init` hands back — the module imports no output API, so
that message is the only channel it has. MSVC produces the accepted layout by
construction, because link.exe emits descriptors for explicitly named libraries before the
`/DEFAULTLIB` ones. **Fix we carry:** `MI_MINGW_REDIRECT_FIRST` (default ON), the same
`./…` spelling applied to `bin/mimalloc-redirect.lib`.

**Second fix, and the one that is genuinely ours: GCC emulated TLS on the allocation
path.** With the layout above, the process died at load. soldr's conda-forge
`x86_64-w64-mingw32-gcc 15.3.0` has no native TLS — `__thread int x;` compiles to
`__emutls_v.x` plus a call to `__emutls_get_address`, and `__declspec(thread)` draws
"warning: 'thread' attribute directive ignored" and emits an ordinary global.
`__emutls_get_address` allocates its per-thread table with `malloc`, which with the
override live is `mi_malloc`, which reads the thread-local again: unbounded recursion, and
cdb on a Windows runner shows exactly that cycle with one
`libgcc_s_seh_1!__emutls_get_address` frame per turn. We move the two thread-locals
reachable from the allocator onto dynamic Win32 TLS keys, which never allocate:
`src/threadlocal.c` (`mi_thread_locals`, `mi_slot_fast`) and `src/options.c` (the
output-recursion guard, reached via `mi_malloc` -> arena reservation ->
`_mi_verbose_message` -> `_mi_fputs`), behind `MI_WIN_TLS_SLOTS` and
`_mi_prim_tls_key_*` in the prim layer. MSVC is untouched. Upstreamable: it is a real
defect on any `--disable-tls` mingw-w64, independent of this fork.

**`minject` produces a non-starting image on a mingw-linked exe — same defect, now
explained.** Facts from CI run 33609497360: `minject --verbose --postfix=<p>` reads the
exe, reports `inject 'mimalloc-redirect.dll'`, prints a correct reordering
(`mimalloc-redirect.dll` #0, `mimalloc*.dll` #1, then `KERNEL32.dll` and the
`api-ms-win-crt-*` set), writes the file, and `minject --list` on the result shows the
intended 13-entry table. The image then exits **127**. That is **not** a PE defect:
minject had built the correct layout, the override engaged, and the process then
stack-overflowed in the emulated-TLS recursion above, before `main`. Superseded by
`MI_MINGW_REDIRECT_FIRST`, which achieves the same import order at link time and works on
a cross build, where minject (a Windows PE utility) cannot run at all. Nothing to report
upstream about minject.

## Local reproduction

```sh
git worktree add /tmp/up3 pr/mingw-thread-exit-dev3
cd /tmp/up3
cmake -S . -B b -G Ninja && cmake --build b
ctest --test-dir b
./b/mimalloc-test-stress-heaps.exe 32 50 50
```

Compare against the same commands on stock `upstream/dev3`.
