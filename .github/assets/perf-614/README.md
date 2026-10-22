# Issue 614 attribution evidence

This archive records local attribution and structural correctness evidence.
It is not a performance acceptance result. The CPU candidate remains pending
its original CI measurement cohorts and full platform gates.

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
