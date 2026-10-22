## Stage-one result: candidate not accepted

Exact baseline `227eef06ff03f92966f38508441d6c2f60a75135`, candidate `287cac2f664a46703ebb6522eef8c8dbeb984cbb`. All three original cohorts completed with 120 actual samples per arm (base/head/null), unchanged traces and sanity gates. Original executables/hash manifests, raw samples, workflow logs and independently recomputed ledgers are retained. No hosts are pooled.

**No statistically supported target CPU or throughput gain.** Across 18 metrics: 10 PASS, 8 INCONCLUSIVE, 0 FAIL; zero memory credit in every row, all release bounds held. The stage-two condition is not met. I will not expand this candidate to the remaining rows or rerun it unchanged. It is rejected for delivery despite successful correctness/platform gates.

The measured chart binaries confirm the intended prologue removal, but structural work reduction is not a measured benefit. CPU changes in both ephemeral targets lean worse, and the plain-ephemeral RSS interval is unresolved; neither is silently relabelled as equivalence. Further implementation requires fresh attribution within the original CPU-only scope, not weaker thresholds.

### large-class/8

Original run: https://github.com/zackees/mimalloc-pprof/actions/runs/39823333694

base `227eef06ff03` head `287cac2f664a` on AMD EPYC 7763 64-Core Processor, 60 reps (up to 120); 4 logical CPUs, 8-worker cells have 2.00 workers per logical CPU (above 1 means oversubscription); arm order: rotating-reversed-blocks-v1

Rule-12 ledger (CLAUDE.md; ci/perf_ab_ledger.py): `saved%` of the reducible peak-RSS gap, then per metric the paired change [95% sign-test interval] against its allowance (saved%/3 for CPU, throughput and faults; noise for memory and for cells that saved none). INCONCLUSIVE is not a pass.

| workload | saved% | verdict | ops/s | cpu s | minor faults | p99 operation ns | peak RSS MiB | RSS 0.5 s after drain MiB |
|---|---|---|---|---|---|---|---|---|
| large-class/8 | 0% | **INCONCLUSIVE** | +0.1% [-0.6, +0.6] / 0.7% **PASS** | -0.0% [-0.4, +0.3] / 0.3% **INCONCLUSIVE** | -0.6% [-2.0, +1.4] / 2.2% **PASS** | +0.1% [-1.0, +0.6] / 1.1% **PASS** | -0.0% [-0.2, +0.0] / 0.1% **PASS** | -0.4% [-1.8, +0.1] / 1.8% **PASS** |

### large-class-ephemeral/8

Original run: https://github.com/zackees/mimalloc-pprof/actions/runs/39823337377

base `227eef06ff03` head `287cac2f664a` on AMD EPYC 7763 64-Core Processor, 60 reps (up to 120); 4 logical CPUs, 8-worker cells have 2.00 workers per logical CPU (above 1 means oversubscription); arm order: rotating-reversed-blocks-v1

Rule-12 ledger (CLAUDE.md; ci/perf_ab_ledger.py): `saved%` of the reducible peak-RSS gap, then per metric the paired change [95% sign-test interval] against its allowance (saved%/3 for CPU, throughput and faults; noise for memory and for cells that saved none). INCONCLUSIVE is not a pass.

| workload | saved% | verdict | ops/s | cpu s | minor faults | p99 operation ns | peak RSS MiB | RSS 0.5 s after drain MiB |
|---|---|---|---|---|---|---|---|---|
| large-class-ephemeral/8 | 0% | **INCONCLUSIVE** | -0.4% [-0.7, -0.1] / 0.5% **INCONCLUSIVE** | +0.3% [+0.2, +0.4] / 0.3% **INCONCLUSIVE** | +0.0% [+0.0, +0.8] / 0.8% **INCONCLUSIVE** | -0.0% [-1.0, +0.6] / 1.0% **PASS** | +0.0% [-0.0, +2.6] / 0.1% **INCONCLUSIVE** | +0.0% [-9.4, +0.8] / 9.8% **PASS** |

### large-class-ephemeral/8 short generations (chart build)

Original run: https://github.com/zackees/mimalloc-pprof/actions/runs/39823341873

base `227eef06ff03` head `287cac2f664a` on AMD EPYC 9V74 80-Core Processor, 60 reps (up to 120); 4 logical CPUs, 8-worker cells have 2.00 workers per logical CPU (above 1 means oversubscription); arm order: rotating-reversed-blocks-v1

Rule-12 ledger (CLAUDE.md; ci/perf_ab_ledger.py): `saved%` of the reducible peak-RSS gap, then per metric the paired change [95% sign-test interval] against its allowance (saved%/3 for CPU, throughput and faults; noise for memory and for cells that saved none). INCONCLUSIVE is not a pass.

| workload | saved% | verdict | ops/s | cpu s | minor faults | p99 operation ns | peak RSS MiB | RSS 0.5 s after drain MiB |
|---|---|---|---|---|---|---|---|---|
| large-class-ephemeral/8 short generations (chart build) | 0% | **INCONCLUSIVE** | -0.3% [-0.9, +0.4] / 0.8% **INCONCLUSIVE** | +0.4% [+0.1, +0.9] / 0.4% **INCONCLUSIVE** | -0.4% [-0.8, +0.0] / 0.7% **PASS** | +1.4% [-0.7, +2.1] / 1.4% **INCONCLUSIVE** | +0.0% [-0.0, +0.0] / 2.6% **PASS** | +0.0% [-0.0, +0.0] / 2.6% **PASS** |

