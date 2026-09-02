# Gap matrix refresh: 2026-09-02 (issue #274, Bun parity P9b)

Re-runs the four checks that produced `docs/bun-gap-analysis-2026-09-01.md` against
Bun's **current** pin, and re-derives the consumer-surface status against everything
that has merged since (#276, #281, #284, #286, #289, #291, #297; #299 and #302 open).
Every claim below marked **[V]** was re-verified in this session against the raw
sources named; **[I]** is inference from verified facts.

---

## 1. Bun's pin has not moved

`oven-sh/bun@main`'s `scripts/build/deps/mimalloc.ts` (fetched 2026-09-02 via
`gh api repos/oven-sh/bun/contents/scripts/build/deps/mimalloc.ts`) still pins:

```
const MIMALLOC_COMMIT = "942b8342575bdece649438ca76f32276a019c51e";
```

Identical to the 2026-09-01 analysis's base **[V]**. The DirectBuild `defines`/`cflags`
in that file are unchanged too: `MI_STATIC_LIB`, `MI_SKIP_COLLECT_ON_EXIT=1`,
`MI_NO_PROCESS_DETACH=1`, `MI_BUILD_RELEASE` (release only), `MI_MALLOC_OVERRIDE`
(Linux, non-ASan only), `MI_DEFAULT_ALLOW_THP=0` (Linux), `MI_LIBC_MUSL=1` +
`-ftls-model=local-dynamic` (musl), `lang: "cxx"` (compiles `src/static.c` as C++)
**[V]**.

**Consumer-surface diff, byte-for-byte, against the files cached from the 2026-09-01
session:**

| File | Path | Diff vs 2026-09-01 |
|---|---|---|
| `mimalloc_sys.rs` | `src/mimalloc_sys/mimalloc.rs` | **zero-byte diff** [V] |
| `MimallocArena.rs` | `src/bun_alloc/MimallocArena.rs` | **zero-byte diff** [V] |
| `MimallocWTFMalloc.h` | `src/jsc/bindings/MimallocWTFMalloc.h` | **zero-byte diff** [V] |
| `BunJSCModule.h` | `src/jsc/modules/BunJSCModule.h` | **zero-byte diff** [V] |
| `heapStats-mimalloc.test.ts` | `test/js/bun/jsc/heapStats-mimalloc.test.ts` | **zero-byte diff** [V] |

**Conclusion: no new gap exists at Bun's current pin.** The 32-symbol consumer set, the
`mi_option_t` slot-0-42 mapping, `MI_MAX_ALIGN_SIZE`, and the `mi_heap_area_t` layout
from the 2026-09-01 analysis are re-verified current as of this session — re-grepped
directly from the freshly fetched files, not assumed. No sub-issue filed under this
phase: 9b's instruction is "if Bun added something we lack, open a sub-issue" and
nothing was added.

One correction to the 2026-09-01 issue text: it names the test file path as
`test/js/bun/util/heapStats-mimalloc.test.ts`; the real path is
`test/js/bun/jsc/heapStats-mimalloc.test.ts` (`util` does not exist in the tree) — noted
in PR #286 already, repeated here since 9b explicitly asks for a fresh read.

---

## 2. Gap status vs. the 2026-09-01 closure order

