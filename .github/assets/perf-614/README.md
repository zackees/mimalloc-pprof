# Issue 614 attribution evidence

This archive records local attribution and structural correctness evidence.
It is not a performance acceptance result. The first CPU candidate is rejected for delivery: its three original target
cohorts found no supported CPU/throughput gain. The nine platform workflows
passed; that does not establish a performance benefit.

## Mechanism and provenance

Frozen baseline: `227eef06ff03f92966f38508441d6c2f60a75135`.
Candidate source: `287cac2f664a46703ebb6522eef8c8dbeb984cbb`.

Baseline profiles identified `_mi_page_unpurge_all` entering a substantial
register-save/stack-frame prologue even when neither unformed nor formed
blocks require restoration. The candidate moves the original no-work checks
into a small wrapper, returning immediately for clean pages and tail-calling
the unchanged restoration body when needed. It does not change purge policy,
geometry, retention, or page selection.

Count-only baseline probes observed:

| Original workload | Calls | Calls with restoration work |
| --- | ---: | ---: |
| large-class/8 | 1,078,045 | 0 |
| large-class-ephemeral/8 | 1,073,032 | 0 |
| short-generation chart ephemeral/8 | 265,819 | 0 |

These are observations of the recorded traces, not a claim that dirty pages
never occur. Atomic count probes and diagnostic event builds are unsuitable
for acceptance timing. Sampled profiles use debug/frame-pointer builds on a
Ryzen 7 3700X; restricted kernel symbols and limited sample counts constrain
attribution. Any local replay timing is diagnostic only.

## Correctness evidence

`red-instrumented.patch` adds the restoration-entry diagnostic counter to the
original body. The clean-page test fails against that body (`red-test.log`).
The candidate passes the event-counter, purge-hole and large-span C tests
(`green-tests.log`). The test verifies both that the clean page header remains
byte-for-byte unchanged and that the restoration body is not entered.
The original RED executable was overwritten during rebuilding; the patch and
log preserve its reproducible failing condition, not the original binary.

C++ Debug/full event-counter and large-span tests also passed. The first
C++ test invocation ran the event test but could not run the not-yet-built
large-span binary (`cxx-tests.log`). After building it, the later large-span
run passed (`cxx-large-test.log`). The optional
C++ purge-hole target failed on existing goto-across-initialization errors in
unchanged `test/test-purge-holes.c`; its failing build log is retained. This
archive does not claim the entire C++ suite passed.

The first local gate failed on missing `sys/sdt.h`. Supplying the existing Nix
libsystemtap include path allowed all 45 checks to pass; both gate logs remain.

## Archive verification

`attribution-archive.json` records the ZIP SHA-256. Its internal `manifest.json`
records the byte length and SHA-256 of all 43 other members. Verify both the
outer digest and each member before using the evidence. Files include original
sampled data/reports, baseline and probe executables, event/residency reports,
probe patches, test/build logs and host/compiler provenance.

Restore executable permissions when replaying archived binaries. The baseline
profile binaries have no build IDs; restore their recorded paths or configure
perf's symbol root explicitly. No acceptance inference follows from archive
integrity alone.

## Platform gates

`platform-gates.zip` contains metadata and original logs for all nine successful
platform workflows on candidate `287cac2f664a46703ebb6522eef8c8dbeb984cbb`.
The API tree records in `source-equivalence.json` verify that CI's test merge
`c9eab6aebbc68a4763b6063f1ebbf6060b6fe39e` has the identical source tree.

