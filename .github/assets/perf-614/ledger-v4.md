# V4 original stage-one ledger

Baseline `227eef06ff03f92966f38508441d6c2f60a75135`; candidate `8fda62c6d05a68ea4c0ffe2859f07eabf5e72d97`.

STOP: no statistically supported target CPU/throughput gain

Original per-host paired cohorts only; zero memory credit. Intervals and verdicts are retained.

## large-class/8

Run 39845980415; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 120, 'head': 120, 'null': 120}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | +0.1033 [-0.5035, +0.5822] | 0.8696 | PASS |
| cpu s | -0.1548 [-0.3298, +0.0418] | 0.3071 | PASS |
| minor faults | -1.0087 [-3.5398, +0.4444] | 3.0435 | PASS |
| p99 operation ns | -0.0280 [-1.1577, +1.1211] | 1.1280 | PASS |
| peak RSS MiB | -0.0570 [-2.5930, +0.0158] | 0.1688 | PASS |
| RSS 0.5 s after drain MiB | +0.1305 [-1.4765, +1.6039] | 1.2146 | INCONCLUSIVE |

Release p95: 1040.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8

Run 39845983742; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 120, 'head': 120, 'null': 120}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.3513 [-0.5710, +0.0413] | 0.3058 | INCONCLUSIVE |
| cpu s | +0.3114 [+0.1776, +0.4774] | 0.2455 | INCONCLUSIVE |
| minor faults | -0.1901 [-0.7663, +0.3891] | 0.7782 | PASS |
| p99 operation ns | -1.8827 [-2.6022, -1.1088] | 1.0971 | PASS |
| peak RSS MiB | +0.0052 [-0.0472, +0.1258] | 0.0629 | INCONCLUSIVE |
| RSS 0.5 s after drain MiB | +0.0000 [-0.0455, +0.7329] | 10.8636 | PASS |

Release p95: 1050.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8 short generations (chart build)

Run 39845987944; AMD EPYC 9V45 96-Core Processor; samples per arm: {'base': 95, 'head': 95, 'null': 95}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.2938 [-0.8665, +0.5941] | 0.9289 | PASS |
| cpu s | +0.2126 [-0.2354, +0.7039] | 0.7425 | PASS |
| minor faults | -0.3774 [-0.7782, +0.3831] | 1.1236 | PASS |
| p99 operation ns | +0.0000 [-0.5043, +0.4993] | 1.0188 | PASS |
| peak RSS MiB | -0.0052 [-2.7441, +0.0054] | 2.6757 | PASS |
| RSS 0.5 s after drain MiB | -0.0052 [-2.7441, +0.0054] | 2.6757 | PASS |

Release p95: 1090.0 ms; bound: 1300 ms; held: True.

