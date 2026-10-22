# V3 original stage-one ledger

Baseline `227eef06ff03f92966f38508441d6c2f60a75135`; candidate `5b2d8f960297fda825b05b7471893fc4cf6c2f33`.

REJECT V3: two confirmed per-cell regressions; no stage two or unchanged rerun

Original per-host paired cohorts only; zero memory credit. Intervals and verdicts are retained.

## large-class/8

Run 39840352295; AMD EPYC 9V74 80-Core Processor; samples per arm: {'base': 110, 'head': 110, 'null': 110}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.4470 [-1.0644, -0.0539] | 0.9693 | INCONCLUSIVE |
| cpu s | +0.6433 [+0.4006, +0.8918] | 0.3584 | FAIL |
| minor faults | -0.4376 [-1.7021, +1.3158] | 1.7621 | PASS |
| p99 operation ns | +1.8763 [+0.7396, +2.9586] | 1.5129 | INCONCLUSIVE |
| peak RSS MiB | +0.0000 [-0.0052, +0.0051] | 0.0052 | PASS |
| RSS 0.5 s after drain MiB | +0.0000 [-0.0052, +0.0051] | 0.0052 | PASS |

Release p95: 1120.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8

Run 39840357033; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 75, 'head': 75, 'null': 75}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.8333 [-1.0046, -0.5076] | 0.3310 | FAIL |
| cpu s | +0.3109 [+0.0964, +0.4994] | 0.1852 | INCONCLUSIVE |
| minor faults | +0.0000 [-0.3906, +0.7968] | 1.5444 | PASS |
| p99 operation ns | +0.6169 [-0.0564, +1.6256] | 1.0839 | INCONCLUSIVE |
| peak RSS MiB | +0.1045 [+0.0052, +2.6618] | 2.6071 | INCONCLUSIVE |
| RSS 0.5 s after drain MiB | +10.9141 [+0.0000, +14.1152] | 13.1240 | INCONCLUSIVE |

Release p95: 1050.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8 short generations (chart build)

Run 39840362233; AMD EPYC 9V74 80-Core Processor; samples per arm: {'base': 120, 'head': 120, 'null': 120}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | +0.3585 [+0.0259, +0.7054] | 1.1761 | PASS |
| cpu s | +0.1323 [-0.0657, +0.4696] | 0.4251 | INCONCLUSIVE |
| minor faults | +0.0000 [-0.3831, +0.7547] | 0.7634 | PASS |
| p99 operation ns | +1.0573 [-0.6935, +2.0647] | 0.7289 | INCONCLUSIVE |
| peak RSS MiB | +0.0052 [-0.0052, +2.6757] | 2.5398 | INCONCLUSIVE |
| RSS 0.5 s after drain MiB | +0.0052 [-0.0052, +2.6705] | 2.5398 | INCONCLUSIVE |

Release p95: 1030.0 ms; bound: 1300 ms; held: True.

