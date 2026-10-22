/* The free-arena reclaim -- `mi_purge_all_ex(MI_PURGE_RECLAIM)` (docs/arena-reclaim.md).

   WHAT THIS PROVES

   A1  the workload really reserves extra arenas (the precondition of every row below);
   A2  WITHOUT the flag nothing is ever released: the same call with the same live set leaves
       the arena accounting byte-for-byte unchanged (positive control for the flag itself);
   A3  with the flag, every arena that is completely free is released, the live objects in the
       arenas that are NOT free survive byte-for-byte, `arenas_kept == 0` (no arena was left
       behind that the reclaim could have released), and the `committed` statistic loses the
       released arenas' own commit -- never the reservation, and never more than it holds;
   A4  the accounting moves by exactly the reported `arena_reclaim_bytes`, and after the last
       live object is gone the remaining arenas go too -- i.e. the metadata follows the LIVE set
       and not the process's peak;
   A5  (MI_DEBUG only) the safety rule: a thread that is INSIDE the allocator -- here a worker
       holding a page pinned in `mi_heap_delete` through the exported #271 stall hook -- makes
       the reclaim refuse to run at all (`reclaimed == false`, nothing released, accounting
       unchanged), and it runs for real once that thread is out;
   A6  a live but PARKED worker (mi_on_thread_idle_start) does NOT block the reclaim: the claim
       protocol reaches it, which is the whole point of doing this through #366's park states.

   HOW THE ARENAS ARE FORCED

   `mi_option_arena_reserve` is in KiB (`mi_option_has_size_in_kib`, src/options.c) with a 1 GiB
   default; the row lowers it to its 32 MiB minimum. `mi_arena_reserve` then over-reserves by
   `MI_ARENA_MAX_CHUNK_OBJ_SIZE` (32 MiB), so a 32 MiB object's page -- 513 slices, one more than
   the 512-slice b-bitmap chunk -- reserves 96 MiB, and a second such object does NOT fit beside
   it: a request larger than one chunk is met from whole free chunks, and after one object at
   most one chunk is left. Each big object therefore gets an arena of its own, and the first
   small allocation in the process likewise reserves an arena (`mi_arena_reserve` runs for it
   too). A1 MEASURES that size per object instead of hardcoding it, so the rows below stay exact
   on any platform. Process-wide state (the scavenger, anything the C++ runtime allocated) is
   not assumed away either: the rows assert on DELTAS and on the live objects, and A1 pins the
   one precondition it does need -- "the process had no arena yet" -- by name, so a failure
   there is diagnosable rather than mysterious.

   Threading is modelled on test-purge-all.cpp (platform threads, never <thread>: some
   mingw-w64 toolchains ship the win32 threads model). A hang fails through ctest's TIMEOUT. */

#include <mimalloc.h>
#include <mimalloc-stats.h>   // mi_stats_get: the `committed` statistic the release debits

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

// `internal.h` declares the library's own entry points without an `extern "C"` block (every
// other white-box test is C, or reaches only its `static inline`s); this test measures the
// arena accounting through `_mi_purge_holes_report_collect`, so it needs the C linkage -- but
// only in the default build, where the library itself is C. With MI_USE_CXX the library is
// compiled as C++, its internal declarations are C++-mangled, and CMake passes MI_USE_CXX on
// to this target so that the wrapper is dropped and the two agree. The standard C++ headers
// must be included FIRST: `internal.h` pulls in `<atomic>` itself, and that cannot happen
// from inside an `extern "C"` block.
#if !defined(MI_USE_CXX)
extern "C" {
#endif
#include "mimalloc/internal.h"   // _mi_purge_holes_report_collect: the arena accounting measured below
#if !defined(MI_USE_CXX)
}
#endif

#if defined(_WIN32)
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

#if defined(_WIN32)
typedef HANDLE thread_t;
typedef DWORD(WINAPI* thread_fun_t)(void*);
#define THREAD_RET DWORD WINAPI
static bool thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  *t = CreateThread(NULL, 0, fn, arg, 0, NULL);
  return (*t != NULL);
}
static bool thread_join(thread_t t) {
  const DWORD waited = WaitForSingleObject(t, INFINITE);
  DWORD code = 0;
  const BOOL got = GetExitCodeThread(t, &code);
  CloseHandle(t);
  return (waited == WAIT_OBJECT_0 && got && code == 0);
}
#else
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
static bool thread_start(thread_t* t, thread_fun_t fn, void* arg) { return (pthread_create(t, NULL, fn, arg) == 0); }
static bool thread_join(thread_t t) { void* r = NULL; const int rc = pthread_join(t, &r); return (rc == 0); }
#endif

