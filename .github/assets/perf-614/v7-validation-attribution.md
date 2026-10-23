# V7 validation attribution

Candidate purge test: 34 pass, 1 pacing failure (6 discards vs 0). Baseline matched build passes. No unchanged candidate rerun.

Additional source/log evidence: MI_DIAGNOSTICS=1 in actual candidate compiler flags. Event counter implementation is unsampled monotonic atomic counts; print omits zero values. No test event resets and no production reset callers. Both final candidate and baseline event lines omit large_repurpose, meaning zero successful repurposes in those processes. MI_EVENT_LARGE_REPURPOSE occurs before the specialized removal call. Thus candidate specialization was not executed by this failing suite; this does not prove a test-only bug or waive the pacing assertion. Generic removal source is unchanged except extraction and a macro resolving to the exact original setter in production.

The test computes global discard-count change around thread C. Other threads including the scavenger remain enabled; it does not attribute the discards to C. This is a potential attribution defect, not yet proven root cause. A diagnostic should identify the discarding thread and reason before changing the assertion or allocator.

Secure C++ compile error is independently reproduced against frozen baseline identical purge test source and flags. A narrow compile repair would move initialized locals before reachable goto edges without changing checks.

Rule7 stop on optimization work and measurement remains in effect. Asked owner whether to expand work order to diagnose/fix validation blockers. No new approval inferred from elapsed time.
