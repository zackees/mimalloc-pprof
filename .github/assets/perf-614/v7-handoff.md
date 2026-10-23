# V7 draft handoff

Implementation C commit `2ec4d32e`; generated Rust C commit `28ef6ad7`.
Baseline: `227eef06ff03f92966f38508441d6c2f60a75135`.

V7 specializes only removal of the selected retired large singleton. It avoids the already-clear full-queue flag atomic RMW and generic linked-list checks, preserving ownership, queue/page counts, geometry and callback/failure ordering. No policy change.

## Evidence

- Original shared removal: focused work-count RED (one redundant flag write; two flag cases fail), actual executable retained.
- Candidate: C large-span PASS; secure C++ large-span PASS; actual ASan + owner-gate large-span PASS. Tests include generic control, state/flags/accounting/cache checks and 121 ordered recarve pairs.
- Design and implementation reviews clean. Generated Rust source regenerated separately.
- Secure C++ purge suite cannot compile: goto crosses initialized locals. Reproduced on byte-identical frozen-baseline test with matching compiler flags.
- Candidate ASan + owner-gate purge suite: 34 passed, 1 failed (`new-thread-does-not-sweep-at-once`, six global discards vs zero). Matching baseline run passed. No unchanged candidate retry and no waiver.
- Both purge processes recorded zero successful large repurposes, so V7's specialized path did not execute in that failing suite. Global discard accounting may include background activity; root cause remains unresolved.
- Archive contains exact binaries, source patch/new headers, commands, build flags, logs and internal member hashes; external digest is adjacent. The preserved RED test header was reconstructed from the test before the later generic-control addition; the actual failing executable and log are preserved.

## Remaining work

Resolve validation blockers with focused RED/GREEN evidence. Then validate generated Rust and applicable platforms, predeclare and run V7 performance against the frozen baseline, and apply #614's complete acceptance criteria. No V7 performance runs have been dispatched; no CPU/RSS benefit is claimed. PR must remain draft. V1–V6 rejections and full ledgers are retained alongside this evidence.

This handoff publishes the current work, not completion of #614. Final merge, task-owned cleanup and original-checkout synchronization remain outstanding.
