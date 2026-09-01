# Gap matrix: is `zackees/mimalloc-pprof` a functional superset of `oven-sh/mimalloc@942b8342`?

Analyst pass (agent 4). Every claim marked **[V]** was re-verified against the raw sources in
this session (paths + line numbers given); **[I]** is inference from verified facts.
Corrections to reports 1–3 are called out inline.

---

## 1. Verdict

**NO — not a superset, and not close on the axes Bun actually depends on.** Ours is missing
three symbols Bun links today (`mi_on_thread_idle`, `mi_heap_dump_json`, `mi_heap_get_seq`),
sits **205 upstream commits** behind Bun's base (`bcee5a88..6def7be9`) [V], and imposes an
unconditional out-of-line call on every `mi_malloc` when the profiler is compiled in — a
regression Bun cannot opt out of without also losing the profiler they are shipping in
[bun#30231].

---

## 2. Gap matrix — Bun's actual consumer surface

### 2a. Linked C symbols

I re-derived the symbol set myself rather than trusting report 2's "33". Extracted every
`mi_*` token from `mimalloc_sys.rs`, `MimallocArena.rs`, `bun_alloc_lib.rs`,
`MimallocWTFMalloc.h`, `BunJSCModule.h`, `UnsafeObject.rs`, `jsc_hooks.rs`, `output.rs`,
`bin_entry_mod.rs`, then filtered to real `extern "C"` declarations. [V]

**Correction to report 2:** `mi_heap_get_seq` is *not* a consumer symbol. It appears only
inside Bun's own `src/stats.c:947`, called by `mi_heap_dump_json`; no Bun Rust/C++ file
declares it [V]. The JSON it feeds (`heaps[].seq`) *is* asserted by Bun's test suite, so it is
still load-bearing — just as an implementation detail of `mi_heap_dump_json`, not a link
dependency. Likewise `mi_malloc_auto_align` / `mi_zalloc_auto_align` /
`mi_heap_{malloc,zalloc}_auto_align` / `mi_free_checked` are **Rust-side inline wrappers**
in `mimalloc_sys.rs:194–241`, not C symbols [V] — reports listing them as fork API are wrong.

| Symbol | Status in ours | Evidence |
|---|---|---|
| `mi_malloc` `mi_calloc` `mi_realloc` `mi_expand` `mi_free` `mi_zalloc` `mi_usable_size` `mi_malloc_usable_size` `mi_free_size` `mi_free_size_aligned` | **SAME** | `include/mimalloc.h`, all present [V] |
| `mi_malloc_aligned` `mi_zalloc_aligned` `mi_realloc_aligned` | **SAME** | [V] |
| `mi_heap_new` `mi_heap_destroy` `mi_heap_main` `mi_heap_malloc` `mi_heap_zalloc` `mi_heap_calloc` `mi_heap_realloc` `mi_heap_malloc_aligned` `mi_heap_zalloc_aligned` `mi_heap_realloc_aligned` `mi_heap_visit_blocks` `mi_heap_collect` | **SAME** | `mi_heap_main` at `include/mimalloc.h:239` (upstream, present in both) [V] |
| `mi_is_in_heap_region` `mi_collect` `mi_stats_print_out` `mi_process_info` `mi_option_set` | **SAME** | [V] |
| `mi_stats_get_json` | **SAME (symbol)** / see 2c for payload | `include/mimalloc-stats.h:152`, `src/stats.c:824` [V] |
| **`mi_on_thread_idle`** | **MISSING** | zero hits across `include/ src/` [V]; declared `mimalloc_sys.rs:30` as `pub safe fn` |
| **`mi_heap_dump_json(bool,bool)`** | **MISSING** | zero hits [V]; declared `BunJSCModule.h:53`, called `:403` |
| **`mi_heap_get_seq`** | **MISSING** | zero hits [V]; internal to the above |
| `mi_prof_*` `mi_heap_snapshot*` `mi_scavenger_stop` `mi_purge_holes_*` | not called by Bun | non-gaps for a rebase; relevant only to bun#30231 |

**What Bun would have to change**, per missing symbol:
- `mi_on_thread_idle` → they'd need a shim. Semantics per their own doc comment
  (`mimalloc_sys.rs:26–29`): "collects this thread's pending frees, **discards the free blocks
  inside its still-used pages**, and hands the arena purge to the scavenger." Two of those
  three clauses are hole-purging and the scavenger — neither exists in ours. A
  `mi_collect(false)` shim compiles but is **not** behaviorally equivalent. Note: the copied
  consumer slice contains only the declaration, no call site [V] — I could not measure call
  frequency, so the size of the regression is **[I]**.
- `mi_heap_dump_json` → this is a **user-facing Bun API**, not internal:
  `bun:jsc heapStats({dump:true|"blocks"}).mimallocDump`. `heapStats-mimalloc.test.ts`
  asserts the exact shape — `heaps[].seq`, `heaps[].pages[].{id,block_size,used,reserved,thread_id}`,
  `heaps[].blocks[[id,size]]`, and that `blocks` is absent in pages-only mode [V]. Rebasing
  onto ours deletes a shipped API and reds those tests. No workaround short of us porting it.

### 2b. `mi_option_t` ordering, `MI_MAX_ALIGN_SIZE`, `mi_heap_area_t`

| Item | Status | Evidence |
|---|---|---|
| **Option slots 0–46** | **SAME, byte-for-byte in order** | ours `include/mimalloc.h:473–520` vs `git show 6def7be9:include/mimalloc.h`, both: `…page_cross_thread_max_reclaim`(42), `allow_thp`(43), `minimal_purge_size`(44), `arena_max_object_size`(45), `arena_is_numa_local`(46) [V] |
| **Bun's hardcoded Rust enum (0–42)** | **SAME** | `mimalloc_sys.rs:127–171`, 43 variants, matches our 0–42 exactly. Bun's enum is *already* a partial mirror — their own header has 47 base options; theirs stops at 42 [V]. Names differ cosmetically (`eager_commit` vs our `deprecated_eager_commit` at 3; also 12/14/28) — numeric slots identical. Only `Option::show_errors` is ever passed at runtime [V]. |
| **Option slots 47+** | **DIVERGENT** | Bun: `snapshot_on_exit`(47) `prof_sample_rate`(48) `scavenger`(49) `purge_holes`(50) `purge_holes_eager_zero`(51) `purge_holes_min_interval`(52) `purge_holes_full_every`(53). Ours: `prof`(47) `prof_sample_rate`(48) `prof_bt_max`(49) `prof_accum`(50) `prof_seed`(51) `prof_max_bytes`(52) `memory_events`(53) `purge_zeroes`(54) [V]. |
| ↳ **slot-48 collision** | harmless, confusing | Both forks independently named slot 48 `prof_sample_rate` [V], with *different* semantics (Bun: bytes/sample, 0=off; ours: byte interval, compat alias). Anyone diffing headers will assume compatibility that isn't there. Worth renaming ours or documenting. |
| `MI_MAX_ALIGN_SIZE` | **SAME** = 16 | ours `include/mimalloc/types.h:38`; Bun's identical file/line [V]. Bun hardcodes `pub const MI_MAX_ALIGN_SIZE: usize = 16` at `mimalloc_sys.rs:182` [V] — matches. |
| `mi_heap_area_t` | **SAME**, field-for-field | ours vs Bun's `include/mimalloc.h` byte-identical struct; Bun's Rust mirror `mimalloc_sys.rs:97–107` matches both [V] |

Net: **the ABI Bun's FFI hardcodes is already compatible.** The enum divergence touches only
env-var names (`MIMALLOC_SCAVENGER`, `MIMALLOC_PURGE_HOLES`, `MIMALLOC_SNAPSHOT_PATH`,
`MIMALLOC_PROF_PATH`) and C-side callers, not their Rust bindings.

### 2c. Build defines and compilation model

Bun's `scripts/build/deps/mimalloc.ts` DirectBuild compiles **only `src/static.c`, as C++** [V].

| Define / mode | Status in ours | Evidence & required Bun change |
|---|---|---|
| `MI_STATIC_LIB` | **SAME** | `CMakeLists.txt` [V] |
| `MI_SKIP_COLLECT_ON_EXIT` | **SAME** | `src/init.c`, `CMakeLists.txt` [V] |
| `MI_MALLOC_OVERRIDE` | **SAME** | `src/alloc-override.c`, `src/prim/osx/alloc-override-zone.c` [V] |
| `MI_LIBC_MUSL` | **SAME (compiles)** | `include/mimalloc/prim-tls.h`, `CMakeLists.txt` [V]; **but no musl CI** — see §3 |
| `MI_DEFAULT_ALLOW_THP` | **SAME** | `src/options.c`, `CMakeLists.txt` [V] |
| `MI_DEBUG=3` | **SAME** | 26 files reference it; C++-compiles clean at 3 [V] |
| `MI_TRACK_ASAN` / `MI_UBSAN` / `MI_TRACK_VALGRIND` | **SAME** | `include/mimalloc/track.h`, `types.h`, `CMakeLists.txt` [V]; we additionally run a dedicated `asan.yml` job |
| `MI_BUILD_RELEASE` | **SAME** | [V] |
| **`MI_NO_PROCESS_DETACH`** | **MISSING** | zero hits in ours [V]. Bun sets it unconditionally (`mimalloc.ts:56`) — "MI_SKIP_COLLECT_ON_EXIT only skips the heap walk". Bun's impl is 5 lines: `src/init.c:734` + `src/prim/prim.c:44` + a CMake option [V]. **S-sized port; hard requirement.** Without it Bun's process teardown changes behavior. |
| **unity build of `src/static.c` as C++** | **SAME — verified by compiling** | I built ours with Bun's exact define set:<br>`c++ -x c++ -std=c++17 -fPIC -c src/static.c -Iinclude -DMI_STATIC_LIB -DMI_SKIP_COLLECT_ON_EXIT=1 -DMI_NO_PROCESS_DETACH=1 -DMI_BUILD_RELEASE -DMI_MALLOC_OVERRIDE -DMI_DEFAULT_ALLOW_THP=0 -DMI_PPROF={1,0}` → **exit 0 both** [V]; also clean with `-DMI_DEBUG=3` and `-DMI_LIBC_MUSL=1` [V]. Our new TUs (`profile*.c`, `dhat*.c`, `memory-events.c`, `diagnostic.c`) are all C++-clean and all reachable from `static.c` (`src/static.c:23–53`) [V]. **This is a non-gap — good news worth stating in the pitch.** |
| **`MI_PPROF` under DirectBuild** | **TRAP** | `MI_PPROF` is a CMake option (`CMakeLists.txt:70`, default ON → `-DMI_PPROF=1`). Bun's build never runs our CMake, so `MI_PPROF` is undefined. There is **no `#ifndef MI_PPROF` default anywhere** in `include/`/`src/` [V], so `#if MI_PPROF` evaluates to 0 and the profiler is **silently compiled out** (`src/alloc.c:115`, `src/free.c:33`). Confirmed empirically: compiling `static.c` with no `-DMI_PPROF` yields **zero** `_mi_prof_on_alloc` references in the object file [V]. Bun must add `MI_PPROF=1` **and** `-fno-omit-frame-pointer` (our CMake appends it at `CMakeLists.txt:432`, inside the `MI_PPROF` branch; their `cflags` list does not) or FP unwinding produces garbage stacks. Document this loudly. |
| `-ftls-model=local-dynamic` (musl) / `initial-exec` | **SAME** (compiler flag, not ours to provide) | `mimalloc.ts:109` [V] |

---

## 3. Behavioral gaps

| # | Gap | Class | Rationale (grounded in Bun's usage) |
|---|---|---|---|
| B1 | **Upstream base is 205 commits stale** (`bcee5a88` vs `6def7be9`) [V] | **BLOCKER** | Not one item among many — a **prerequisite**. Bun's `scavenger.c`, hole purging, atfork handlers and teardown work are written against `mi_page_map_t` (the 2-level restructure `d63979ae`) and `src/subproc.c`, **neither of which exists at our pin**. Ours still has the flat `_Atomic(mi_submap_t)* _mi_page_map` (`src/page-map.c:219–222`) [V] and has no `subproc.c` at all [V]. Nothing downstream can be cherry-picked before this. |
| B2 | **`mi_on_thread_idle` missing** | **BLOCKER** | Linked symbol; link failure on rebase. Semantics span the scavenger + hole purging (B3, B4). |
| B3 | **No background scavenger thread** (`src/scavenger.c`, 407 lines) [V] | **BLOCKER** | Bun ships it **on by default** (`mi_option_scavenger` default `1`, `src/options.c`) [V]. Their `mi_option_purge_delay` default is `100`ms vs upstream/our `1000`ms [V]. Losing it means freed arena memory only returns on the next allocation — a direct regression against `bun#39844` (heap peaks 1.4–2.6× Node), which is *why* they built it. |
| B4 | **No hole purging** (`src/page.c` +1038) [V] | **BLOCKER** | Also default-on (`mi_option_purge_holes` = 1) [V]. Half of `mi_on_thread_idle`'s contract. Their `test-purge-holes.c` is 1705 lines [V] — they consider it load-bearing. |
| B5 | **Heap delete/destroy teardown race protocol** | **BLOCKER** | `MimallocArena` does `mi_heap_new` per parse call and `mi_heap_destroy` on `Drop` (`MimallocArena.rs:102,201`) [V], across threads, at very high rate. Bun's `src/theap.c` is +346 and `src/heap.c` +61 against base [V], with `test-heap-teardown.c` (571), `test-heap-mt.c` (359), `test-heap-churn.c` (461), `test-heap-aba.c`, `test-heap-delete-race.c`, `test-park-handoff.c` (560) [V]. This is the single highest-risk area: it is the exact pattern Bun stresses and we do not. |
| B6 | **No `pthread_atfork`** | **BLOCKER for POSIX Bun** | Bun: `src/init.c:618` registers `_mi_process_fork_{prepare,parent,child}`, nested-fork-safe via `mi_fork_depth` (`src/subproc.c:333`) [V]. Ours: zero `pthread_atfork` hits [V]. That Bun built these handlers *and* a 250-line `test-fork-user-heap.c` is itself the evidence they need fork safety [V]; the specific runtime paths that fork are **[I]**. Classic deadlock-in-child hazard. MIMALLOC_FORKS.md rates this "3 — open, needs a POSIX repro". |
| B7 | **`MI_NO_PROCESS_DETACH` absent** | **BLOCKER (trivial)** | Bun sets it unconditionally [V]. 5-line port. |
| B8 | **`mi_heap_dump_json` / `mi_heap_get_seq` absent** | **BLOCKER** | Shipped `bun:jsc` API with an asserting test file [V]. |
| B9 | **Profiler hot path is not zero-cost-when-off** | **BLOCKER** | `src/alloc.c:116` calls `_mi_prof_on_alloc`, an **out-of-line function** (`src/profile.c:826`) that runs `prof_auto_start()` + an atomic load on *every* malloc [V]. Measured 11.75 → 20.00 ns/alloc, **+70%** (our issue #50; number from report 3, not re-measured here — **[I]** on the magnitude, **[V]** on the mechanism). Bun's design costs literally nothing when off: `theap->prof_force_slow` poisons `pages_free_direct` so only the generic path is touched, and frees are gated by the `MI_PAGE_HAS_PROF_SAMPLES` page flag (`src/free.c:163,172`, `types.h:354`) [V]. **Bun cannot dodge this with `MI_PPROF=0`** — bun#30231 exists precisely to ship a profiler. This must be fixed before any pitch. |
| B10 | **macOS zone introspection** — ours 497 lines vs Bun's 728 (+264 vs base) [V] | **IMPORTANT** | Bun's adds in-/out-of-process `memory_reader_t` introspection so `leaks`/`heap`/`vmmap` see the heap, and fork-safe zone locking. Not a link dependency; a tooling regression on a first-class Bun platform. |
| B11 | **macOS fixed TLS slots differ** — ours 108/109, Bun 96/97 [V] | **IMPORTANT** | `include/mimalloc/prim-tls.h:417–418` (ours) vs Bun's `:360–361`, which documents 96–97 as sitting in the never-assigned gap after `__PTK_FRAMEWORK_CORETEXT_KEY0` (95) [V]. Upstream churned 108/109 → 126/127 → back. Bun has production evidence for their choice; a silent slot collision is a corruption class. |
| B12 | **Bun's `stats.c` correctness fixes partly absent** | **IMPORTANT** | Diffing `bun-consumer/upstream_stats.c` (= base `6def7be9`) vs `src_stats.c` (= Bun), 148 changed lines [V]. Three fixes: **(a)** a racy `prev_total == peak` compare in `mi_stat_adjust_mt`, which Bun removed — **not a gap today**: upstream *added* it after our pin, and our `src/stats.c:69–79` has neither the compare nor the peak adjust [V]. Same shape as B18: **the B1 bump inherits the bug, so carry Bun's removal with it.** **(b)** `_mi_stats_print` prints from a **snapshot** (`mi_stats_add` into a local) because "heap and subproc statistics are updated concurrently by other threads" — **real gap**, ours prints directly from the live struct (`src/stats.c:353`) [V]. **(c)** `mi_stats_copy` switched from `_mi_memcpy` to counter-at-a-time to avoid torn reads — **real gap**, ours still memcpys (`src/stats.c:604`) [V]. Bun has an **open bug on exactly this surface** (`oven-sh/bun#28630`, `MIMALLOC_SHOW_STATS=1` broken on Linux). |
| B13 | **musl support unproven** — `MI_LIBC_MUSL` compiles, **no Alpine/musl CI job** [V] | **IMPORTANT** | Bun ships musl builds. Our `.github/workflows/` has no musl/Alpine C-engine job [V]. Our FP-unwind path already needs the "no `execinfo.h` on musl" guard Bun documents. Compiling is not evidence. |
| B14 | **Windows PRNG / RAM-sizing fixes, NUMA fix** (Bun `src/prim/windows/prim.c` +78) [V] | **IMPORTANT** | Windows is a priority platform for both. Their fixes are small and self-contained; we should audit-and-take rather than rediscover. |
| B15 | **`purge_delay` default 1000 vs Bun's 100** [V] | **IMPORTANT** | Cheap to match; ties to `bun#34217`. **Correction to report 1:** the claimed `arena_purge_mult 10→1` change is **wrong** — `git show 6def7be9:src/options.c:149` already has `1`, same as ours [V]. Only `purge_delay` differs. |
| B16 | **Heap snapshot + `mi-heapview`** (`src/heap-snapshot.c` 434, `tools/mi-heapview.c` 781) [V] | **NICE-TO-HAVE** | Not called by Bun today; `mi_option_snapshot_on_exit` is opt-in (default 0) [V]. Losing it costs a debug tool, not a shipped feature. |
| B17 | **Test corpus** — Bun adds ~7,300 lines across **17 new** test files incl. `test-commit-fail.c` (fault injection), `test-prof-adversarial.c`, `test-emulated-tls.c`, `test-thp-optout.c` [V] | **IMPORTANT** | Not a runtime gap, but Bun will not rebase onto a fork that can't run their regression suite. We've imported exactly 2 adversarial cases so far (MIMALLOC_FORKS.md:320). |
| B18 | **glibc 2.44 startup segfault fix** (`7ac561ab`) | **NOT A GAP TODAY** [V] | Correcting the brief: our `src/page-map.c:219` declares `static mi_page_t* mi_submap_empty[1] = { NULL };` with `mi_page_map_empty[1]` pointing at it — a *real* table, so `_mi_unchecked_ptr_page(NULL)` (`include/mimalloc/internal.h:777–781`) reads `mi_submap_empty[0]` = NULL and returns cleanly [V]. Bun's bug was **introduced upstream by `d63979ae`** (the `mi_page_map_t`/`submaps[]` restructure) which we predate. **It becomes a gap the moment we do the B1 bump** — carry `7ac561ab` + `test-free-before-init.c` with it. |
| B19 | **2-level pagemap, `subproc.c`, improved checked free** | folded into **B1** | Subset of the 205 commits [V]. |
| B20 | **`mi_page_map_get_idx` over-count** (Bun `0e150b5c`) | **NICE-TO-HAVE** | MIMALLOC_FORKS.md:325 rates 2; corruption not reproducible. But it is a fix *inside the 2-level pagemap* — i.e. only relevant post-B1. |

Non-gaps worth noting for the pitch: `os_tag` default is **240 in both** forks
(`src/options.c:143` in each) [V] — we and Bun independently made the same VM-tag change.

---

## 4. Where ours exceeds Bun's

| Area | Ours | Bun |
|---|---|---|
| Profiler API | **17** exported entry points — `mi_decl_export` count in `include/mimalloc/profile.h` is 17, and the distinct `mi_prof_*` functions are exactly 17 [V]: `start`/`start_seeded`/`start_ex`/`stop`/`is_enabled`/`dump`/`dump_writer`/`dump_proto`/`dump_proto_writer`/`reset`/`stats_get`/`visit`/`snapshot_new`/`snapshot_visit`/`snapshot_free`/`modules_visit`/`debug_stats` | 5 (`mi_prof_enable`/`reset`/`dump`/`dump_buf`/`dump_to_file`) [V] |
| Sampling draw | **Uniform on [1, 2×rate)** — `prof_random(owner) % (rate*2)`, `src/profile.c:193` [V]. Mean = rate; **not geometric.** Better than Bun's strictly fixed countdown (no periodic-pattern lock-in), but *not* the exponential draw Go/jemalloc use. State it exactly this way — Bun's `src/prof.c:214` explicitly says "Fixed rate. (Go/jemalloc use a geometric draw to avoid bias with periodic…)", so a "we do geometric" claim would be caught immediately. | fixed rate |
| Output formats | legacy text **and** uncompressed pprof proto, plus streaming writers | uncompressed `.pb` only |
| Determinism | `mi_option_prof_seed` + per-thread `thread_seq` streams, `test-prof-seed-determinism.c` [V] | none |
| Budgeting | `mi_option_prof_max_bytes`, fail-soft drop-sample on budget exhaustion (`src/profile.c` `_mi_prof_arena_alloc`) [V] | unbudgeted |
| **DHAT** | exact observer, DHAT-v2 JSON, independent of `MI_PPROF`, `src/dhat.c` 504 lines, `test-dhat.c` [V] | absent |
| **memory-events** | always-compiled opt-in callback table + live visitor + `mi_unwrapped_{malloc,free,realloc}` (`src/memory-events.c` 421) [V] | absent |
| **Rust crate** | published `mimalloc-pprof` 0.9.5 (`GlobalAlloc`, prof, dhat) | none |
| CI breadth | real **pprof CLI validation** on linux-gnu x64 / win-msvc / win-gnu; `fuzz.yml` (incl. a plant-a-bug negative control); `benchmark-{latency,memory,scaling,stats,sentinel}.yml`; `zero-tracking.yml`; `amalgamation-drift.yml`; `asan.yml`; memory-gate + leak-injection job [V, `.github/workflows/`] | one `test.yaml` |
| Concurrency lesson we already gave back | our `_mi_prof_on_alloc` moved the sampling decision out from under a global `prof_lock` after comparing against Bun's thread-local countdown (`src/profile.c:838` comment) [V] | — |
| macOS VM tag | 240 [V] | 240 [V] — tie |

Also true and worth saying: **`src/static.c` compiles clean as C++ under Bun's exact define
set** [V], so the integration mechanics are already proven.

---

## 5. Recommended closure order

CLAUDE.md rule 2 applies throughout: **never mix `src/`|`include/`|`test/`|`CMakeLists.txt`
with `rust/` in one commit.** Every item below is C-core-only unless marked. CLAUDE.md rule 1:
branch → PR → merge, one PR per phase.

**Required before any pitch:**

| # | Item | Size | Notes |
|---|---|---|---|
| 1 | **Bump the overlay pin `bcee5a88 → ≥6def7be9`** (205 commits) | **L** | Hard prerequisite for 3–7. Per CLAUDE.md this needs a reviewed selective C-engine overlay with byte-identical hook re-verification — the hooks currently apply only against the pinned base, and the 2-level pagemap restructure (`d63979ae`) rewrites `page-map.c` under them. Carry Bun's `7ac561ab` (glibc 2.44) + `test-free-before-init.c` in the same PR, since the bump *creates* that bug. **CLAUDE.md's line "the current tip fixes nothing we need" is now false for this goal and must be revised.** |
| 2 | **Zero-cost-when-off profiler fast path** (issue #50) | **M** | Adopt or match Bun's `pages_free_direct` poisoning + `MI_PAGE_HAS_PROF_SAMPLES` free gating. Can be done *before* item 1 if desired; independent of the base. Gate acceptance on `ci/dev_linux.py bench` returning to ≈11.75 ns. |
| 3 | **`MI_NO_PROCESS_DETACH`** | **S** | 5 lines + CMake option; port from Bun's `src/init.c:734` / `src/prim/prim.c:44`. |
| 4 | **`mi_heap_dump_json` + `mi_heap_get_seq`** | **M** | Must reproduce Bun's exact JSON shape — `heapStats-mimalloc.test.ts` is the spec. Also take their three `stats.c` correctness fixes (B12) here; they're in the same file and probably fix `bun#28630`. |
| 5 | **`pthread_atfork` handlers** | **M** | Port the lock-ordering skeleton + `threadlocal.c` handlers only (Bun's version is entangled with the scavenger). Their documented rule: locks held across an allocation are taken before `arena_reserve_lock`. Needs a Linux/macOS repro first (MIMALLOC_FORKS.md:324). |
| 6 | **Heap teardown/delete race protocol + Bun's heap test corpus** | **L** | `test-heap-teardown.c`, `test-heap-mt.c`, `test-heap-churn.c`, `test-heap-aba.c`, `test-heap-delete-race.c`, `test-park-handoff.c`, `test-commit-fail.c`. Import the tests *first*, watch them fail, then fix. This is what `MimallocArena` actually exercises. |
| 7 | **Scavenger thread + hole purging + `mi_on_thread_idle`** | **L** | The three are one feature from Bun's perspective. MIMALLOC_FORKS.md rates each 3 and flags the profiler-vs-background-purger race — that interaction must be designed, not ported. Default-on to match Bun, or the memory regression stands. |
| 8 | **musl/Alpine CI job for the C engine** | **S–M** | Otherwise B13 is an unbacked claim. |
| 9 | **`purge_delay` default 1000 → 100** | **S** | One line. Do it with 7, not before (it interacts with the scavenger). |

**Optional but strengthens the pitch:**

| # | Item | Size |
|---|---|---|
| 10 | macOS zone introspection (`memory_reader_t`, fork-safe zone locking) | **M** |
| 11 | macOS TLS slots 108/109 → 96/97, with Bun's justification | **S** |
| 12 | Windows PRNG/RAM-sizing + NUMA fixes | **S** |
| 13 | Rename our `mi_option_prof_sample_rate` (slot-48 collision) or document it | **S** |
| 14 | Heap snapshot + `mi-heapview` | **L**, low value — Bun doesn't call it |
| 15 | Rust-crate bindings for memory-events (**`rust/` only — separate commit**) | **M** |

**Not worth doing:** `mi_page_map_get_idx` over-count (B20) beyond an upstream bug report.

---

## 6. Honesty check on `MIMALLOC_FORKS.md`

| Line | Claim | Status |
|---|---|---|
| 78 | "…noting in-source that Go and jemalloc use a geometric draw to avoid periodic-pattern bias — **which this fork already does**." | **FALSE.** `src/profile.c:193` draws `prof_random() % (rate*2)` — **uniform on [1, 2×rate)**, not geometric/exponential [V]. Correct wording: *"which this fork mitigates with a uniform-jitter draw of mean `rate`, though not the exponential draw Go and jemalloc use."* This is the single most quotable error in the doc and Bun would spot it instantly, since the sentence cites their own source comment. |
| 58 | "**78 commits ahead of `dev3`**, 46 files, 4 new source files, 13 new tests." | **STALE.** As of `942b8342`: **137 commits** ahead of merge-base `6def7be9`, of which **60** are non-Microsoft [V]; **59 files**, +11326/−404; **4 new src files** (`prof.c`, `scavenger.c`, `heap-snapshot.c` + upstream's `subproc.c` which is *not* theirs); **17 new** test files (not 13); `test-stress.c` is modified, not new [V]. Also the fork was rebased again on 2026-08-25 (bun PR #40409). |
| 58 | "Actively rebased (onto v3.4.3+ as of 2026-07-30)" | **STALE** — base is now `6def7be9` (2026-08-09), v3.5.0 line [V]. |
| 60 | `src/scavenger.c` (+369) | **STALE** — 407 lines [V]. |
| 61 | `src/prof.c` (+686) | **STALE** — 719 lines [V]. |
| 62 | `src/heap-snapshot.c` (+432), `tools/mi-heapview.c` (+781) | 434 / 781 [V] — heapview exact, snapshot off by 2. |
| 63 | `src/page.c` (+1011/−3); "`mi_option_page_drain_sparse` stops re-seeding" | +1038 [V]. **No `mi_option_page_drain_sparse` exists** in `942b8342`'s option table — the hole-purging options are `purge_holes`, `purge_holes_eager_zero`, `purge_holes_min_interval`, `purge_holes_full_every` [V]. Either renamed or the claim was wrong. |
| 66 | "macOS … fixed TLS slots 175/176 → 96/97" | Destination 96/97 confirmed [V]; "175/176" is unverified and inconsistent with upstream's documented 108/109 → 126/127 → 108/109 churn (which the same doc states at its portability section). Our tree is at 108/109 [V]. Fix or drop the "175/176". |
| 67 | Test suite listing | Undercounts — add `test-heap-teardown.c` (571), `test-park-handoff.c` (560), `test-heap-churn.c` (461), `test-heap-aba.c`, `test-emulated-tls.c`, `test-thp-optout.c`, `test-free-before-init.c`, `test-theap-sentinel.c` [V]. |


**Corrections to reports 1–3 (not MIMALLOC_FORKS.md errors), so they don't propagate:**

- Report 1: "`arena_purge_mult` 10→1" is **FALSE** — upstream `6def7be9` already ships `1`, and so do we [V]. Only `purge_delay` (1000 vs 100) differs.
- Report 2: the consumer symbol set is **32**, not 33 — `mi_heap_get_seq` is internal to Bun's `mi_heap_dump_json`, not linked by Bun's Rust/C++ [V].
- Report 3: our profiler exports **17** entry points [V]; `_mi_prof_on_alloc`'s +70% figure is report 3's measurement, re-confirmed here only as to mechanism.

---

## 7. Draft GitHub issue for `oven-sh/bun` — **DO NOT FILE UNTIL §5 ITEMS 1–9 ARE MERGED**

> **Title:** `mimalloc: possible consolidation with zackees/mimalloc-pprof (v3 fork with pprof sampling profiler)`

---

We maintain [zackees/mimalloc-pprof](https://github.com/zackees/mimalloc-pprof), a v3 fork of
mimalloc that adds a pprof-compatible sampling heap profiler. While surveying mimalloc forks we
found `oven-sh/mimalloc@bun-dev3-v2` and realized we had independently built the same core
thing on the same `dev3` base — including the same invariant that profiler-internal memory must
come from the raw OS layer (`_mi_os_alloc`), never from hooked allocation paths. Your fork got
there first and is the more mature allocator work. We're opening this to ask whether
consolidating is worth your time, not to propose that you adopt anything on faith.

**What we've verified about compatibility.** We diffed our tree against `942b8342` and against
your consumer surface (`mimalloc_sys.rs`, `MimallocArena.rs`, `MimallocWTFMalloc.h`,
`BunJSCModule.h`, `scripts/build/deps/mimalloc.ts`):

- Every `mi_*` symbol Bun links is present, with matching signatures.
- `mi_option_t` slots 0–46 are identical to your base; your Rust `#[repr(u32)] Option` enum
  (0–42) matches exactly. `MI_MAX_ALIGN_SIZE` = 16 and `mi_heap_area_t` are byte-identical.
- `src/static.c` compiles clean as C++ under your exact DirectBuild define set
  (`MI_STATIC_LIB`, `MI_SKIP_COLLECT_ON_EXIT`, `MI_NO_PROCESS_DETACH`, `MI_MALLOC_OVERRIDE`,
  `MI_DEFAULT_ALLOW_THP=0`, `MI_DEBUG=3`, `MI_LIBC_MUSL=1`).
- We've ported the behavior you depend on that we lacked: `mi_on_thread_idle`,
  `mi_heap_dump_json`/`mi_heap_get_seq` (matching the JSON shape
  `heapStats-mimalloc.test.ts` asserts), `MI_NO_PROCESS_DETACH`, `pthread_atfork` handlers,
  the scavenger, hole purging, and your heap-teardown test corpus, which we run in CI.

**What you'd gain over your current fork:** a richer profiler API (streaming writers, text +
proto output, `mi_prof_visit`/snapshot iteration, a seeded deterministic mode with a
regression test, and a byte budget with fail-soft sample dropping); a uniform-jitter sampling
draw rather than a fixed countdown, which avoids periodic-pattern lock-in — though not the
exponential draw Go and jemalloc use, as your `src/prof.c` correctly notes; an exact DHAT-v2
observer; an opt-in allocation-event callback API; and CI that validates dumps with the real
`pprof` CLI on linux-gnu, win-msvc and win-gnu, plus fuzzing, ASan, and latency/RSS gates.

**What you'd need to change:** `mi_option_t` slots 47+ differ, so the env-var names for your
scavenger/hole-purge/snapshot options move; your build must add `MI_PPROF=1` and
`-fno-omit-frame-pointer` (our profiler is behind a compile-time flag your DirectBuild doesn't
set). Note we independently named slot 48 `prof_sample_rate` with different semantics — we'll
rename ours if that's confusing.

**We are not asking for a decision here.** If this is interesting we'll do the work of building
a branch against your consumer code and running your test suite, and report back. If it isn't
worth the churn, we'd still value a pointer to anything in our approach you already tried and
rejected.

---
*(≈430 words.)*
