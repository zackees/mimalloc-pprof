# Issue 585 measurement evidence

Frozen baseline: `ee3623f6b7a485754a0a0aa3462d8cba43390e8b`.
Current measured source: `ad9fbd952ca934257f4acdc19889e74d8886a18f`.
This directory is an evidence snapshot. **Acceptance is incomplete.**

## Current outcome

[Full per-cell ledgers and original run links](https://github.com/zackees/mimalloc-pprof/issues/585#issuecomment-6328936369):
11 of 13 rows have supported PASS evidence. Nine plain rows retain their original
source/host/sample records through verified identity of all four actual linked
base/head throughput/latency executables. Short-generation chart passed at 65
samples per arm; chart Larson passed at 120. Standard chart remains INCONCLUSIVE
on minor faults and peak RSS, and profiler remains INCONCLUSIVE on p99, both at
120 samples per arm. CPU/throughput passed in both; release bounds held. Memory
credit is zero. These results do not establish a new RSS saving.

[Full platform CI passed 104 checks with five intentional skips](https://github.com/zackees/mimalloc-pprof/issues/585#issuecomment-6328893298).
The tested PR merge and measured head have identical entire Git trees. This
correctness result does not replace performance acceptance. PR 609 remains draft.
[Bounded local attribution did not establish the cause of the two uncertainties](https://github.com/zackees/mimalloc-pprof/issues/585#issuecomment-6328974076).

## Subsequent correctness failure and fixture correction

The evidence-only head `28ac6796` subsequently completed with 102 successful checks,
two failed checks and five intentional skips (the same 109 check names).
[Windows clang-cl debug failed the arena-reclaim-reset A5 worker precondition](https://github.com/zackees/mimalloc-pprof/issues/585#issuecomment-6329284276);
its required gate failed as a consequence. Later clang-cl shared/owner-gate/memory
steps were skipped after the failure, so this run does not validate them. Earlier
measured-source platform success remains historical evidence.

The fixture waited for only 20,000 yields before assuming worker progress. The
correction waits for the actual pinned/parked predicates under the existing
300-second CTest timeout, preserving all reclaim and accounting assertions. A
controlled 200 ms delay in both workers gives RED exit 1 before and GREEN exit 0
after with the same Debugfull allocator library. All four local reclaim CTests
pass. This demonstrates a possible scheduling defect, not the exact historical
CI cause. Full CI for the correction remains required. Allocator source and the
original 11-PASS/two-INCONCLUSIVE performance outcome are unchanged.

`arena-reclaim-wait.zip` and its separate manifest retain the complete failed
Windows log, original check inventory, source snapshots, local driver, build
commands, library, probe binaries and RED/GREEN/test outputs. The local driver
reads the checkout: the before phase used the original test at `28ac6796` and the
after phase used the correction. The archived source snapshots are authoritative;
adjust original scratch paths before reproduction. The driver is Linux-only
(`usleep`); Windows coverage comes from full CI, not these local probes.

## Contents and limits

`measurements.zip` retains exact input bytes and original relative paths.
`manifest.json` records every entry's byte count and SHA256, original executable
aliases, known unavailable historical data, and original protocol/result files.
Surviving benchmark executables are stored once per actual digest under
`executables/`. The archive includes original failed, incomplete, legacy and
rejected experiments; inclusion never means PASS. Numeric artifacts retain their
original commit IDs, CPU models, repetitions and arm protocols. Do not pool
incompatible datasets, replace inconclusive outcomes, or reconstruct missing data
from rounded logs. Local diagnostics and fixture outputs are not CI acceptance.

The archive includes original perf-ab raw JSON, scaling raw records, executable
provenance, terminal CI logs, local diagnostic records, reports and reproduction
scripts. Local scripts preserve their original scratch paths; adjust those paths
to the extracted evidence before reuse. This snapshot is not a build cache or an
installed toolchain, and it does not retain full source checkouts/build trees.
Historical reports can describe state superseded by the current outcome above.

Three historical losses are explicit in the manifest: old first-sanity samples
were not saved, a pre-checkpoint timed-out run produced no raw artifact, and an
old failed run's executables were not preserved. No substitute measurements were
created for them. Preserving this evidence does not finish issue 585's remaining
performance or memory work.

Verify the archive and every entry without extracting or executing its contents:

```sh
python - <<'PYVERIFY'
import hashlib, json, zipfile
from pathlib import Path
root = Path('.github/assets/perf-585')
for manifest_name, archive_name in [('manifest.json', 'measurements.zip'),
                                    ('arena-reclaim-wait-manifest.json', 'arena-reclaim-wait.zip')]:
    manifest = json.loads((root / manifest_name).read_text())
    archive = root / archive_name
    assert archive.stat().st_size == manifest['archive']['bytes']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['archive']['sha256']
    with zipfile.ZipFile(archive) as zipped:
        assert len(zipped.namelist()) == len(manifest['files'])
        assert set(zipped.namelist()) == {f['path'] for f in manifest['files']}
        for entry in manifest['files']:
            data = zipped.read(entry['path'])
            assert len(data) == entry['bytes']
            assert hashlib.sha256(data).hexdigest() == entry['sha256']
print('All evidence hashes verified; acceptance remains incomplete.')
PYVERIFY
```