static void yield_cpu(void) {
#if defined(_WIN32)
  SwitchToThread();
#else
  sched_yield();
#endif
}

// ---------------------------------------------------------------------------------------------
// constants and helpers
// ---------------------------------------------------------------------------------------------

#define ARENA_RESERVE_KIB   ((size_t)32 * 1024)          // `mi_option_arena_reserve` takes KiB (32 MiB = MI_ARENA_MIN_SIZE)
#define BIG_BYTES           ((size_t)32 * 1024 * 1024)   // = MI_ARENA_MAX_CHUNK_OBJ_SIZE: one object per arena
#define BIG_COUNT           4

static int failures = 0;

static void check(bool ok, const char* what) {
  printf("  %-62s %s\n", what, (ok ? "ok" : "FAILED"));
  if (!ok) { failures++; }
}

// The arena accounting the audit reads: reserved address space and the arenas' own metadata.
typedef struct arena_accounting_s {
  size_t reserved;      // `arena_reserved_bytes`: total arena address space
  size_t meta;          // `arena_meta_bytes`: the info blocks (mi_page_t tables + bitmaps)
  size_t committed;     // commit-bitmap bytes (an upper bound on charged commit)
} arena_accounting_t;

static arena_accounting_t accounting(void) {
  mi_holes_report_t rep;
  _mi_purge_holes_report_collect(&rep);
  arena_accounting_t a; a.reserved = rep.arena_reserved_bytes; a.meta = rep.arena_meta_bytes;
  a.committed = rep.arena_committed_bytes;
  return a;
}

// The sub-process's `committed` statistic (the exported accessor, not the internals). Releasing an
// arena must take away the memory that arena itself held committed -- above all its info block,
// i.e. exactly the metadata this phase exists for -- and the reservation is a different number: on
// Windows a reservation that was never committed is not in `committed` at all, so debiting the
// whole reservation (what `mi_arenas_unsafe_destroy` does, harmlessly, at process exit) walks a
// live process's statistic past zero and leaves it corrupt for the rest of the run. Rows A3/A4 pin
// both ends: the number must stay positive, and the debit must not exceed what was released.
static int64_t committed_stat(void) {
  mi_stats_t_decl(stats);   // the API checks size/version: a zeroed struct is rejected
  return (mi_stats_get(&stats) ? stats.committed.current : -1);
}

static void* big_alloc(size_t n, unsigned char pattern) {
  void* p = mi_malloc(n);
  if (p == NULL) { printf("  out of memory for %zu bytes\n", n); exit(2); }
  memset(p, pattern, n);
  return p;
}

static bool big_verify(void* p, size_t n, unsigned char pattern) {
  const unsigned char* b = (const unsigned char*)p;
  for (size_t i = 0; i < n; i++) { if (b[i] != pattern) { return false; } }
  return true;
}

// A purge with the flag under test. Returns the report; retries briefly while a scavenger pass
// happens to hold the arena purge guard (only possible on the first rows of a fresh process).
static mi_purge_all_report_t reclaim_all(void) {
  mi_purge_all_ex(MI_PURGE_FORCE, 100, NULL);   // settle: purge everything purgeable first
  mi_purge_all_report_t rep; _mi_memzero(&rep, sizeof(rep));
  for (int i = 0; i < 20; i++) {
    (void)mi_purge_all_ex((mi_purge_flags_t)(MI_PURGE_FORCE | MI_PURGE_RECLAIM), 100, &rep);
    if (rep.reclaimed) break;
    yield_cpu();
  }
  return rep;
}

// ---------------------------------------------------------------------------------------------
// A1-A4: reservation, control, reclaim, and "the metadata follows the live set"
// ---------------------------------------------------------------------------------------------

