# Benchmark restoration verification (#615)

The reported TCMalloc build failure is repaired by merged PR #608. A complete
current-main run now verifies the repair through publication. This evidence
PR closes the remaining verification work; it adds no allocator or workflow
changes and claims no CPU candidate improvement.

## Original failure and repair

[Run 39577325702](https://github.com/zackees/mimalloc-pprof/actions/runs/39577325702/job/119145992175#step:5:75)
failed before measurement on `ee3623f6b7a485754a0a0aa3462d8cba43390e8b`:

- Expected TCMalloc module-lock SHA-256:
  `340a645e18fb1cdaed3dfd465312a3447de84ab31f78bd7db1863e2ee80f79f6`.
- Observed: `c0b842b0e615e31c165a5d7f005f97fe6c0dcbed05576ebf17967d0702232544`.

[PR #608](https://github.com/zackees/mimalloc-pprof/pull/608), merged as
`2c7fed4edf91cf412c8969cf3cafeb690f0ed3e3`, pinned the dependency-lock patch,
selected Bazel 9.1.0 explicitly, and retained `--lockfile_mode=error`.
The earlier soldr Cargo.toml warning was not the fatal error. No additional
source defect was reproduced on current main, so this PR does not duplicate
the existing repair or substitute an arbitrary checksum.

The existing tests reject a corrupt module lock before invoking the build
tool, and reject Bazel 9.3.0 even when the lock is valid. The allocator-builder
and benchmark-workflow modules pass together: **30 tests**. Their verbose
output and the original failing build log are retained in the archive.

## Complete current-main result

[Run 39815683542](https://github.com/zackees/mimalloc-pprof/actions/runs/39815683542)
used exact source `227eef06ff03f92966f38508441d6c2f60a75135`, full mode,
15 blocks, and normal execution. All six workflow jobs passed:

1. Native allocator build, full measurement, raw validation, site rendering
   and site validation.
2. Independent sealed-artifact audit.
3. Benchmark branch publication.
4. Pages packaging.
5. Pages deployment.
6. Independent branch and Pages publication audit.

The raw validator reports **2,250 samples**, **30 cells**, **15 minimum blocks
per cell**, all five allocators (TCMalloc, jemalloc, Microsoft mimalloc, Bun
mimalloc, mimalloc-pprof), headline eligibility, and no validation errors.
The sealed site's raw samples match the original raw artifact exactly.

Published branch commit: `0dde10e817210be049a5f4dae460f526f64f49b2`.
[Live benchmark site](https://zackees.github.io/mimalloc-pprof/).
Independent local audits also verified the sealed artifact, exact published
branch revision, and live Pages payload. Their original output is retained.
This full main-branch dispatch exercised the production publication path used
by the schedule; it is not a claim that a later scheduled run has already run.

The site carries forward scaling results from run `39741871074`; this run
refreshes the core throughput suite. It does not close scaling issue #607,
prove fresh memory/latency campaigns, or replace #614's CPU acceptance ledger.

## Evidence and reproduction

`verification.json` identifies the archive SHA-256, source, run, and published
revision. `verification.zip` retains the original raw, provenance, validation,
and sealed-site artifacts; the original failing log; successful workflow
metadata/logs; focused test output; and local audit output. Its internal
`manifest.json` records every member's byte length and SHA-256. Verify the
outer digest and each member before using extracted evidence. The archive
contains no replacement or generated benchmark samples.

From the repository root, after extracting into a scratch directory:

```sh
python -B ci/benchmark_report.py validate-site \
  --site-dir <scratch>/benchmark-downloads/benchmark-site-39815683542 \
  --detached-digest <scratch>/benchmark-downloads/benchmark-validation-39815683542/manifest.sha256
```

The retained branch revision and live-site audits record publication at the
time of this verification. Later scheduled publications can legitimately
replace the live payload; that does not rewrite the archived result.
