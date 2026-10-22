# V6 original stage-one ledger

Baseline `227eef06ff03f92966f38508441d6c2f60a75135`; candidate `a40d8d6e717df31859103a034bf349d32e7760fc`.

Rejected: no statistically supported target CPU/throughput gain. 14 PASS, 4 INCONCLUSIVE, zero FAIL. No stage two or unchanged rerun.

Original per-host paired cohorts only; zero memory credit. All ten platform workflows passed. Platform success does not establish performance acceptance.

## large-class/8

Run 39855326807; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 80, 'head': 80, 'null': 80}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | +0.8402 [-0.0645, +1.4413] | 0.6799 | PASS |
| cpu s | +0.0272 [-0.2649, +0.3017] | 0.4542 | PASS |
| minor faults | -0.4357 [-2.6087, +2.2026] | 2.6432 | PASS |
| p99 operation ns | -0.5771 [-1.1513, +0.5482] | 1.6103 | PASS |
| peak RSS MiB | +0.0131 [-0.0578, +0.0684] | 0.0893 | PASS |
| RSS 0.5 s after drain MiB | -0.1289 [-1.9111, +1.5691] | 2.0848 | PASS |

Release p95: 1040.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8

Run 39855330463; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 120, 'head': 120, 'null': 120}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.1958 [-0.4755, +0.1010] | 0.6521 | PASS |
| cpu s | +0.1477 [+0.0069, +0.3486] | 0.2342 | INCONCLUSIVE |
| minor faults | +0.3929 [-0.3802, +0.7905] | 0.3968 | INCONCLUSIVE |
| p99 operation ns | +0.5028 [+0.0000, +1.0616] | 1.0037 | INCONCLUSIVE |
| peak RSS MiB | +0.0230 [-0.0210, +0.0839] | 0.1020 | PASS |
| RSS 0.5 s after drain MiB | +0.0000 [-0.7731, +10.9190] | 0.8235 | INCONCLUSIVE |

Release p95: 1050.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8 short generations (chart build) short generations (chart build)

Run 39855334782; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 110, 'head': 110, 'null': 110}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.1823 [-0.5360, +0.2481] | 0.5711 | PASS |
| cpu s | +0.1091 [-0.1198, +0.2890] | 0.2946 | PASS |
| minor faults | +0.0000 [-0.3861, +0.7605] | 1.1407 | PASS |
| p99 operation ns | -0.0517 [-0.9932, +0.5873] | 1.0965 | PASS |
| peak RSS MiB | -2.5124 [-2.6753, +0.0000] | 2.5489 | PASS |
| RSS 0.5 s after drain MiB | -2.5124 [-2.6753, +0.0000] | 2.5489 | PASS |

Release p95: 1010.0 ms; bound: 1300 ms; held: True.