static void run_reclaim_rows(void) {
  printf("[A1] reserve %d arenas with %d x %zu MiB objects\n", (int)BIG_COUNT, (int)BIG_COUNT, BIG_BYTES / (1024 * 1024));
  const arena_accounting_t base = accounting();                        // the row assumes: no arena yet
  mi_option_set(mi_option_arena_reserve, (long)ARENA_RESERVE_KIB);     // KiB, lowered to the minimum: one big object per arena
  void* keep_small = mi_malloc(64);                                    // make sure this thread owns a theap (and an arena)
  const arena_accounting_t small = accounting();                       // what one small allocation reserved (0 or one arena)
  void* big[BIG_COUNT];
  // `arena_size` is MEASURED, not assumed: one big object forces one fresh arena whose size
  // follows from `mi_arena_reserve`'s over-reserve (96 MiB here: the object's page needs 513
  // slices, i.e. one more than a 512-slice b-bitmap chunk, so the request is
  // `align_up(513 slices + MI_ARENA_MAX_CHUNK_OBJ_SIZE, MI_ARENA_MAX_CHUNK_OBJ_SIZE)` and the
  // arena ends up with 1536 slices). Only ONE such object fits per arena: a request of more
  // than one chunk is satisfied from whole free chunks, and one object leaves at most one.
  // Every object must force the SAME size for the rows' byte arithmetic to be exact.
  size_t arena_size = 0;
  for (int i = 0; i < BIG_COUNT; i++) {
    const arena_accounting_t was = accounting();
    big[i] = big_alloc(BIG_BYTES, (unsigned char)(0x40 + i));
    const size_t step = accounting().reserved - was.reserved;
    if (i == 0) { arena_size = step; }
    else if (step != arena_size) { arena_size = 0; }
  }
  const arena_accounting_t first = accounting();
  printf("  reserved %.1f MiB (was %.1f), metadata %.3f MiB, one arena is %.1f MiB\n",
         (double)first.reserved / (1024.0 * 1024.0), (double)small.reserved / (1024.0 * 1024.0),
         (double)first.meta / (1024.0 * 1024.0), (double)arena_size / (1024.0 * 1024.0));
  check(base.reserved == 0, "A1: no arena existed before the row (so the counts below are exact)");
  check(arena_size > 0, "A1: each big object reserved an arena of its own (and of the same size)");
  check(first.reserved == small.reserved + arena_size * BIG_COUNT, "A1: the big objects account for exactly that many arenas");

  // Free half of them: those arenas are now completely free, the other half still hold data.
  mi_free(big[1]); big[1] = NULL;
  mi_free(big[3]); big[3] = NULL;
  const arena_accounting_t before = accounting();

  // A2: the control -- without the flag, nothing is released (the behaviour before this flag).
  mi_purge_all_report_t plain; _mi_memzero(&plain, sizeof(plain));
  (void)mi_purge_all_ex(MI_PURGE_FORCE, 100, &plain);
  const arena_accounting_t after_plain = accounting();
  check(!plain.reclaimed && plain.arenas_reclaimed == 0 && plain.arena_reclaim_bytes == 0, "A2: without MI_PURGE_RECLAIM nothing is released");
  check(after_plain.reserved == before.reserved && after_plain.meta == before.meta, "A2: the arena accounting is unchanged by the control call");

  // A3: the reclaim itself.
  const int64_t committed_before = committed_stat();
  const mi_purge_all_report_t rep = reclaim_all();
  const arena_accounting_t after = accounting();
  const int64_t committed_after = committed_stat();
  printf("  reclaimed %zu arenas / %.1f MiB; pending subprocs %zu; kept %zu; committed %lld B\n",
         rep.arenas_reclaimed, (double)rep.arena_reclaim_bytes / (1024.0 * 1024.0), rep.subprocs_pending, rep.arenas_kept,
         (long long)(committed_after - committed_before));
  check(rep.reclaimed, "A3: the reclaim ran (no thread was inside the allocator)");
  check(rep.arenas_reclaimed >= 2, "A3: both completely free arenas were released");
  check(rep.arena_reclaim_bytes >= 2 * arena_size, "A3: the released bytes cover both reservations");
  check(rep.arenas_kept == 0, "A3: no completely free arena was left behind (kept == 0)");
  check(rep.subprocs_pending == 0, "A3: no sub-process was pending");
  check(after.reserved + rep.arena_reclaim_bytes == before.reserved, "A3: reserved address space dropped by exactly the reported bytes");
  check(after.meta < before.meta, "A3: the arenas' metadata dropped with them");
  check(committed_before > 0 && committed_after > 0, "A3: the committed statistic stayed positive across the release");
  check(committed_before - committed_after <= (int64_t)rep.arena_reclaim_bytes, "A3: the committed debit never exceeds the released reservations");
  check(big_verify(big[0], BIG_BYTES, 0x40) && big_verify(big[2], BIG_BYTES, 0x42), "A3: the live objects in the non-free arenas survived byte-for-byte");

  // A4: free what is left and reclaim again -- after that, the metadata of the peak is gone.
  mi_free(big[0]); mi_free(big[2]);
  mi_purge_all(true);  // reset-style purges can leave data slices committed
  const arena_accounting_t purged = accounting();
  const int64_t committed_before2 = committed_stat();
  const mi_purge_all_report_t rep2 = reclaim_all();
  const arena_accounting_t after2 = accounting();
  const int64_t committed_after2 = committed_stat();
  printf("  reclaimed %zu arenas / %.1f MiB more; metadata now %.3f MiB\n",
         rep2.arenas_reclaimed, (double)rep2.arena_reclaim_bytes / (1024.0 * 1024.0), (double)after2.meta / (1024.0 * 1024.0));
  check(rep2.reclaimed && rep2.arenas_reclaimed >= 1, "A4: the remaining arenas of the peak were released too");
  check(after2.reserved + rep2.arena_reclaim_bytes == after.reserved, "A4: reserved address space dropped by exactly the reported bytes");
  check(after2.meta < first.meta, "A4: the peak's metadata is gone -- only what is still live is accounted for");
  check(after2.reserved <= small.reserved, "A4: the peak's address space is gone too (only what the small object needed may stay)");
  check(committed_after2 > 0, "A4: `committed` stayed positive across the second release too");
  check(committed_before2 - committed_after2 <=
          (int64_t)(purged.meta - after2.meta + purged.committed - after2.committed),
        "A4: reclaim debits only metadata and slices still committed");
  mi_free(keep_small);
}

