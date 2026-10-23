# V7 investigation: retired large singleton queue removal

Not implemented or measured. No performance claim.

Fixed baseline 227eef06ff03f92966f38508441d6c2f60a75135. Baseline dense ephemeral profile attributes 0.71% self samples to mi_page_queue_remove (perturbed attribution only). Existing original event runs record large_repurpose 554050 large, 530645 plain ephemeral, 130788 chart ephemeral; repeated report lines are the same process counters, not additional samples.

Selection in page-retired-search.h requires first == last, retired, used == 0, separate arena metadata, and a large-bin queue. Generic mi_page_queue_remove nonetheless loads/tests linked neighbors and performs mi_page_set_in_full(false). Optimized baseline disassembly v7-baseline-queue-remove.asm retains lock btr at 0x1f00b to clear the already-clear full-queue bit. The source helper updates pages_full_size only if the flag changes. Generic push similarly clears the full flag, but optimizing push is not yet selected.

Proposed bounded candidate: specialized removal for the selected large singleton, preserving all queue count/page count updates and failure sequencing, proving the full bit is already clear and owner-only. No page-selection, geometry, aging, retention, purge, publication order or service policy change. Keep generic removal for all other callers. New logic belongs in a new file; focused shared-function RED/GREEN test must count flag RMWs and verify queue/page state. Must verify failure paths and concurrency before implementation acceptance.

Next: inspect all full-bit writers and ownership preconditions, build shared structural test RED on generic removal, then implement and inspect optimized code. Existing timings cannot predict any gain; original declared perf campaign remains required for a new candidate.