- [asan](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378819): success.
- [c-unit](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378654): success.
- [cross](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378542): success.
- [fuzz](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378855): success.
- [macos](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378817): success.
- [purge](https://github.com/zackees/mimalloc-pprof/actions/runs/39817379021): success.
- [python](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378317): success.
- [rust](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378888): success.
- [windows](https://github.com/zackees/mimalloc-pprof/actions/runs/39817378954): success.

The cancelled runs from the earlier PR-open event are superseded by these
label-triggered runs. Their cancellation-derived failures are not represented
as passing tests. These results establish platform validation of this source;
they do not establish CPU, RSS or latency acceptance. The archive has an outer
SHA-256 in `platform-gates-archive.json` and per-member hashes in its manifest.

## V1 measurement disposition

[V1 full original ledger](ledger-v1.md): **10 PASS, 8 INCONCLUSIVE, zero FAIL**.
All three original cohorts reached 120 samples per arm, with zero memory
credit and passing release bounds. No target CPU or throughput interval
establishes a gain, so the predeclared condition for the remaining ten rows
was not met. No unchanged rerun or stage-two expansion was dispatched.
The candidate is rejected for lack of demonstrated benefit; its inconclusive
metrics have not been converted to PASS or invented FAIL verdicts.

`v1-measurements.zip` preserves all three original sample files, actual
throughput/latency executables and original SHA manifests, workflow metadata
and logs, exact dispatch requests/responses, independently recomputed ledgers,
and exact source snapshots for the baseline and measured candidate. Source
snapshots omit only historical `.github/assets/` evidence; every included
file was verified against its original Git blob ID. The archive also retains
disassembly from the measured binaries and the rebase source-equivalence proof.
`v1-measurements-archive.json` records its outer digest; its internal manifest
records each member's byte length and digest. No cross-host pooling is used.

## V2: guard the call at retirement publication (acceptance pending)

Candidate `59d369609ab47358df0f883b6419252f1ffbf03a` replaces the rejected
wrapper with an inline condition at `_mi_page_publish_retired`. Clean pages
skip the call entirely; dirty pages and other callers retain the original
restore function. Optimized assembly confirms the skipped call, but the
caller prologue remains. This is structural evidence, not a CPU gain.

`v2-structural.zip` retains RED patch/log/executable, GREEN test logs and
executables, source diff, code-generation object/settings/disassembly and
review disposition. Its manifest hashes every other member; the external
JSON hashes the archive. The RED patch applies to `a98a43c1` (the preserved V1
history), restoring the original restore body and adding the entry-count test.
The assertion fails because clean retirement still invokes restoration.

With V2, the clean-retirement test passes and same-page reuse works. Real OS
discards exercise formed-only and tail-only conditions independently; the
formed test explicitly restores only its tail before checking the guard.
C Release event/purge-hole/large-span pass 3/3; C++ Debug/full event/large-span
pass 2/2. The final isolation checks also pass separately in C and C++.
The existing C++ purge-hole compile limitation described above remains.
Source review is clean after correcting the test isolation gap.

The archive retains setup failures and superseded logs transparently:
`v2-red-build.log` is the corrected target build; the initial target name was
invalid. `v2-green-tests.log` was collected while the test binary was being
rebuilt and is superseded by `v2-complete-tests.log` after build completion.
Initial codegen configuration omitted soldr's `cc` argument; fixing it left
an empty Release flag cache. Only the `optimized` codegen logs/disassembly,
object and settings establish the optimized result (`-O3 -DNDEBUG`). The
unoptimized disassembly is not acceptance evidence. The successful Rust
amalgamation command ran from `rust/`; an earlier root invocation could not
find the pinned toolchain. No build failure was treated as a passing test.

Local gate passed all 45 checks in 55 seconds, stamped the candidate above.
`v2-campaign-plan.json` and `v2-dispatches.json` retain the predeclared plan
and three original target dispatches, announced in issue comment
[6348172962](https://github.com/zackees/mimalloc-pprof/issues/614#issuecomment-6348172962).
Frozen baseline, sample caps, workloads, null/sanity arms and zero-credit
acceptance remain unchanged. V1 results are not pooled with V2. Fresh full
platform checks and complete performance acceptance remain pending.

The candidate source snapshot in the campaign scratch was verified against
all 855 original Git blobs excluding only historical `.github/assets/`
evidence (`v2-source-manifest.json`). Retain that snapshot with the eventual
measured artifacts; this manifest alone does not ship the source tar.

## Original V2 platform outcome and test correction

All eleven original platform/check workflows are terminal: ten succeeded;
ASan failed in the owner-gate Debug lane. The new event-counter test called
`_mi_page_free_collect` without acquiring the owner gate. Its assertion is a
real failure, retained in `v2-original-platform.zip` (25 members, outer digest
in the matching JSON, per-member digests inside). Native MSVC, win-GNU,
both native Macs, Rust, memory/default-path checks and the other ASan lanes
passed. These do not erase the failed owner-gate lane.

`v2-test-fix-evidence.zip` retains the failing CI logs and local reproduction,
the test-only correction and focused GREEN evidence. The first local build
was named `v2-owner-asan` but sanitizer support was disabled by a missing
header: its RED/GREEN proves the owner-gate assertion, not ASan coverage.
The later `v2-owner-sanitized` build supplied the existing compiler-rt header,
explicitly enabled ASan and linked `__asan_init`; the corrected test passed.
The disabled-gate configuration also passed. Review was clean and independently
reran the sanitized test. Original setup and warning logs remain in the archive.

The correction acquires the owner gate before internal page accesses and
releases it before heap deletion. Stamped corrected head
`876d7c313026090b0dbeaadb8d0cf2f03f0c9c13` passed 45/45 local checks in 59 seconds
(`v2-test-fix-local-gate.log`). The source-equivalence JSON verifies the only
changes from measured V2 are the test and evidence; allocator, Rust, build
settings and benchmark harness are unchanged. The three original performance
cohorts are retained without duplicate dispatches. Fresh platform validation
of the corrected tests is running; no final platform or performance acceptance
is claimed here.

## V2 measurement disposition: rejected

[Original V2 ledger](ledger-v2.md): **11 PASS, 7 INCONCLUSIVE, zero FAIL**.
All three original cohorts completed; large-class stopped at 60 samples per
arm under the unchanged adaptive protocol, both ephemeral rows reached 120.
All release bounds passed. No target CPU/throughput interval establishes a
gain. The predeclared stage-two criterion therefore failed; the remaining ten
rows were not dispatched, and no unchanged candidate was rerun.

Plain-ephemeral p99 has a positive interval (+0.4727% to +1.5848%) and remains
INCONCLUSIVE against its noise allowance. Its after-drain RSS interval
(-0.7276% to +10.9645%) also remains INCONCLUSIVE and is not accepted. No
uncertainty is waived, no memory credit claimed and no hosts pooled. V2 is
rejected for lack of demonstrated benefit; numeric verdicts are retained.

`v2-measurements.zip` retains original raw samples, actual measured binaries
and SHA manifests, workflow metadata/logs, exact dispatch requests/responses,
independent per-cell ledgers, the predeclared campaign and disposition,
verifiers, and exact baseline/candidate source snapshots. Historical evidence
alone is excluded from source snapshots. `v2-source-manifest.json` records
verification of all candidate source files against Git blobs; the baseline
snapshot's verification is retained from V1. Check the external digest and
all internal manifest members before using the archive.

The test-only owner-gate correction changes no measured allocator or harness
code; source equivalence is retained. Fresh corrected platform CI remains
separate and cannot establish a performance benefit. Do not merge the rejected
restore-guard mechanism on the strength of correctness checks alone.

## V3 rejected: skip zero-state reuse accounting

The first completed original cohort (run39840357033, plain ephemeral)
confirmed a throughput regression of -0.8333% (95% CI -1.0046% to -0.5076%)
beyond its 0.3310% noise allowance. V3 is rejected and removed. All three
original cohorts are now complete; no stage two or unchanged rerun was
dispatched. [Initial rejection ledger](https://github.com/zackees/mimalloc-pprof/issues/614#issuecomment-6349925978).

V1 and V2 production changes have been removed. The new candidate skips the
full span/accounting call after successful retired-page selection when its
demand-state byte is zero. The result was discarded, the state remains zero
under every runtime cap/enable value, and no grow/shrink event is emitted.
Nonzero states retain the original call. Selection, geometry, aging,
retention and purge policies are unchanged.

`v3-structural.zip` preserves three count-only baseline probe variants and
their original executables, patches, commands and logs. The narrowest probe
observed 426,264 / 392,636 / 95,960 repeated zero-state accounting calls in
the three target replays. These instrumented counts prove occurrence, not
performance benefit; their timings are not acceptance evidence.

The archive also contains focused RED and GREEN executables/logs, exhaustive
state/bin/runtime-option equivalence tests, C++ and actual ASan owner-gate
results, optimized codegen, and review disposition. Reproducing either source
patch requires its separately archived new header. See the internal notes
and external archive digest. Performance acceptance remains pending.

## Corrected V2 platform evidence

`v2-corrected-platform.zip` preserves corrected source `876d7c31` validation:
ten workflows completed successfully, including C/native MSVC, Windows GNU,
native Mac arches, Rust, and ASan owner-gate. The fuzz workflow still reported
`in_progress` at collection despite all three jobs completing successfully.
The complete job list and individual job logs are retained; this is unresolved
workflow finalization, not a claimed successful workflow. No rerun or manual
cancellation was requested. The original ASan test failure remains separately
preserved. These correctness results do not change V2's performance rejection.

A subsequent fresh REST observation confirms that corrected V2 fuzz run
`39835530789` completed successfully, with all three jobs successful.
`v2-corrected-fuzz-finalization.json` retains that run and complete job record.
The earlier pending-status snapshot stays unchanged. All eleven corrected
V2 platform workflows therefore completed successfully; V2 remains rejected
for lack of demonstrated performance benefit.

## V4 candidate: accounting without an unused size result

After removing rejected V3, V4 calls the original demand-transition helper
through a dedicated internal API with the known bin index. Every state uses
the original transition and runtime option checks. Optimized assembly removes
the repeated bin classification and unused size-result calculation. This
uses the repeated-call attribution preserved in `v3-structural.zip`.

`v4-structural.zip` preserves focused RED/GREEN executables, patches with
separate new headers, exhaustive state/bin/runtime-option equivalence tests,
C/C++/actual ASan results, optimized code and review. Its internal notes
explain reproduction and limitations. Performance acceptance remains pending;
V3's confirmed throughput regression is not waived by this new candidate.

## Complete V3 disposition

[Original full ledger](ledger-v3.md): **6 PASS, 10 INCONCLUSIVE, 2 FAIL**.
Large-class CPU also regressed beyond noise (+0.6433%, CI +0.4006% to
+0.8918%, allowance 0.3584%). Short-chart throughput improved (+0.3585%,
CI +0.0259% to +0.7054%); that cannot offset either confirmed regression.
All release bounds held. Zero memory credit, no cross-host pooling, no
threshold relaxation, no unchanged rerun and no stage-two expansion.

`v3-measurements.zip` preserves original raw samples, exact binaries, hashes,
workflow records/logs, dispatch requests/responses, plans, verifiers, ledger,
and exact baseline/candidate source snapshots. `v3-platform.zip` preserves
all eleven successful platform workflows and exact candidate/test-merge tree
equivalence. Every archive has an internal member manifest and external
digest. Platform success does not change V3's rejection.