// ---------------------------------------------------------------------------------------------
// A5: a thread inside the allocator forbids the reclaim (MI_DEBUG: the #271 stall hook)
// ---------------------------------------------------------------------------------------------

#if MI_DEBUG > 0

typedef struct stall_ctx_s {
  mi_heap_t* heap;
  void*      block;
} stall_ctx_t;

static stall_ctx_t g_stall;

static THREAD_RET stall_worker(void* arg) {
  (void)arg;
  // Inside the allocator, holding a page pinned ("pinned, not yet claimed"), i.e. exactly the
  // state in which a reclaim that freed the arena under it would be a use-after-free.
  mi_heap_delete(g_stall.heap);
  return 0;
}

static void run_busy_thread_row(void) {
  printf("[A5] a thread inside the allocator forbids the reclaim\n");
  mi_option_set(mi_option_arena_reserve, (long)ARENA_RESERVE_KIB);
  g_stall.heap = mi_heap_new();
  g_stall.block = mi_heap_malloc(g_stall.heap, BIG_BYTES);
  if (g_stall.block == NULL) { check(false, "A5: heap page allocated"); return; }

  mi_atomic_store_release(&mi_debug_stall_in_heap_delete_claim, (uintptr_t)1);
  thread_t worker;
  if (!thread_start(&worker, stall_worker, NULL)) { check(false, "A5: worker thread started"); return; }
  // Wait for the hook: 2 == "pinned, not yet claimed" inside mi_heap_visit_page_claim.
  // A yield count does not guarantee worker progress. CTest's TIMEOUT bounds a
  // worker that never reaches the hook; reclaim must not run before it does.
  while (mi_atomic_load_acquire(&mi_debug_stall_in_heap_delete_claim) != 2) { yield_cpu(); }
  const bool stalled = (mi_atomic_load_acquire(&mi_debug_stall_in_heap_delete_claim) == 2);
  check(stalled, "A5: the worker is stalled inside mi_heap_delete (page pinned)");

  // Create the empty candidate after pthread/CreateThread has finished allocating its
  // runtime state; otherwise that state can occupy and pin the arena under test.
  void* empty = big_alloc(BIG_BYTES, 0x5A);
  mi_free(empty);
  const arena_accounting_t before = accounting();
  mi_purge_all_report_t rep2; _mi_memzero(&rep2, sizeof(rep2));
  (void)mi_purge_all_ex((mi_purge_flags_t)(MI_PURGE_FORCE | MI_PURGE_RECLAIM), 0 /* no waiting */, &rep2);
  const arena_accounting_t after = accounting();
  check(!rep2.reclaimed, "A5: the reclaim reports that it could not run");
  check(rep2.arenas_reclaimed == 0 && rep2.arena_reclaim_bytes == 0, "A5: nothing was released while a thread was inside the allocator");
  check(rep2.subprocs_pending >= 1, "A5: the sub-process is reported pending");
  check(after.reserved == before.reserved && after.meta == before.meta, "A5: the arena accounting is untouched");

  // let the worker finish its heap delete, then the very same arena is free for real
  mi_atomic_store_release(&mi_debug_stall_in_heap_delete_claim, (uintptr_t)0);
  const bool joined = thread_join(worker);
  check(joined, "A5: the worker finished its heap delete");
  mi_free(g_stall.block);  // heap_delete preserves live blocks
  g_stall.block = NULL;
  const mi_purge_all_report_t rep3 = reclaim_all();
  check(rep3.reclaimed && rep3.arenas_reclaimed >= 1, "A5: with the worker gone the arena IS released");
}

