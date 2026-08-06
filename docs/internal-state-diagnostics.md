# Internal-state diagnostic methodology

Issue [#167](https://github.com/zackees/mimalloc-pprof/issues/167) turns the
thread-local growth failures behind #128 and upstream
[microsoft/mimalloc#1358](https://github.com/microsoft/mimalloc/issues/1358)
into repeatable review and CI checks. Everything described here is diagnostic or
test-only. `MI_DEBUG=0` has no diagnostic field, source file, symbol, code, or data.

## Allocation inventory

[`ci/internal-state-inventory.json`](../ci/internal-state-inventory.json) is the
machine-readable inventory. Each allocator-owned allocation or growth boundary records:

- the allocator domain and exact semantic call signature;
- initial owner, owner transitions, lifetime, and every actor that can destroy it;
- logical requested size versus usable-size assumptions;
- cleanup path, including allocation-failure behavior;
- publication point; and
- locks held or acquired transitively.

The following is the review-facing index. The JSON inventory is authoritative for the
exact callee/argument signature and expands each compact cell into separate structured
fields.

| Site | Domain and owner transition | Lifetime / destroyer | Logical versus usable extent | Publication, cleanup, and lock context |
| --- | --- | --- | --- | --- |
| `arena-os-aligned-offset-dispatch` | OS memid -> arena caller | memid lifetime; arena/sub-process teardown | requested size / aligned OS reservation | returned unpublished; OS/arena locks are transitive |
| `arena-os-aligned-dispatch` | OS memid -> arena caller | memid lifetime; arena/sub-process teardown | requested size / aligned OS reservation | returned unpublished; OS/arena locks are transitive |
| `arena-allocation-dispatch` | managed arena or OS -> typed caller | caller/memid lifetime | requested size / slice or OS extent | wrapper only; arena claim locks are transitive |
| `arena-pages-metadata` | sub-process main heap -> heap arena-page slot | heap/arena lifetime | computed metadata bytes / allocator class | initialize then CAS; loser/teardown frees; cleanup lock |
| `arena-reservation` | local OS reservation -> arena registry | arena/sub-process lifetime | slice-rounded request / memid OS size | manage then publish; failure frees immediately |
| `huge-os-pages-reservation` | huge OS reservation -> arena registry | arena/sub-process lifetime | requested pages / returned pages and byte size | managed after return; OS and arena locks |
| `heap-object` | main heap -> `subproc->heaps` | heap/sub-process lifetime | `sizeof(mi_heap_t)` / allocator class | initialize then link under `heaps_lock`; key failure frees |
| `thread-local-data` | sub-process meta -> thread TLD | thread lifetime | `sizeof(mi_tld_t)` / meta slot | initialize before TLS use; thread exit frees |
| `subprocess-object` | parent meta -> global sub-process list | explicit sub-process lifetime | `sizeof(mi_subproc_t)` / meta slot | list publish under `subprocs_lock`; destroy children then free |
| `memory-events-unwrapped-block` | raw OS -> API caller | caller lifetime | header + payload / recorded OS size | initialize header before return; caller frees |
| `page-map-flat-reservation` | raw OS -> flat page-map globals | process page-map lifetime | bitmap + map / recorded OS size | single-init publication; page-map teardown frees |
| `page-map-two-level-reservation` | raw OS -> two-level globals | process page-map lifetime | top map + null submap / recorded OS size | null submap before release publish; teardown frees |
| `page-map-submap` | raw OS -> indexed page-map slot | page-map lifetime | fixed submap / fixed OS size | CAS publish under map lock; loser frees |
| `profiler-dump-chunk` | raw OS -> operation-local list | one dump | header + capacity / exact retained size | initialize then link; dispose frees all; no allocator lock |
| `profiler-arena-chunk` | raw OS -> global profiler arena | profiler session | max chunk expression / recorded chunk size | prepend under profiler serialization; bulk teardown |
| `profiler-stack-table-initial` | profiler arena -> intern-table global | profiler session | capacity pointers / bump extent | zero then publish under `prof_lock`; bulk teardown |
| `profiler-stack-table-growth` | profiler arena -> replacement table | profiler session | grown capacity / bump extent | rehash then publish under `prof_lock`; old region stays arena-owned |
| `profiler-interned-stack` | profiler arena -> intern table | profiler session | header + PC depth / bump extent | copy then hash-publish under `prof_lock`; bulk teardown |
| `profiler-sample-record` | profiler arena -> active/free lists | profiler session, logical reuse | record size / bump extent | initialize before page/list linkage under `prof_lock` |
| `profiler-snapshot` | raw OS -> snapshot caller | caller lifetime | header + entries + PCs / exact total | deep-copy under `prof_lock`, then return; caller frees |
| `profiler-proto-modules` | raw OS -> dump-local modules | one protobuf dump | fixed module array / same cleanup expression | never global; every exit frees |
| `profiler-proto-pc-table` | raw OS -> dump-local hash table | one protobuf dump | power-of-two table / same cleanup expression | zero before use; every exit frees |
| `stats-json-buffer` | default heap -> stats caller | caller lifetime | requested capacity / allocator class | pointer/size update after successful growth; caller frees |
| `thread-heap-object` | sub-process meta -> heap and TLD lists | refcounted thread/heap lifetime | theap size / meta slot | dual-list publication locks; refcount-zero frees |
| `exclusive-arena-theap` | exclusive arena -> heap and TLD lists | refcounted thread/heap lifetime | rounded theap size / arena slice | arena + list locks; matching arena free |
| `test-control-tls-slot-array-owner` | test default heap -> prospective TLS pointer | negative-control process only | TLS logical count / allocator class | owner check must reject before publication; test-only |
| `per-thread-tls-slot-array` | main heap -> thread TLS pointer | thread lifetime | header + logical slots / allocator class | zero/check/count before TLS publish; failure preserves old pointer |
| `global-tls-key-bitmap` | process-main meta -> global key bitmap | process lifetime | bitmap logical bits / recorded meta size | copy/init under global TLS lock, then replace and free old |

[`ci/check_internal_state.py`](../ci/check_internal_state.py) lexically scans every C
source for the owning allocation APIs used by persistent internal state. Primitive
implementations and public allocation wrappers are explicitly excluded in the checker;
all other uses must match an inventory entry. A new call, removed call, changed callee,
changed ownership argument, or changed size expression fails CI. Occurrence numbers only
disambiguate identical calls in mutually exclusive page-map implementations.

The checker's `--selftest` is a required positive control. It injects an unclassified
site, changes the TLS allocation's owner/size signature, and removes a classified site;
the gate must reject all three mutations.

## Focused dynamic checks

`MI_DEBUG_FULL` adds an owner word to internal locks. Acquiring a lock already owned by
the current thread fails before entering the platform lock, so a recursive deadlock
becomes a bounded diagnostic. Releasing from a non-owner and destroying an owned lock
also fail. Diagnostic formatting uses a fixed stack buffer and the raw stderr primitive;
it cannot allocate through mimalloc.

The dynamic TLS array is checked after each growth for two invariants:

1. its page belongs to a main heap rather than a user-destroyable default heap; and
2. every newly added logical slot is zero, independent of allocator size-class slack.

The private `MI_TEST_TLS_CONTROL` build supplies three deterministic controls: a
wrong-owner block, poisoned new slots, and a one-shot growth allocation failure. The
first two must fail with their exact diagnostic. The failure control proves the old
table and every heap/theap identity remain usable, then proves retry succeeds. This
define is attached only to a dedicated test executable and is absent from library
targets.

`test-threadlocal-growth` uses only public APIs. It keeps 1,050 heaps live to cross the
16, 32, 64, 128, 256, 512, and 1,024 geometric capacities and the first linear-growth
step. It verifies identity after every insertion, then repeats growth on worker-thread
exit and sub-process destruction paths.

## Release performance firewall

[`ci/check_release_equivalence.py`](../ci/check_release_equivalence.py) preprocesses the
complete `src/static.c` allocator at `MI_DEBUG=0` for `MI_PPROF=ON` and `OFF`. The
permanent CI check compares the current revision with a same-revision variant where
`diagnostic.c` is forcibly included; compiler input must remain identical, and a token
scan rejects every diagnostic field, hook, and test-control name. This covers
declarations and object layouts as well as executable code and static data without
blocking unrelated intentional release changes in future PRs.

For the issue #167 integration PR, `--base origin/main` additionally compares the entire
release amalgamation with its parent. That one-time result belongs in the PR evidence;
it is intentionally not a permanent cross-revision policy.

CI additionally configures both release variants and rejects `diagnostic.c` in the
compile database or any diagnostic symbol in the archive. The release checker has its
own positive control: injected release data must make the comparison fail.

## Manual audit loop

When reviewing new allocator state, start from the inventory entry rather than only the
allocation line:

1. Follow ownership through every default-heap, sub-process, and thread transition.
2. Pair the logical requested extent with every initialization, copy, and cleanup
   extent; never infer logical initialization from an allocator's usable size.
3. Trace all failure exits before publication and verify the previous state remains
   authoritative.
4. Expand each helper call into its transitive locks and allocator calls. Check lock
   order against startup, thread exit, heap destruction, and process teardown.
5. Add a positive control that deliberately violates the new invariant. A diagnostic
   that has never been seen to fire is not a gate.
6. Put all instrumentation behind `MI_DEBUG_FULL` or a private test define, then rerun
   the release-equivalence gate for profiler ON and OFF.

This loop is deliberately narrow: it catches ownership, extent, publication, cleanup,
and lock-order regressions without adding a release runtime option or dependency.