`main` moved from the `bcee5a88` pin to `6def7be9` and merged seven of the nine
"required before any pitch" items (#5 §5) since the last analysis. Status below is
re-derived from the actual repo state (grep/ls on `main`, not PR descriptions alone),
cross-checked against each PR's body for the items that are hard to verify by
inspection (e.g. what a "snapshot-safe stats printing" fix actually touches).

### Required-before-pitch items (2026-09-01 §5, items 1-9)

| # | Item | 09-01 status | 09-02 status | Evidence |
|---|---|---|---|---|
| 1 | Bump overlay pin `bcee5a88 → ≥6def7be9` | OPEN (blocker) | **DONE — #276** | `CLAUDE.md` pin note now reads `6def7be9`; PR #276 title: "bump upstream overlay pin to 6def7be9 (Bun merge-base) + glibc 2.44 free(NULL) fix" |
| 2 | Zero-cost-when-off profiler fast path (issue #50) | OPEN | **DONE — #281** | PR #281: "zero-cost-when-off fast path; default MI_PPROF=1 in headers" |
| 3 | `MI_NO_PROCESS_DETACH` | OPEN (S) | **DONE — #284** | `grep -rn MI_NO_PROCESS_DETACH src/ include/` hits `src/init.c`, `CMakeLists.txt` [V] |
| 4 | `mi_heap_dump_json` + `mi_heap_get_seq`, + B12 stats.c fixes | OPEN | **DONE — #286** | `include/mimalloc-stats.h:168` declares `mi_heap_dump_json`; PR body confirms both `_mi_stats_print` snapshot fix and `mi_stats_copy` counter-at-a-time fix landed in the same PR |
| 5 | `pthread_atfork` handlers | OPEN | **DONE — #289** | PR #289: "pthread_atfork fork-safety handlers with lock-order contract" |
| 6 | Heap teardown/delete race protocol + Bun's heap test corpus | OPEN (L) | **DONE — #291** | `test-commit-fail.c`, `test-heap-teardown.c`, `test-heap-mt.c`, `test-heap-churn.c`, `test-heap-aba.c`, `test-heap-delete-race.c` all present in `test/` [V]; PR #291 reports 20/20 clean in Debug+FULL |
| 7 | Scavenger thread + hole purging + `mi_on_thread_idle` | OPEN (L) | **OPEN — PR #299 (7a, scavenger) + PR #302 (7b, holes), both open** | `grep mi_on_thread_idle include/ src/` returns nothing on `main` [V]; `src/scavenger.c` and `src/page-holes.c` do not exist on `main` yet — they exist only on the `bun-parity/p7-scavenger` / stacked `bun-parity/p7b-holes` branches |
| 8 | musl/Alpine CI job | OPEN (S-M) | **DONE — #297** | `.github/workflows/c-unit.yml` job `ctest-musl`, container `alpine:3.20` [V] |
| 9 | `purge_delay` default 1000 → 100 | OPEN (S) | **OPEN — tied to #299** | `src/options.c:140`: `{ 1000, ... purge_delay ... }` — still 1000 on `main` [V]; PR #299's description says it flips to 100 when that PR lands |

**7/9 done.** The remaining two (#7, #9) are the same blocker: both live in the P7a/P7b
stack, not yet merged.

### Optional-but-strengthens-the-pitch items (2026-09-01 §5, items 10-15)

| # | Item | 09-01 status | 09-02 status | Evidence |
|---|---|---|---|---|
| 10 | macOS zone introspection (`memory_reader_t`, fork-safe zone locking) | OPEN | **OPEN** | `src/prim/osx/alloc-override-zone.c` is 519 lines [V] — unchanged from the 2026-09-01 count (497 was the pre-#276-bump figure; 519 reflects the B1 upstream bump, not new introspection work). Bun's is 728 lines; the gap is unaddressed |
| 11 | macOS TLS slots 108/109 → 96/97 | OPEN | **DONE — #297** | PR #297 title: "...macOS TLS slots 96/97 (#273)" |
| 12 | Windows PRNG/RAM-sizing + NUMA fixes | OPEN | **DONE — #297** | Same PR; `src/prim/windows/prim.c` shows a diff in the fast-forward log that produced this session's `main` (`+73` lines) |
| 13 | Rename `mi_option_prof_sample_rate` (slot-48 collision) or document it | OPEN | **OPEN** | `include/mimalloc.h:547` still names our slot-48 option `mi_option_prof_sample_rate`; PR #299 appends `scavenger` *after* `purge_zeroes` without renumbering, so this collision is untouched and still needs a documentation call (or a rename) before 9c |
| 14 | Heap snapshot + `mi-heapview` | OPEN (low value) | **OPEN, not attempted** | `src/heap-snapshot.c`, `tools/mi-heapview.c` still present, untouched; Bun doesn't call it — no action needed for 9c |
| 15 | Rust-crate bindings for memory-events | OPEN | **OPEN, not attempted** | `rust/` only; out of scope for the C-core-only PRs that have landed |

### Behavioral gap IDs (2026-09-01 §3, B1-B20) — compact cross-reference

| ID | 09-01 | 09-02 | PR / evidence |
|---|---|---|---|
| B1 (205-commit stale base) | BLOCKER | **DONE** | #276 |
| B2 (`mi_on_thread_idle` missing) | BLOCKER | **OPEN** | #299 |
| B3 (no scavenger thread) | BLOCKER | **OPEN** | #299 |
| B4 (no hole purging) | BLOCKER | **OPEN** | #302 |
| B5 (heap teardown race protocol) | BLOCKER | **DONE** | #291 |
| B6 (no `pthread_atfork`) | BLOCKER | **DONE** | #289 |
| B7 (`MI_NO_PROCESS_DETACH` absent) | BLOCKER | **DONE** | #284 |
| B8 (`mi_heap_dump_json`/`mi_heap_get_seq` absent) | BLOCKER | **DONE** | #286 |
| B9 (profiler hot path not zero-cost-when-off) | BLOCKER | **DONE** | #281 |
| B10 (macOS zone introspection) | IMPORTANT | **OPEN** | not started |
| B11 (macOS TLS slots) | IMPORTANT | **DONE** | #297 |
| B12 (`stats.c` correctness fixes) | IMPORTANT | **DONE** | folded into #286 |
| B13 (musl/Alpine CI) | IMPORTANT | **DONE** | #297 |
| B14 (Windows PRNG/RAM/NUMA fixes) | IMPORTANT | **DONE** | #297 |
| B15 (`purge_delay` 1000→100) | IMPORTANT | **OPEN** | tied to #299 |
| B16 (heap snapshot + mi-heapview) | NICE-TO-HAVE | **OPEN, not planned** | Bun doesn't call it |
| B17 (Bun's ~7,300-line test corpus) | IMPORTANT | **PARTIAL** | 6 of the named heap/commit-fail tests imported (#291); `test-park-handoff.c`, `test-theap-sentinel.c`, `test-purge-holes.c`, `test-prof-adversarial.c`, `test-emulated-tls.c`, `test-thp-optout.c` still absent — most are tied to #299/#302 |
| B18 (glibc 2.44 startup segfault fix) | NOT A GAP TODAY → becomes one at B1 | **DONE** | carried in #276 alongside the pin bump, as flagged |
| B19 (2-level pagemap, `subproc.c`) | folded into B1 | **DONE** | #276 |
| B20 (`mi_page_map_get_idx` over-count) | NICE-TO-HAVE | **OPEN, not planned** | low value, unchanged |

**Summary counts (B1-B20, 20 items):** **11 DONE**, **8 OPEN**, **1 PARTIAL**, **0 NEW**.

---

## 3. What's still blocking 9c (filing the Bun issue)

Per issue #274's own acceptance criteria, 9c requires 9a green on every platform (not
yet — `mi_on_thread_idle` doesn't exist on `main`) and 9b showing zero new gaps (true,
per §1 above, but that is a *necessary*, not *sufficient*, condition — the remaining
OPEN items above are pre-existing gaps, not new ones, but they are still gaps). The
draft issue text in `docs/bun-gap-analysis-2026-09-01.md` §7 asserts "we've ported the
behavior you depend on that we lacked: `mi_on_thread_idle`, ... the scavenger, hole
purging" — **that sentence is not yet true** and must not be posted until #299 and #302
merge. This session does not touch 9c (explicitly out of scope) and files no new
sub-issues (no new gaps found).

**Remaining before 9c can proceed**, in order:
1. #299 (scavenger + `mi_on_thread_idle`, 7a) merges.
2. #302 (hole purging, 7b, stacked on #299) merges.
3. `purge_delay` flips 1000 → 100 (bundled with #299 per its PR description).
4. `ci/check_bun_surface.py` (this phase's 9a deliverable) goes from "1 missing symbol,
   `continue-on-error: true`" to a clean, hard-gated pass on ubuntu + alpine — the
   `bun-surface` CI job's dated TODO comment marks exactly this transition.
5. Item 13 above (slot-48 `prof_sample_rate` collision) gets a documentation call or a
   rename before the draft issue text can claim it as "we'll rename ours if that's
   confusing" without being aspirational.
6. Optional strengthening items 10 (macOS zone introspection) and B17's remaining test
   imports are not blockers for 9c — they were never on the required list — but the
   draft issue's "we run your heap-teardown test corpus in CI" claim should be checked
   against exactly which files import cleanly before that sentence is repeated verbatim.

No `MIMALLOC_FORKS.md` changes are needed this session: PR #275 already corrected the
document's stale claims (see its title, "correct stale Bun fork claims from the parity
gap analysis"), and this refresh found no further drift in that file specifically — the
09-01 analysis's §6 corrections were about the fork's own state, which #275 addressed
separately from consumer-surface parity.