#else   // MI_DEBUG > 0

static void run_busy_thread_row(void) {
  printf("[A5] skipped: the deterministic heap-delete stall hook exists only with MI_DEBUG>0\n");
}

#endif  // MI_DEBUG > 0

// ---------------------------------------------------------------------------------------------
// A6: a parked live thread does not block the reclaim
// ---------------------------------------------------------------------------------------------

typedef struct park_ctx_s {
  std::atomic<long> ready;
  std::atomic<long> go;
  std::atomic<long> started;   // `mi_on_thread_idle_start` returned true (#366: false -- nothing to hand off -- in a gated build, where every thread outside the allocator is parked anyway)
  void* block;
} park_ctx_t;

static park_ctx_t g_park;

static THREAD_RET park_worker(void* arg) {
  (void)arg;
  g_park.block = mi_malloc(64);                       // this thread owns a theap ...
  const bool parked = mi_on_thread_idle_start();      // ... and hands its theaps over (#272)
  mi_atomic_store_release(&g_park.started, (parked ? 1 : 0));
  mi_atomic_store_release(&g_park.ready, 1);
  while (mi_atomic_load_acquire(&g_park.go) == 0) { yield_cpu(); }
  if (parked) { mi_on_thread_idle_end(); }            // the documented pair: only after a park that happened
  return 0;
}

static void run_parked_thread_row(void) {
  printf("[A6] a parked live thread does not block the reclaim\n");
  // one more arena of our own to reclaim while the parked thread exists
  void* big = big_alloc(BIG_BYTES, 0x61);
  mi_free(big);
  mi_atomic_store_release(&g_park.ready, 0);
  mi_atomic_store_release(&g_park.go, 0);
  thread_t worker;
  if (!thread_start(&worker, park_worker, NULL)) { check(false, "A6: worker thread started"); return; }
  // Establish the parked-thread precondition before reclaim; the same CTest
  // timeout bounds a missing hand-off, regardless of scheduling or yield speed.
  while (mi_atomic_load_acquire(&g_park.ready) == 0) { yield_cpu(); }
  check(mi_atomic_load_acquire(&g_park.ready) == 1, "A6: the worker announced its idle hand-off");
  check(mi_atomic_load_acquire(&g_park.started) == 1 || MI_OWNER_GATE != 0,
        "A6: the worker is parked (mi_on_thread_idle_start, or the owner gate)");

  const mi_purge_all_report_t rep = reclaim_all();
  printf("  reclaimed %zu arenas / %.1f MiB with one parked thread alive\n",
         rep.arenas_reclaimed, (double)rep.arena_reclaim_bytes / (1024.0 * 1024.0));
  check(rep.reclaimed, "A6: the reclaim ran (a parked thread is claimable, not an obstacle)");
  check(rep.subprocs_pending == 0, "A6: no sub-process was pending");

  mi_atomic_store_release(&g_park.go, 1);
  check(thread_join(worker), "A6: the worker left its park and exited");
  check(g_park.block != NULL, "A6: the parked thread's own allocation was never released under it");
  mi_free(g_park.block);
}

// ---------------------------------------------------------------------------------------------

