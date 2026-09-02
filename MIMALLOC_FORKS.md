# mimalloc forks: a survey

A catalog of forks and heavy patch sets of [microsoft/mimalloc](https://github.com/microsoft/mimalloc),
what each actually changes, and — in [pass 3](#pass-3-should-we-merge-it) — a 1–5 rating of
whether that change belongs in this repository's v3 line.

Compiled **2026-07-31** against upstream v3.4.3 / v2.4.1 / v1.9.11.

---

## How this was built, and how much to trust it

Six parallel agents covered disjoint slices — fork graph, language runtimes, applications,
security/research, distro packaging, platform ports — each required to mark claims **[V]**
(verified from a fetched source) or **[I]** (inferred).

**The method finding is worth as much as the results.** Text search barely worked. What
worked was enumerating the fork network and diffing it:

```sh
gh api repos/microsoft/mimalloc/forks --paginate                    # 1,146 forks
gh api repos/microsoft/mimalloc/compare/dev3...OWNER:REPO:BRANCH    # exact file deltas
gh api repos/microsoft/mimalloc/commits?sha=BRANCH                  # filter non-upstream authors
```

That sweep covered all 473 forks pushed since 2024-06-01 plus every older fork with ≥1 star,
and it is what separated real patch sets from mirrors. GitHub *code* search
(`mimalloc extension:patch`, `MI_MALLOC_VERSION language:C`) was the other high-yield tool.
Web search returned mostly upstream docs.

### Three traps this survey hit, recorded so the next reader doesn't

1. **`ahead_by` is not divergence.** GoldJohnKing's Arma 3 fork reports **822 commits ahead**
   of the matching upstream tag; the real diff is **9 files**. LuisaGroup reports 29 ahead, of
   which 25 are upstream merge commits. Always look at files changed and filter by author.
2. **`MI_MALLOC_VERSION` changed encoding width.** `212` is v2.1.2 (3-digit era); `30403` is
   v3.4.3 (5-digit era). The header comment "major + 2 digits minor + 2 digits patch" describes
   only the newer scheme, so a naive parser will misread every v1/v2-era vendored copy.
3. **Names lie.** Ruby's `ruby_mimalloc` is a **typo** for `ruby_mimmalloc` — "mim" as in
   *mimic*, an unrelated plain-malloc wrapper. `romange/mimalloc` (Dragonfly DB's author) has
   **zero own commits**, so Dragonfly is not a mimalloc forker. `mimalloc-safe`'s `GlobalAlloc`
   impl is byte-identical to the crate it forked.

### Coverage limits

~600 zero-star forks last pushed before 2024-06-01 were not individually compared. Twelve
forks returned no compare result (deleted or private default branch), all zero-star. Several
"no patches" verdicts for distros rest on commit history rather than byte-diffs against the
upstream tarball. There is no confirmed statement of *why* CPython remains on v2.1.2 — treat
the absence of a v3 plan as unverified, not as evidence of a decision.

---

## Tier 1 — substantial forks

### 1. Bun — `oven-sh/mimalloc` @ `bun-dev3-v2`

**At `942b8342` (2026-08-24; bumped into bun `main` 2026-08-25 via bun PR #40409): 137 commits
ahead of merge-base `6def7be9` (60 by non-Microsoft authors), 59 files, +11,326/−404, 3 fork-new
source files (`prof.c`, `scavenger.c`, `heap-snapshot.c`; `subproc.c` is upstream), 17 new test
files, v3.5.0 line.** The most divergent living fork by a wide margin, and the only one tracking
v3 that changes allocator behavior rather than build glue.

| Change | What it does |
|---|---|
| `src/scavenger.c` (+407) | Demand-driven **background purge thread**. Waits on a `subproc->scavenger_wake` futex word set by `mi_arena_schedule_purge`, so freed arena memory returns to the OS without waiting for the next allocation. Platform-native wait/wake: `futex` / `__ulock` / `WaitOnAddress` / pthread condvar. Blocks all signals, coalesces wakes on the 0→set edge, 30 s safety-net timeout, `mi_scavenger_stop()` exported. A parked thread can hand its heaps to it. |
| `src/prof.c` (+719) | **pprof-compatible sampling heap profiler.** Zero fast-path cost when off; when on, `theap->prof_force_slow` poisons `pages_free_direct` so every malloc detours into `_mi_malloc_generic` where the byte countdown lives, and sampled blocks set a `MI_PAGE_HAS_PROF_SAMPLES` page flag so frees route to `_mi_prof_free`. Frame-pointer unwinding rather than `.eh_frame`; `build_id` in Mapping records; `<execinfo.h>` guarded behind glibc/Apple because musl has no `backtrace()`. |
| `src/heap-snapshot.c` (+434), `tools/mi-heapview.c` (+781) | Live heap snapshot + CLI viewer, `mi_heap_dump_json`, `mi_stats_get_json`. |
| `src/page.c` (+1038/−3) | **Hole purging** — reclaims free runs *inside still-used pages*, tracked by a bitmap keyed on OS page rather than block. Options: `purge_holes`, `purge_holes_eager_zero`, `purge_holes_min_interval`, `purge_holes_full_every`. |
| Zero-tracking | `zalloc` **skips its `memset`** when a purge left the range reading back zero. |
| `MI_NO_PROCESS_DETACH` | Opt out of the exit-time destructor entirely. |
| macOS | Fork-safe locking, fixed TLS slots → 96/97 (the never-assigned gap after `__PTK_FRAMEWORK_CORETEXT_KEY0`=95; ours are 108/109), in- and out-of-process malloc-zone introspection via `memory_reader_t`. |
| v3 race fixes | `mi_theap_merge_stats` guarded against concurrent `_mi_theap_free`; arena bits and page-map cleared on alloc-fresh failure paths. |
| Test suite | `test-purge-holes.c` (+1665), `test-prof-adversarial.c`, `test-heap-mt.c`, `test-fork-user-heap.c`, `test-heap-delete-race.c`, `test-commit-fail.c`, `test-heap-teardown.c` (571), `test-park-handoff.c` (560), `test-heap-churn.c` (461), `test-heap-aba.c` (129), `test-emulated-tls.c` (184), `test-thp-optout.c` (107), `test-free-before-init.c` (56), `test-theap-sentinel.c` (227), `test-purge-zero.c` (79), `test-snapshot.c` (79), `test-prof.c` (62), plus `MI_DEBUG` fault-injection hooks (`mi_debug_fail_os_commit_after`). |

**Direct prior art for this repository.** Bun independently built a pprof sampling profiler on
the same `dev3` base and independently arrived at our core invariant — profiler memory comes
from the raw OS layer, never hooked paths. It chose a *different* hot-path strategy
(`pages_free_direct` poisoning vs. our guarded inline checks) and a fixed byte-rate sampler,
noting in-source that Go and jemalloc use a geometric draw to avoid periodic-pattern bias —
which this fork mitigates with a uniform-jitter draw of mean `rate` (`src/profile.c:193`),
though not the exponential draw Go and jemalloc use.

### 2. CPython — `python/cpython`, `Objects/mimalloc/`

Vendored **v2.1.2**, not a GitHub fork. The most consequential modified mimalloc after Bun's,
and the reason free-threaded Python works at all. Divergence tracked in
[cpython#113141](https://github.com/python/cpython/issues/113141).

- **Heap and page tagging** — a 4-bit `tag` on `mi_page_t`, 8-bit on `mi_heap_t`, so the GC can
  find all GC-enabled objects by walking mimalloc heaps via `mi_heap_visit_blocks` instead of
  the non-thread-safe `_gc_next`/`_gc_prev` lists. Critical "when threads exit and pages become
  abandoned — the garbage collector still needs to identify pages containing GC-enabled objects."
- **QSBR** (quiescent-state-based reclamation) — `qsbr_node`/`qsbr_goal` fields on `mi_page_t`
  delay reuse of pages holding `PyObject`s so lock-free readers can still dereference them.
- Separate heaps per `PyThreadState` (PyMem, non-GC, GC-with-dict, GC-without), per-interpreter
  isolation of abandoned segments, UWP support, ~9 platform fixes.
- `MI_PRIM_THREAD_ID() = _PyThread_Id()` — **the one hook that was upstreamed**, and CPython is
  the *only* project in this entire survey that sets it.

### 3. GoldJohnKing — Arma 3 custom allocator (**139★, most-starred fork**)

28 branches, one per upstream release (`Arma-3-v1.7.3` … `Arma-3-v3.3.1`). `main` is a clean
mirror, which is why a naive `main` diff shows nothing.

The real diff is **9 files**: `src/cma/` implements Bohemia Interactive's Custom Memory
Allocator DLL ABI over `mi_malloc`/`mi_malloc_aligned`/`mi_free`/`mi_usable_size`, plus a
three-line hook in `mi_process_init_once`. The substance is three tuning defaults:

```c
mi_option_set_default(mi_option_arena_eager_commit,   1);
mi_option_set_default(mi_option_allow_large_os_pages, 1);
mi_option_set_default(mi_option_purge_delay,         -1);  // never return memory to the OS
```

`set_default` rather than `set`, so environment variables still win. Claims up to 30% higher
FPS than Arma's stock tbbmalloc, largest with "Lock Pages in Memory" granted.

**Structurally this is the cleanest template for a minimal rebasable fork** — all new code in a
dedicated `src/<feature>/` directory, one tiny hook, a branch per upstream tag. That is the same
discipline this repo's rule 6 mandates.

### 4. Dolt — `dolthub/mimalloc` + `dolthub/musl`

Two coordinated forks implementing `MI_MUSL_BUILTIN`: mimalloc's per-thread state is moved
*into musl's `struct pthread`*. musl gains `__mimalloc_default_heap_location()` and
`__mimalloc_recurse_location()`; `__libc_start_main` and the dynamic linker call
`_mi_process_init` directly; thread id comes from `__get_tp()`.

This dissolves the TLS-model and recursion problems of overriding malloc on musl rather than
working around them — the most structurally interesting idea in the survey, and inherently a
libc-side change.

### 5. CyberAgentGameEntertainment — Unity backend

Only 4 commits, but ~800 lines of new platform code: **`src/prim/unity/prim.c` (+528)** retargets
mimalloc's OS layer onto Unity's `IUnityMemoryManager` instead of mmap/VirtualAlloc, with a new
`MI_PRIM` dispatch case.

**The only fork in the entire 1,146-fork network that adds a new platform primitive layer.**

### 6. Malterlib — host-controlled allocator lifetime

7 fork-original commits: expose `mi_process_done`, **disable auto-initialization** so the host
build system owns allocator lifetime, a separate TLS override mechanism, **raw syscalls for
clocks on Linux** (bypassing libc `clock_gettime`), and a periodic-collection driver.

### 7. MaskRay — `mimalloc-lite`

One squashed commit, **≈ −5,000 lines**, by LLVM/lld's maintainer. Linux + GCC/Clang only;
removes macOS/Windows/WASI backends, `MI_PADDING`, `MI_SECURE`, `MI_ENCODE_FREELIST`,
`MI_DEBUG>2`, C++ `new`/`delete` interceptors, `mi_register_deferred_free`, and `src/libc.c`;
replaces a 210-line CMakeLists with a 6-line Makefile. A statement about how much of mimalloc is
optional, more than a maintained fork.

### 8. Others of substance

| Fork | Change |
|---|---|
| `WeiguangTWK/android_external_mimalloc` | AOSP/bionic integration — Soong `Android.bp`, bionic `malloc_info` XML and `malloc_iterate` implemented over `mi_heap_visit_blocks`, documented as replacing/augmenting Scudo. |
| `NikoMalik/mimalloc` | Freestanding Linux: raw syscalls, no libc, own `strtol`/`strstr`/`memcpy`, **spinlocks instead of pthread mutexes**. Stalled since 2025-11; diff inflated by a reformat pass. |
| `monographdb/mimalloc` | Exports **`mi_heap_page_utilization()`** — a page fill-ratio query to drive a DB engine's memory-pressure decisions. Abandoned, 1331 commits behind. |
| `jevinskie/mimalloc` | macOS/XNU custom pthread TSD slot, Mach-O aliases via inline asm. Abandoned WIP — commit messages read "i have no idea what im doing". Technique reference only. |
| `unikraft/lib-mimalloc` | The only bare-metal-ish port. Targets **v1.6.1**, *pre-`src/prim/` entirely*. Comments out `_mi_options_init()` so nothing allocates during early boot; treats the heap like WASM's — non-shrinkable, commit/reset/mprotect disabled. Unmaintained since 2021; a GSoC'24 revival stalled on a GPF in `pthread_join`. |

---

## Tier 2 — heavy integrations that configure rather than patch

These matter because they show how mimalloc is actually *tuned* in production.

| Project | Finding |
|---|---|
| **Capcom RE ENGINE** | mimalloc **2.0.3**. Perf-critical game-loop threads get **separate mimalloc heaps**; low-priority worker/middleware threads are collapsed into one logical thread with locks, trading execution speed for address-space efficiency. |
| **Meilisearch** | Moved to **v3 and enabled override mode** because they statically link LMDB, whose C code calls the system allocator; under v2 the two allocators didn't cooperate and **LMDB pages leaked** — "resolved by unifying them." Measured +13%/−9% mixed, materially lower steady RSS. **Disabled on Windows** (underperformed). |
| **Apache Arrow** | Makes mimalloc the **default memory pool** since GH-43254 (2024-07-16), displacing jemalloc. The R docs still say "jemalloc (used by default)" — stale. Disables mimalloc on R-devel Linux for "spurious sanitizer failures". |
| **psqlODBC** | Official PostgreSQL ODBC driver merged mimalloc as an optional submodule. Motivation quoted: lock contention in a multithreaded Windows app; "running on thousands of our production deployments for 9 months without issue." |
| **Emscripten** | Vendors **v3.4.1**; `-sMALLOC=mimalloc`. dlmalloc stays default because mimalloc "comes at a cost in code size and memory usage". |
| **Unreal Engine** | `FMallocMimalloc`, unmodified. Live bug: 16-byte alignment violations in UE 5.6 (UE-231770). |
| **Rust crates** | `libmimalloc-sys` now defaults to **v3** with `v2` opt-in — the polarity flipped. Rspack pins `local_dynamic_tls` on Linux citing upstream #147. **napi-rs does not default to mimalloc**; it is per-project opt-in. |

---

## Tier 3 — security and research forks

All three hardening forks are research artifacts, and **all predate v3**.

| Fork | Base | Technique | Measured cost |
|---|---|---|---|
| **xTag / mtmalloc** (RUB, EuroS&P'22) | v1.6.1 | Software pointer tagging without MTE: one physical page mapped at many virtual aliases via `MAP_SHARED`, so a 4-bit tag fits in pointer bits. Exports `heap_start`/`shadow_base` for an LLVM LTO pass; tag re-rolled on free ⇒ UAF detection. | **24.9%** runtime (SPEC CPU2017 intspeed) vs. unmodified mimalloc. Memory 6% expected, 60–70% observed on some benchmarks. |
| **MetaSafe** (USENIX Sec'24) | v2 | All allocator metadata behind an **MPK-gated region**; multi-pool heap; **per-object liveness bits in page metadata** giving O(1) `isLive`. | 25.5% on Rust microbenchmarks, **3.5% on Servo**. Memory +27%, but **8.3× under tokio**. |
| **TRust** (USENIX Sec'23) | v1.7.0 | mimalloc as the allocator for an MPK-protected "unsafe region"; all pages sourced from that region. Chose mimalloc partly for a smaller TCB (7k vs jemalloc's 24.5k SLoC). | 7.55% runtime, +13% memory. |
| **rimalloc** | v2.3.2 | Full Rust rewrite with **Lean proofs of the allocator's arithmetic invariants**, asserted by debug validators inside a libFuzzer target; `loom` concurrency models; Miri-clean. `secure` feature reproduces `MI_SECURE=3/4`. | ~5% for the secure feature. |

### Verified negatives — things that do not exist

- **No ARM MTE fork.** Upstream issue #817 closed as not planned; no PR, no fork.
- **No CHERI port.** The ISMM 2023 "Picking a CHERI Allocator" paper does not evaluate mimalloc;
  it appears only because five benchmarks came from *mimalloc-bench*.
- **No TSAN/UBSAN/USDT fork**; upstream already ships `MI_TRACK_VALGRIND`, `MI_TRACK_ASAN`, and
  Windows ETW with a WPR profile.
- **No independent heap profiler** beyond Bun's, Datadog's PR #1266 (which is v2-only — it
  touches `src/segment.c` and does not apply to v3), and this repository.
- **No CVE against the mimalloc C core**, and no public heap-exploitation writeup.
- **Not mimalloc users**, contrary to common assumption: ClickHouse, DuckDB, Redis, Valkey,
  TiKV, Qdrant (jemalloc); MongoDB (TCMalloc); ScyllaDB (Seastar's own); Envoy (tcmalloc);
  Node, Deno, V8, Erlang/OTP, Perl, R core, .NET, Java, Zig.
- **Structurally blocked**, not merely unadopted: **PHP** (ZendMM bypasses `malloc`, and
  `RTLD_DEEPBIND` breaks preloaded allocators) and **LuaJIT** (non-GC64 packs GC pointers into
  32 bits; mimalloc gives no low-address guarantee).

### The one open security analysis

[Upstream #372](https://github.com/microsoft/mimalloc/issues/372) (KAIST, open since Feb 2021):
`_mi_page_free` does not clear free-list metadata when reclaiming a page, so a later `malloc`
can return memory containing the encoded free list; and huge allocations land at predictable
addresses with `MAP_NORESERVE` always set, making allocation success an address-space oracle.
No fork fixes either.

---

## Platform ports and portability

**The prim layer is not the hard part.** There are exactly five backends — `windows`, `unix`,
`osx`, `wasi`, `emscripten` — and `unix/prim.c` also serves Linux, Android, every BSD,
Solaris/illumos, Haiku, QNX, Cygwin and musl. There is no pluggable hook; porting means adding
an `#elif`. The merged QNX port was **+8 lines in one file**.

**TLS bootstrap recursion is the actual wall.** QNX ([#309](https://github.com/microsoft/mimalloc/issues/309))
has been open six years and iOS ([#263](https://github.com/microsoft/mimalloc/issues/263)) likewise,
both the same shape: TLS initialization allocates. v3's four selectable TLS models in
`prim-tls.h` are the response, but the platform still has to choose correctly — and macOS
churned 108/109 → 126/127 → **reverted to 108/109 in v3.4.3**.

**v3's page map is the new portability risk, and it fails asymmetrically.** `MI_MAX_VABITS` is
compile-time everywhere except Windows and RISC-V. LA57 and ARM 52-bit VA are deliberately
clamped away — harmless. Every real bug landed in the opposite direction, on platforms with
*fewer* VA bits than assumed:

- **[#1087](https://github.com/microsoft/mimalloc/issues/1087) — the headline unfixed v3 bug.**
  100% reproducible access violation in `mi_page_map_set_range()` at `page-map.c:264` on Windows
  when High-Entropy ASLR is disabled, i.e. whenever the kernel returns a page below 2 GB. Open
  since May 2025, **no maintainer response**.
- RISC-V SV39 has only 256 GiB of user VA against a hard-coded 2 TiB aligned hint (#939); RISC-V
  is now the only architecture with **runtime** VA detection (`riscv_hwprobe`).
- `os.c:133` silently disables aligned-hint allocation entirely below 46 VA bits.
- **32-bit is the weakest area**: i686 + `MI_SECURE` segfaults in `test-stress` (#1152, open, no
  maintainer response); ARM32 Android saw 2.5× more allocation-failure crashes from 2.1.2 → 2.2.2
  (#1048, open); mimalloc costs ~20–30% of a 32-bit process's usable VA (#996).
- **Big-endian is a commented-out `#define`** in `bits.h`, referenced in exactly one place. No CI,
  no auto-detection. Treat as unimplemented rather than supported-but-buggy.

---

## Distro packaging

Only **four** ecosystems carry real source divergence; the rest ship pristine upstream and
diverge only in build flags.

| Ecosystem | Divergence |
|---|---|
| **Arch** 3.4.3 | Cherry-picks `b2226c19` for upstream #1341 — `_mi_checked_ptr_page()` dereferences `_mi_page_map` with no NULL check, and glibc 2.44's `__newlocale` calls `free(NULL)` during static init. **Release builds only.** Arch shipped ahead of upstream. |
| **Alpine** | `cmake-add-insecure-suffix.patch` — Alpine makes **`MI_SECURE=ON` the default** `libmimalloc.so`. **Excludes ppc64le and riscv64 from mimalloc3** because `test-api` and `test-stress` fail there; mimalloc2 has no such exclusion, making it a v3 regression on 64K-page architectures. |
| **vcpkg** 3.4.3 | `pkgconfig-cxx.diff` — with `MI_USE_CXX=ON` the generated `.pc` omitted libstdc++. |
| **Conan** | Three patches, all on 2.x; 3.3.2 is pristine. `MI_OVERRIDE` and `MI_WIN_REDIRECT` default **off**. |
| **conda-forge** | Patch `0003` makes `_mi_theap_malloc_zero` `extern inline` because a **Windows shared-library build of v3.4.1 otherwise fails to link** — a live v3 bug in the `mi_theap_t` layer. |

### The dominant real-world bug class is ISA baseline, not the allocator

`rorx`/BMI2 and `popcnt` SIGILLs on older x86, `ldaddal` on Cortex-A72 — Debian #1094881
(grave), #1106879 (open), Fedora #2342055, #2425568 (**reopened**). Debian and Fedora
*independently* discovered that **`MI_OPT_ARCH=OFF` is a no-op** — the arm64 branch re-enables
`-march` regardless; the working knob is `MI_NO_OPT_ARCH=ON`. Related: `CMAKE_SYSTEM_PROCESSOR`
comes from the *running* kernel, so armhf on an arm64 kernel took the arm64 branch.

A second instance of the same defect shape: riscv64 MMU mode was detected **at build time**,
pinning VA bits to 39; the buildd was SV39 and users ran SV48 → SIGSEGV. Fixed only in v3.4.1 by
moving to runtime detection.

**Debian sets `MI_BUILD_TESTS=OFF`** — mimalloc's tests run on no Debian buildd on any
architecture. Every bug above was found by users or downstream build failures. Distro packaging
is not a portability signal.

---

## Cross-cutting themes

**Multiple mimalloc instances in one process** is the recurring failure, found *independently*
in five ecosystems: napi-rs (macOS fixed TLS slot, #1301), Arrow (`MI_TLS_MODEL_LOCAL`, #1327),
conda-forge (dlopen), Bun (musl TLS model), PHP (`RTLD_DEEPBIND`, #377). Any project shipping
mimalloc inside a shared library will meet it.

**`src/static.c` is load-bearing ecosystem-wide.** Every integration surveyed compiles it as a
single translation unit; Bun documents why — per-file CMake builds produce duplicate symbols.
That validates this repo's rule 6 well beyond the Rust sys crate.

**Nobody enables `MI_SECURE` by default** except Alpine, as packaging policy.

**Environment variables are process-global**, which the Arma 3 fork discovered the hard way:
NVIDIA's display driver embeds mimalloc 3.1.6, so `MIMALLOC_*` set for your process is also
honored by NVIDIA's instance.

---

## Pass 3: should we merge it?

Rated per **change**, not per fork, because most forks mix genuinely upstreamable work with
purely local glue. Rated from this repository's v3 line.

| # | Meaning |
|---|---|
| **5** | Take it now — fixes a real bug affecting users beyond the fork that found it |
| **4** | Strong candidate — broadly useful, needs modest adaptation |
| **3** | Take it behind a flag — real but workload-dependent |
| **2** | Probably not — narrow benefit or superseded by v3's design |
| **1** | Do not merge — project-specific or conflicts with v3 |

| Change | Source | Rating | Reasoning |
|---|---|---|---|
| `free(NULL)`-before-init NULL check (#1341) | Arch cherry-pick | **3** | Release-only null deref triggered by glibc 2.44's `__newlocale` calling `free(NULL)` during static init. Same bug class as the `mi_heap_new`/`mi_subproc_new` bootstrap bug we already fixed. **Downgraded from 5 after testing:** our v3.4.3 base survives `mi_free(NULL)` as the first mimalloc call in a statically linked build (verified), and `page-map.c:83` sets `_mi_page_map[0] = 1` precisely so `_mi_ptr_page(NULL)` returns NULL. The upstream repro is `LD_PRELOAD` + glibc 2.44 on Linux, which we did not reproduce — so this is "appears handled in our configuration", not "immune". Worth tracking rather than cherry-picking blind. |
| `extern inline` for `_mi_theap_malloc_zero` | conda-forge patch 0003 | **5** | A Windows **shared-library** build of v3.4.1 fails to link without it. Our `src/static.c` amalgamation almost certainly masks this; a `MI_BUILD_SHARED=ON` Windows job would surface it. Cheap, and it is in exactly the layer our profiler hooks. |
| `MI_NO_OPT_ARCH=ON` as our build default | Debian/Fedora/Gentoo/nixpkgs | **5** | The single most common real-world mimalloc bug is a SIGILL from `-march` exceeding the target baseline, and `MI_OPT_ARCH=OFF` **does not work**. Four independent distros converged on the same fix. Costs nothing to adopt. |
| Adversarial profiler test corpus | Bun `test-prof-adversarial.c` | **5** | Directly applicable prior art for a profiler with per-page state; we already imported two of its cases (aligned allocations, empty-profile validity) and found genuine gaps. Free correctness. |
| v3 theap/arena race fixes | Bun | **4** | `mi_theap_merge_stats` vs. concurrent `_mi_theap_free`, and clearing arena bits plus the page map on alloc-fresh failure paths. Real races in code we ship. Needs careful review against our pinned base rather than blind cherry-picking. |
| **Zero the new TLS slots after slot-array growth** | **Bun [`d078ad06`](https://github.com/oven-sh/mimalloc/commit/d078ad06), MIT** | **5 — IMPORTED (#148)** | `rezalloc` preserves the uninitialized slack between the requested size and the bin size, and `_mi_thread_local_get` validates a slot only by its version lane, so garbage could be returned as a `mi_theap_t*`. Confirmed deterministically on our tree before importing: `mi_malloc(40)` → usable 48 → 8 stale bytes survive `mi_rezalloc`. Note the symmetry — Bun fixed the zeroing, we had separately fixed the same function's *provenance* bug (#128 B3), and **each fork still had the other's half** until this import. |
| **`page-map`: give the initial empty page map a real all-NULL submap so `free(NULL)` works before init** | **Bun [`7ac561ab`](https://github.com/oven-sh/mimalloc/commit/7ac561ab), MIT** | **5 — IMPORTED (#266)** | Bumping our overlay pin to `6def7be9` pulls in upstream's 2-level page-map rewrite (`d63979ae`), which introduces the bug this commit fixes: the initial static page map's submap-0 entry is `NULL`, and `_mi_unchecked_ptr_page` (the release/unchecked lookup) indexes `submaps[0][0]` for `p==NULL` without a NULL check. glibc 2.44's `__newlocale` calls `free(NULL)` from the loader before any constructor runs; with `MI_MALLOC_OVERRIDE` that reaches `mi_free` and faults at address 0 before `main()`. The fix points `submaps[0]` at a real zero-initialized submap instead. Verified RED→GREEN locally (glibc 2.42, so the loader itself doesn't trigger it, but the imported `test/test-free-before-init.c` calls `mi_free(NULL)`/`mi_malloc(64)` from a `.preinit_array` entry, the same point in startup): segfaults against the unpatched `mi_page_map_empty`, passes after the fix. Registered as `test-free-before-init`, linked against the static lib specifically because the shared (`MI_OVERRIDE=ON`) target compiles with `MI_FREE_IS_CHECKED=1`, which routes through the NULL-safe `_mi_checked_ptr_page` and would never exercise the bug. |
| **`prof_force_slow` / poison `pages_free_direct` while profiling runs** | **Bun [`942b8342`](https://github.com/oven-sh/mimalloc/commit/942b8342), MIT** | **5 — IMPORTED (#281)** | Strategy import (not a code diff): `mi_theap_t::prof_force_slow` plus `_mi_theap_pages_free_direct_poison`/`_mi_subproc_prof_set_force_slow` (our own new functions, adapted to our `mi_theap_t`/`mi_subproc_t` layout and lock structure rather than Bun's `MI_PAGE_HAS_PROF_SAMPLES` xthread_id flag bit). Fixes the +70% ns/alloc `MI_PPROF=ON`-stopped regression measured in #154/#170: `mi_malloc`/`mi_heap_malloc_small`'s fast path now disassembles byte-identical between `MI_PPROF=ON` and `MI_PPROF=OFF`. See #267/#264 B9 for the full design and #281 for the disassembly/timing evidence. |
| **`stats.c`: racy `prev_total == peak` compare in `mi_stat_adjust_mt`** | issue #266 investigation | **N/A — not present, no action taken** | #266 described this as an upstream regression between our previous pin (`bcee5a88`) and `6def7be9` that Bun then removed, to be excluded when taking `stats.c`. Checked directly: `git show 6def7be9:src/stats.c`'s `mi_stat_adjust_mt` has no `peak` compare of any kind (it only does two `mi_atomic_addi64_relaxed` calls), and it is byte-identical to Bun's `src/stats.c@942b8342`. The peak-tracking compare that does exist in the file (`mi_stat_update`, the single-thread non-`_mt` variant) is unchanged from before and is not the one #266 described. So there was nothing to remove; the issue's description of this item was incorrect for the actual commit range. |
| **`mleak` (mimalloc-bench)** | daanx/mimalloc-bench `bench/mleak/mleak.c`, MIT | **run, #50** | 144-line standalone test: N rounds of threads that allocate a small long-lived object and then exit -- i.e. exactly the thread-exit scenario the MinGW detection fix (originally carried as a fork patch, now upstream-native as of the 6def7be9 pin -- see docs/upstreaming.md) addresses. Built against our Release static lib and run at 10 threads x 100 x {1,3,9} rounds: **completes cleanly, exit 0, no crash or hang**. Not used for a leak *number*: it prints no memory summary of its own, `MIMALLOC_SHOW_STATS`/`VERBOSE` emit nothing in a Release static build, and `PeakWorkingSet64` reads 0 once the child has exited. Our own churn probe (#114 / microsoft/mimalloc#1349) already quantifies the same scenario better and in-process: **+16,992 KiB stock vs +384 KiB fixed** over 400 thread create/join rounds, same commit, one token apart. So mleak is independent confirmation of no regression, not a measurement instrument. |
| `pthread_atfork` fork-safety handlers | Bun (`_mi_process_fork_prepare/parent/child`) | **5 on POSIX — IMPORTED (#270)** | Repro landed first (`test/test-fork-locks.c`). Handlers, the lock order they implement and their debug self-checks live in a new `src/fork.c` (registered once in `src/init.c`'s `mi_process_init_once` under `#if !defined(_WIN32) && !defined(__wasi__)`, wired into `src/prim/osx/alloc-override-zone.c`'s zone `force_lock`/`force_unlock`/`reinit_lock` and its `_malloc_fork_prepare/parent/child` DYLD interpose — macOS is compile-verified only here, see #270's cross-CI note). The lock skeleton is Bun's; the ORDER is not — it was re-derived from this tree's actual lock-nesting graph (documented edge by edge in `src/fork.c`), which differs from Bun's stated rule in two load-bearing ways: (a) the alloc/free **hook** locks (`prof_lock`/`dhat_lock`/`memevt_cb_lock`) are taken from inside the allocation path here, so they must be innermost, not outermost — the ported rule deadlocked deterministically under `MIMALLOC_PROF=1`; (b) `_mi_meta_zalloc` holds `theap_meta_lock` across a full allocation on `heap_main`, so `theap_meta_lock` must precede `heap_main->arena_pages_lock`, `arena_reserve_lock` and the page-map lock. Because a process-global lock (`mi_thread_locals_lock`) sits between two per-subprocess levels, prepare runs one pass per level over all subprocesses rather than one walk per subprocess. `MI_DEBUG>2` builds turn the order into a runtime detector: diagnostic.c's reentrancy checker already records each lock's owning thread, so every acquire records the nesting edges actually observed in the process, and every `fork()` asserts they agree with the documented order. Bun's `tld` registry and scavenger/park state did not exist in this tree when this phase landed, so `mi_tld_t::theaps_lock` was left a **known, documented gap** — CLOSED by #272 (Phase 7a), which adds the registry and then re-initializes that lock in the forked child rather than acquiring it in `prepare`; see `src/fork.c` for why acquiring it is the wrong answer. One further thing this phase found that Bun's design does not handle: `mi_fork_depth` as a single process-wide atomic assumes fork() calls are serialized across threads, which glibc does **not** do (verified empirically). Fixed here with a shared `mi_fork_owner`/depth pair protected by a real `mi_fork_serialize_lock` (no TLS — see #266). |
| `mi_page_map_get_idx` over-counts one slice for OS-aligned pages | Bun [`0e150b5c`](https://github.com/oven-sh/mimalloc/commit/0e150b5c) | **2 — NOT taken, corruption not reproducible** | The two-level page-map variant indexes from `page_start` but still adds the `page_start - slice_start` correction only the flat variant needs, so registration claims one slice past the page and unregistration would zero it. Over-count confirmed by instrumentation (`corr=1 registered=2` for a 1 MiB-aligned request, body spans 1 slice). **But the corruption does not reproduce**: 512 interleaved os_align/ordinary pairs under `MI_DEBUG_FULL`, aligned freed first, came back clean. Structural reason — an os_align page is served by an OS allocation that over-allocates for alignment, so the extra slice lands in that allocation's own trailing slack where nothing else is mapped. Downgraded from 4 to 2 on that evidence. Real defect, still present at `upstream/dev3` tip `1f06f694`, worth an upstream report; not exploitable here on the evidence available. Re-open with a repro that actually corrupts. |
| `mi_on_thread_idle` + background scavenger | Bun `src/scavenger.c` | **5 — IMPORTED (#272, Phase 7a)** | `mi_on_thread_idle` is a linked Bun symbol; the scavenger and the park handoff behind it live in a new `src/scavenger.c` (the file is Bun's; the idle half — `_mi_thread_idle_work`, `_mi_park_leave`, `_mi_theap_sweep_parked`, `mi_on_thread_idle*` — is moved here from Bun's `src/theap.c` so upstream files keep only a handful of guarded lines, CLAUDE.md rule 6). Default-on (`mi_option_scavenger`, appended after `purge_zeroes`; no existing slot renumbered), together with `purge_delay` 1000 → 100 ms. Also ported: the per-subprocess `tlds` registry, `_mi_arenas_purge_now`, and the wake on the subproc purge-expire 0→set edge. Three deviations from Bun, each with evidence in the PR: (a) the lazy start fires only from a MAIN-subproc thread — a scavenger created from a sub-subprocess thread has its stack/TLS in arenas `mi_subproc_destroy` unmaps before `mi_process_done` joins it (reproducible SIGSEGV in `_dl_deallocate_tls` under `test-stress-subprocs`); (b) the new `mi_subproc_t` fields are appended at the TAIL rather than mid-struct — shifting `mi_subproc_t::stats`, which the free path touches, cost ~2 ns/alloc+free on the #154 microbenchmark; (c) `mi_tld_t::theaps_lock` is re-initialized in the forked child and deliberately NOT acquired in `prepare` (acquiring it moves `test-fork-user-heap` case_b's child-side deadlock into the parent). This also closes P5's documented `mi_tld_t::theaps_lock` KNOWN GAP. The profiler-vs-purger interaction was designed rather than assumed: records are unlinked under `prof_lock` before a page can reach `mi_arena_schedule_purge` (asserted in `mi_arenas_page_free_ex`), `mi_prof_visit` / `mi_prof_snapshot_new` never follow a `mi_page_t*` at all, and the scavenger asserts both that it never acquires a theap of its own and that a swept thread's `mi_tld_t.profiler` countdown is untouched. Three bugs found on the way and fixed: `_mi_prim_thread_yield` was `sleep(0)` (glibc `nanosleep({0,0})`, never reschedules — every spin-then-yield in the tree was a busy spin); `_mi_arenas_try_purge`'s one-purger guard was a function-local static a fork could inherit held; and that same guard being non-blocking made `mi_collect(true)` silently purge nothing whenever it lost the race with the scavenger. |
| Page hole purging (`purge_holes*`) | Bun `src/page.c` (+1038) | **5 — open, scheduled (#272, Phase 7b)** | The second clause of `mi_on_thread_idle`'s contract: discard the free blocks inside still-used pages. Default-on in Bun. Lands in `src/page-holes.c` with only hook calls in `page.c` (rule 6), together with `mi_purge_holes_stats_get`/`_report`, `test-purge-holes.c`, `test-purge-zero.c`, and the unification with this fork's own `mi_option_purge_zeroes`. |
| `zalloc` skips `memset` after zero-purge | Bun | **4** | Clean, self-contained win with no API surface. Needs the zero-tracking bookkeeping it depends on, so not a one-line lift. |
| `MI_NO_PROCESS_DETACH` | Bun | **5 — IMPORTED (#268)** | Bun sets it unconditionally, so a rebase changes their process teardown behavior without it; the port was ~5 lines: a CMake option, an early return in `_mi_auto_process_done` (`src/init.c`), and the `#ifndef`-guarded destructor registration (`src/prim/prim.c`). Useful for embedders who own teardown. Confirmed the Windows `DllMain`/TLS-callback and `atexit` exit paths (`src/prim/windows/prim.c`) all funnel through `_mi_auto_process_done`, so the `init.c` early return covers them too. `MIMALLOC_PROF_DUMP_AT_EXIT`/DHAT dump-at-exit are consequently also skipped under the define -- documented in `include/mimalloc/profile.h` and the README; an embedder using `MI_NO_PROCESS_DETACH` must call `mi_prof_dump`/`mi_dhat_dump` itself. `test/test-no-process-detach.c` built twice (with/without the define) and checked against opposite expectations on the `MIMALLOC_VERBOSE=1` "process done" line. Not via `PASS_REGULAR_EXPRESSION`/`FAIL_REGULAR_EXPRESSION` as originally scoped -- those have no bundle equivalent (`ci/bundle_tests.py`'s `SUPPORTED_PROPERTIES`, issue #277) and the `bundle-roundtrip` c-unit job refuses them outright. Generalized the existing `test/run-negative.cmake` `cmake -P` harness pattern instead: `test/run-text-check.cmake` (MODE=REQUIRE\|FORBID, zero-exit contract) plus a matching `forbid_text` field through `bundle_tests.py`/`run_test_bundle.py`. |
| **`mi_heap_dump_json` / `mi_heap_get_seq`** | **Bun [`942b8342`](https://github.com/oven-sh/mimalloc/commit/942b8342), MIT** | **5 — IMPORTED (#286)** | Backs the shipped `bun:jsc` `heapStats({dump:true\|"blocks"}).mimallocDump` API; `heapStats-mimalloc.test.ts` (actual path `test/js/bun/jsc/heapStats-mimalloc.test.ts`, not `util/` as originally cited) asserts the exact JSON shape (`heaps[].seq`, `pages[].{id,block_size,used,reserved,thread_id}`, `blocks[[id,size]]`) -- every assertion ticked in #286. `mi_heap_t::heap_seq` already existed at our pin (`include/mimalloc/types.h`, set in `src/heap.c`), so only the two-line accessor and the dump walk (`src/heap-dump.c`, new file per rule 6, using `mi_subproc_visit_heaps` + `mi_heap_visit_blocks`) were needed. |
| Heap delete/destroy teardown race protocol | Bun (`src/theap.c` +346, `src/heap.c` +61, `src/arena.c`) | **5 — IMPORTED (PR #291)** | `MimallocArena` does `mi_heap_new` per parse call and `mi_heap_destroy` on `Drop`, across threads, at high rate — the exact pattern their `test-heap-teardown.c` / `test-heap-churn.c` / `test-heap-aba.c` corpus stresses. Ported the four-step claim protocol (`ec238987`, `8286bfb6`, `ff96441a`; the `claim_pages` skeleton `8286bfb6` builds on is itself fork-only, `0e150b5c`, not at this tree's pin) adapted for the absence of `pthread_atfork` (#270, not yet merged) and the scavenger/park state (#272). Fixed the exact `mi_theap_is_valid` ABA reproduced from PR #289's evidence (detach must clear `theap->heap`, not `theap->tld`), plus a missing `MI_THREADID_DETACHED` exception in `theap.c`'s validity assertions (present everywhere else in the tree, never ported here). Along the way, found and fixed two use-after-free classes the newly-working protocol made reachable: `mi_arenas_page_free_prim` reading `page->heap` after unpublish (this tree's own instance of the bug `ff96441a` fixes upstream), and — outside anything Bun's tree has — this fork's own always-on `_mi_memevt_on_free`/`_mi_prof_on_alloc` hooks and vanilla `mi_stat_free` reading `page->heap`/`page->theap` from the cross-thread free path, racing a concurrent heap free on another thread. `test-theap-sentinel.c` not imported (targets a `(mi_theap_t*)1` sentinel bug in Bun's `mi_heap_init_theap` that this tree's differently-shaped implementation does not have). `test-park-handoff.c` and the `parked` case of `test-heap-teardown.c` deferred to #272 (Phase 7). Deviates from Bun in one spot: where Bun's `mi_heap_visit_page_claim` hits `mi_assert(false && ...)` on a thread still using a heap mid-delete (debug-only, aborts in Debug and is a no-op in Release), this port calls `_mi_error_message(EINVAL, ...)` unconditionally (reports in every config) before force-seizing the page the same way Bun does. Also ported, after merging P5 (#289): Bun's `_mi_process_is_forked_child` flag (defined in `src/subproc.c`, set once in `src/fork.c`'s `_mi_process_fork_child`, never cleared) and its two consultation sites (`mi_heap_visit_page_claim` in `arena.c`, `mi_heap_detach_theaps` in `heap.c`) — #289's `pthread_atfork` handlers reset lock/subsystem state but never added this persistent flag, and its absence was a genuine regression (`test-fork-user-heap`'s `case_a` intermittently hit `mi_page_is_valid_init` after the merge). KNOWN LIMITATION carried over from Bun, not fixed here: the flag is sticky (set, never cleared), so a forked child that later spawns its own threads (#272) stays on the conservative force-seize/no-abandon paths permanently; tracked as a follow-up in #293 once #272 lands. |
| **`stats.c` snapshot printing + counter-at-a-time `mi_stats_copy`** | **Bun [`942b8342`](https://github.com/oven-sh/mimalloc/commit/942b8342), MIT** | **4 — IMPORTED (#286)** | `_mi_stats_print` now prints from a `mi_stats_add` snapshot instead of the live, concurrently-updated stats struct, and `mi_stats_copy` copies counter-at-a-time through `mi_stats_add` instead of a torn-read-prone `_mi_memcpy`; Bun has an open bug on this surface (bun#28630). |
| glibc 2.44 `free(NULL)`-before-init fix | upstream `7ac561ab` + `test-free-before-init.c` | **5 once the pin moves — open, scheduled (#266)** | Not a bug at our pin `bcee5a88` (the flat page map returns NULL cleanly; see the Arch row above). The 2-level pagemap restructure in the pin bump introduces it, so the fix is carried in the same PR. |
| **Windows PRNG / RAM-sizing fixes** | **Bun [`942b8342`](https://github.com/oven-sh/mimalloc/commit/942b8342) (`6ccccec2`, `c3c36aa8`, `75a1edf8`, `d676cced`), MIT** | **5 — IMPORTED (#297)** | `_mi_prim_random_buf` now prefers `ProcessPrng` (bcryptprimitives.dll, Windows 8+) over always loading bcrypt.dll and resolving `BCryptGenRandom` (~1ms of every process start, paid under the loader lock); `mi_win_loadlibrary`/`mi_win_getlibrary` take `wchar_t*` and call the wide APIs directly instead of `TEXT()`/`TCHAR*` (which resolved to the ANSI entry points here); physical memory is read via `GlobalMemoryStatusEx` instead of `GetPhysicallyInstalledSystemMemory` (SMBIOS parse). Classified against overlay pin `6def7be9` per #66: `git log --oneline 6def7be9..upstream/dev3 -- src/prim/windows/prim.c` is empty, all four commits fork-only. No import lib needed (GetProcAddress-resolved, like the existing bcrypt.dll call). |
| macOS malloc-zone introspection | Bun `alloc-override-zone.c` | **3 — open, unscheduled (candidate for a later phase; zone wiring exists since P5)** | In-/out-of-process `memory_reader_t` for `leaks`/`heap`/`vmmap` and fork-safe zone locking; a tooling regression on macOS without it. |
| **macOS fixed TLS slots 108/109 → 96/97** | **Bun [`942b8342`](https://github.com/oven-sh/mimalloc/commit/942b8342) (`include/mimalloc/prim-tls.h:356-361`), MIT** | **5 — IMPORTED (#297)** | Not a hypothetical collision: per apple-oss-distributions/libpthread's `tsd_private.h`, 108/109 are `__PTK_FRAMEWORK_SWIFT_KEY8`/`_KEY9` — assigned, in-use slots. 96/97 sit in the never-assigned gap after `__PTK_FRAMEWORK_CORETEXT_KEY0`=95; Bun has production evidence for the choice. macOS defaults to `MI_TLS_MODEL_PTHREADS`, not `MI_TLS_MODEL_FIXED`; compile coverage for the fixed model comes from the `macos-arm64-debug-full` bundle (`-DMI_TLS_MODEL_FIXED=ON`, #273 8c), which builds `test-tls-slots-heap`/`test-threadlocal-growth` against it. CI also fetches `apple-oss-distributions/libpthread`'s `tsd_private.h` from `main` (not a pinned commit — a pin can never observe a slot Apple assigns later) and fails if either slot is ever assigned. |
| Linux NUMA node count off-by-one | microsoft/mimalloc upstream/dev3 `66383f06` (PR #1365, David Carlier); cherry-picked by Bun as `16cd3684` | **5 — IMPORTED (#297)** | Landed upstream 2026-08-14, 5 days after our pin `6def7be9` (2026-08-09) — upstream-post-pin, not fork-only; #273 mislabeled it as a Windows fix. `_mi_prim_numa_node_count` returned the highest node index instead of the count (2 nodes reported as 1); fixed to `last_found + 1`, matching the Windows primitive's `GetNumaHighestNodeNumber() + 1`. Flag at the next pin bump so it is not double-applied. |
| Heap snapshot + `mi-heapview` | Bun `src/heap-snapshot.c`, `tools/mi-heapview.c` | **2 — open** | Not called by Bun today (`snapshot_on_exit` default 0); a debug tool, not a shipped feature. |
| `purge_delay` default 1000 → 100 ms | Bun | **3 — IMPORTED (#272, Phase 7a)** | Matches Bun's choice (bun#34217); lands with the scavenger, which it interacts with. Note: `arena_purge_mult` is **already 1** upstream — the earlier "10→1" claim in the Bun research was wrong; only `purge_delay` differs. |
| Page-metadata liveness bits | MetaSafe | **2** | Elegant O(1) UAF detection, but it competes for exactly the page-metadata space our profiler uses, and MetaSafe's 8.3× memory blowup under tokio is disqualifying as a default. Interesting to read, not to merge. |
| `mi_heap_page_utilization()` | monographdb | **2** | Reasonable idea, but v3's per-heap statistics already expose more than this, and the fork is abandoned 1331 commits behind. |
| Host-controlled init / `mi_process_done` | Malterlib | **2** | Solves a real embedder problem, but overlaps `MI_NO_PROCESS_DETACH` and is entangled with their build system. |
| Arma 3 tuning defaults | GoldJohnKing | **1** as defaults, **4** as documentation | `purge_delay = -1` means *never return memory to the OS* — correct for a game that owns the machine, actively harmful for a server. The **pattern** (`set_default` so env vars still win) is worth copying; the values are not. |
| `MI_MUSL_BUILTIN` | Dolt | **1** | Structurally the most interesting idea here, and inapplicable — it requires a patched libc. Cite it, don't merge it. |
| Unity `IUnityMemoryManager` backend | CyberAgent | **1** | Exemplary as a template for adding a prim backend. Zero relevance to us. |
| Software pointer tagging | xTag | **1** | 24.9% runtime overhead, changes the OS mapping strategy wholesale, v1.6.1-era. Research only. |
| libc removal / freestanding | NikoMalik, MaskRay | **1** | Directly opposed to our goals — we *add* a profiler that needs symbolization and OS services. |

### What we should actually do

1. **Add `MI_NO_OPT_ARCH=ON`** — or consciously decide our binaries may use ISA extensions above
   the target baseline. Four distros independently learned this the hard way.
2. **Add an `MI_BUILD_SHARED=ON` Windows job** to surface the `extern inline` link failure that
   our amalgamation currently hides.
3. ~~**Mine Bun's test corpus further** — `test-heap-delete-race.c`, `test-commit-fail.c` and the
   `MI_DEBUG` fault-injection hooks all target hazards we share.~~ Done (#271):
   `test-heap-delete-race.c`, `test-commit-fail.c`, `test-heap-teardown.c`, `test-heap-mt.c`,
   `test-heap-churn.c`, `test-heap-aba.c` imported.
4. **Watch #1087.** A 100%-reproducible page-map access violation on Windows with ASLR disabled,
   unanswered for over a year, is a live risk for a Windows-first fork.
5. **Treat multi-instance/dlopen as a known hazard** if this ever ships as a shared library.

---

## Sources

Upstream: [microsoft/mimalloc](https://github.com/microsoft/mimalloc) ·
[mimalloc-bench](https://github.com/daanx/mimalloc-bench)

Forks: [oven-sh/mimalloc](https://github.com/oven-sh/mimalloc) ·
[GoldJohnKing/mimalloc](https://github.com/GoldJohnKing/mimalloc) ·
[dolthub/mimalloc](https://github.com/dolthub/mimalloc) ·
[dolthub/musl](https://github.com/dolthub/musl) ·
[CyberAgentGameEntertainment/mimalloc](https://github.com/CyberAgentGameEntertainment/mimalloc) ·
[Malterlib/mimalloc](https://github.com/Malterlib/mimalloc) ·
[MaskRay/mimalloc-lite](https://github.com/MaskRay/mimalloc-lite) ·
[WeiguangTWK/android_external_mimalloc](https://github.com/WeiguangTWK/android_external_mimalloc) ·
[NikoMalik/mimalloc](https://github.com/NikoMalik/mimalloc) ·
[monographdb/mimalloc](https://github.com/monographdb/mimalloc) ·
[unikraft/lib-mimalloc](https://github.com/unikraft/lib-mimalloc) ·
[rub-syssec/xTag](https://github.com/rub-syssec/xTag) ·
[moggedhedien/rimalloc](https://github.com/moggedhedien/rimalloc)

Vendored: [python/cpython#113141](https://github.com/python/cpython/issues/113141) ·
[emscripten system/lib/mimalloc](https://github.com/emscripten-core/emscripten/tree/main/system/lib/mimalloc) ·
[Apache Arrow](https://github.com/apache/arrow)

Upstream issues cited: #147, #263, #309, #372, #377, #482, #798, #817, #939, #996, #1046,
#1048, #1077, #1087, #1152, #1213, #1266, #1301, #1327, #1333, #1341.

---

## Upstream's own branches (issue #84)

Third-party forks are only half the landscape. `microsoft/mimalloc` carries **44 branches**,
and fixes reach them well before a release. All were classified against our pin
**`bcee5a88`** (= `upstream/dev3` tip, after the #80 bump).

**Headline: almost nothing is importable, and the two things that looked most promising
were both mirages.** That is the useful result — it means the pin bump, not branch
cherry-picking, is how upstream work reaches us.

### The two leads that did not survive contact

**`copilot/review-*` (three branches) — empty.** These were billed in #84 as possibly the
highest-value artifact in the tracker: `1ec619bc` is titled *"Audit include/src for
allocator safety issues and deliver prioritized triage."* It changes **no source at all** —
`git diff --stat bcee5a88 1ec619bc -- src/ include/` is byte-identical to the diff against
its own base. What the commits actually add are leaked build artifacts: `a.out`, `*.o`,
and a 49-line assembly experiment. There is no triage document in any of the three trees,
and no PR exists for any of them. They are abandoned Copilot session branches; their
reasoning lives in `github.com/microsoft/mimalloc/sessions/…`, which is not publicly
retrievable.

**`dev3-meta` — its "unmerged use-after-free" is branch-local.** 57 commits, actively
developed, and it does contain `2f2c19c6` *"fix access after free for theap_meta stats in
subprocesses."* But the same branch **introduced** that bug three hours earlier in
`1eab8008`, and the code it lives in does not exist in our tree: `git grep theap_meta
bcee5a88 -- src include` returns **zero hits**, as do `src/subproc.c` and
`src/prim/prim-tls.c`. The branch is one unfinished refactor — replacing `src/arena-meta.c`
with a detached theap — whose own tip commit is *"try to fix test heap-os2."* Nothing to
import; its second "priority" commit (`a5650085`) is likewise 3/4 branch-local.

### Where upstream's real audit findings live

Not in the copilot branches — in **upstream issue #1271**, an external LLM audit by *Zoxc*
run in four passes (~132 findings), each triaged by daanx. Still open. Because our pin *is*
the `dev3` tip, nearly every accepted fix is already in; nine spot-checks all confirmed
genuinely fixed. The value is the **residue** daanx marked `todo`/`revisit`, three items of
which sit in paths our hooks occupy — tracked in issues of our own rather than repeated here.

### Classification

| Branch(es) | Status | Verdict |
|---|---|---|
| `dev3` | **our pin** | 0 ahead / 0 behind after #80 |
| `dev3-meta` (57 ahead) | not-in-pin | unfinished meta refactor; nothing separable. **Watch:** if merged, `src/arena-meta.c` is deleted and `src/subproc.c` + `src/prim/prim-tls.c` appear — `src/static.c` must be updated or the Rust sys crate silently loses them |
| `dev` (3 ahead) | not-in-pin | **one real fix**: `1fb345674` makes `mi_page_set_in_full` use `page->reserved` rather than `page->capacity` |
| `pr-1266` | not-in-pin, v2-based | **an upstream heap-profiling PR claiming our filenames** — see below |
| `copilot/review-*` ×3 | empty | build artifacts only; no findings recoverable |
| `dev3-subproc`, `dev3-separate`, `dev3-heap`, `dev3-nuget` | already-in-pin | 0 ahead; fully merged |
| `dev3-cdb`, `dev3-cdb-sk`, `dev3-bin`, `dev3-bin-dbg` | abandoned | forked before the arena/page-map rewrite; trees obsolete (~210 files divergent) |
| `dev-guarded` | superseded | 27 lines of "initial work"; full `MI_GUARDED` shipped long ago |
| `users/gustavovaro/cherry-pick-arm64-fix` | already-in-pin | its one distinctive line is already at `CMakeLists.txt:150` |
| `dev-slice*` ×7, `dev-remap`, `dev-reset`, `dev-platform`, `dev-align`, `dev-trace`, `dev-atomic`, `dev-debug`, `dev-exp-tls`, `dev-win` | abandoned | segment-era experiments (2020–2025); v3 replaced segments with `src/arena.c` entirely |
| `main`, `dev-main`, `dev2`, `dev2-bin` | different release line | the v1/v2 line. Their 3000+ "ahead" counts are that line, **not** 3000 features |
| `daanx-patch-*`, `users/GitHubPolicyService/*` | noise | readme edits and a policy bot |

### `pr-1266`: upstream has a competing heap profiler, in our filenames

*"feat: enable memory profiling"* by Daniel Schwartz-Narbonne (Datadog) adds
**`include/mimalloc/profile.h`, `src/profile.c`, `test/test-profile.c`** — the same paths
this fork claims — with sampled records hung off `page->metadata` behind a
`page->has_metadata` flag, and inline hooks in `alloc.c`/`free.c`/`page.c`.

It is v2-based (touches `src/segment.c`), so it does not apply to our v3 pin and is not
importable. It matters anyway: it is a **collision risk for our upstreaming plan**. If it
lands, our `pr/*` branches conflict at the file level, and there are then two competing
designs for the same feature in the same namespace. Read its design and discussion before
cutting the next upstream PR.

### Method note

Every row was settled with `git merge-base --is-ancestor <sha> bcee5a88` rather than by
reading code and guessing, and branches with large ahead-counts were checked for *which
release line* they belong to before any significance was attributed. Three of the entries
above are corrections to claims made earlier in this repository's own issue tracker.
