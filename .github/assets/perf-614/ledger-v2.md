# V2 original stage-one ledger

Baseline `227eef06ff03f92966f38508441d6c2f60a75135`; candidate `59d369609ab47358df0f883b6419252f1ffbf03a`.

STOP: no statistically supported target CPU/throughput gain

Original per-host paired cohorts only; zero memory credit. Intervals and verdicts are retained.

## large-class/8

Run 39831495052; Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz; samples per arm: {'base': 60, 'head': 60, 'null': 60}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | +0.5714 [-1.3148, +1.2130] | 1.4155 | PASS |
| cpu s | -0.4897 [-1.0908, +0.5073] | 0.8227 | PASS |
| minor faults | -1.4996 [-3.8674, +0.4237] | 2.0921 | PASS |
| p99 operation ns | +0.9573 [-0.6498, +1.7575] | 2.1407 | PASS |
| peak RSS MiB | +0.0559 [-0.0766, +2.5338] | 2.5809 | PASS |
| RSS 0.5 s after drain MiB | -0.1192 [-2.1851, +1.9511] | 3.7011 | PASS |

Release p95: 1100.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8

Run 39831499063; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 120, 'head': 120, 'null': 120}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | +0.3751 [-0.0942, +0.5970] | 0.4314 | PASS |
| cpu s | -0.0879 [-0.1899, +0.0749] | 0.1690 | PASS |
| minor faults | +0.0000 [-0.7722, +0.3906] | 0.7812 | PASS |
| p99 operation ns | +0.8061 [+0.4727, +1.5848] | 0.5456 | INCONCLUSIVE |
| peak RSS MiB | +0.0000 [-0.0733, +0.1626] | 2.5703 | PASS |
| RSS 0.5 s after drain MiB | +0.0227 [-0.7276, +10.9645] | 0.7329 | INCONCLUSIVE |

Release p95: 1050.0 ms; bound: 1300 ms; held: True.

## large-class-ephemeral/8 short generations (chart build)

Run 39831504283; AMD EPYC 7763 64-Core Processor; samples per arm: {'base': 120, 'head': 120, 'null': 120}.

| Metric | Change % [95% CI] | Allowance % | Verdict |
| --- | --- | --- | --- |
| ops/s | -0.4759 [-1.2258, +0.3828] | 0.7212 | INCONCLUSIVE |
| cpu s | +0.2379 [-0.0983, +0.5851] | 0.3687 | INCONCLUSIVE |
| minor faults | +0.1873 [-0.3861, +0.7722] | 0.7692 | INCONCLUSIVE |
| p99 operation ns | +0.5108 [-0.0539, +1.1457] | 1.0678 | INCONCLUSIVE |
| peak RSS MiB | +0.0000 [-0.0314, +0.0054] | 0.0430 | PASS |
| RSS 0.5 s after drain MiB | +0.0000 [-0.0314, +0.0052] | 0.0052 | INCONCLUSIVE |

Release p95: 1010.0 ms; bound: 1300 ms; held: True.