// Run in a fresh process so the user heap's arena is the final slot.
static void run_heap_reuse_row(void) {
  printf("[A7] a user heap can reuse a reclaimed tail slot for a larger arena\n");
  mi_option_set(mi_option_arena_reserve, (long)ARENA_RESERVE_KIB);
  void* keep = mi_malloc(64);
  mi_heap_t* heap = mi_heap_new();
  void* p = mi_heap_malloc(heap, BIG_BYTES);
  if (p == NULL) { check(false, "A7: heap allocation succeeded"); return; }
  mi_arena_t* arena = _mi_safe_ptr_page(p)->memid.mem.arena.arena;
  const size_t idx = arena->arena_idx;
  const size_t old_slices = arena->slice_count;
  check(idx + 1 == mi_atomic_load_acquire(&heap->subproc->arena_count), "A7: the user heap occupies the tail arena");
  mi_free(p);
  mi_heap_collect(heap, true);
  const mi_purge_all_report_t rep = reclaim_all();
  check(rep.reclaimed && rep.arenas_reclaimed > 0, "A7: the empty arena was reclaimed");
  check(mi_atomic_load_ptr_acquire(mi_arena_t, &heap->subproc->arenas[idx]) == NULL,
        "A7: the old arena slot is empty");
  const bool detached = (mi_atomic_load_ptr_acquire(mi_arena_pages_t, &heap->arena_pages[idx]) == NULL);
  check(detached, "A7: the user heap's old tracking was detached");
  if (detached) {
    const size_t larger = 4 * BIG_BYTES;
    p = mi_heap_malloc(heap, larger);
    check(p != NULL, "A7: allocation in a larger arena succeeded");
    if (p != NULL) {
      arena = _mi_safe_ptr_page(p)->memid.mem.arena.arena;
      check(arena->arena_idx == idx && arena->slice_count > old_slices,
            "A7: the same slot now holds a larger arena");
      memset(p, 0x72, larger);
      check(big_verify(p, larger, 0x72), "A7: the replacement allocation remains writable");
      mi_free(p);
    }
  }
  mi_heap_delete(heap);
  mi_free(keep);
}

// A non-exclusive public reservation is still caller-owned when its ID is returned.
// This row is deliberately a separate process: a buggy reclaim unmaps the arena,
// and mi_arena_area(id) then faults under ASan/Windows instead of reporting a failure.
static void run_retained_id_row(void) {
  printf("[A8] a retained ID for a non-exclusive public reservation survives reclaim\n");
  mi_arena_id_t id = NULL;
  const int rc = mi_reserve_os_memory_ex(64 * 1024 * 1024, false, false, false, &id);
  check(rc == 0 && id != NULL, "A8: the public reservation returned an arena ID");
  if (rc != 0 || id == NULL) return;
  size_t size_before = 0;
  void* const area_before = mi_arena_area(id, &size_before);
  check(area_before != NULL && size_before >= 64 * 1024 * 1024,
        "A8: the retained ID names its original reservation");
  mi_purge_all_report_t rep; _mi_memzero(&rep, sizeof(rep));
  (void)mi_purge_all_ex((mi_purge_flags_t)(MI_PURGE_FORCE | MI_PURGE_RECLAIM), 100, &rep);
  check(rep.reclaimed && rep.arenas_kept >= 1,
        "A8: reclaim ran but kept the caller-reserved empty arena");
  size_t size_after = 0;
  void* const area_after = mi_arena_area(id, &size_after);
  check(area_after == area_before && size_after == size_before,
        "A8: the retained arena ID remains valid after reclaim");
}

int main(int argc, char** argv) {
  setvbuf(stdout, NULL, _IONBF, 0);   // a hang must show which row it hangs in (ctest kills on TIMEOUT)
  printf("mimalloc-pprof: free-arena reclaim, MI_OWNER_GATE=%d MI_DEBUG=%d\n", (int)MI_OWNER_GATE, (int)MI_DEBUG);
  if (argc == 2 && strcmp(argv[1], "--heap-reuse") == 0) {
    run_heap_reuse_row();
  }
  else if (argc == 2 && strcmp(argv[1], "--retained-id") == 0) {
    run_retained_id_row();
  }
  else {
    run_reclaim_rows();
    run_busy_thread_row();
    run_parked_thread_row();
  }
  mi_collect(true);
  printf("%s (%d failure(s))\n", (failures == 0 ? "PASSED" : "FAILED"), failures);
  return (failures == 0 ? 0 : 1);
}
