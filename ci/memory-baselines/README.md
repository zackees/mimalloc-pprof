# Windows cross memory baseline provenance

The two `windows-x64-soldr-*-pprof1.json` references were generated from the
**previous merged allocator**, not from the #585 candidate:

- Source: `80a513dfa6f84a16a07b8612c4041acb7cee4091`.
- [Windows full run 36812458448](https://github.com/zackees/mimalloc-pprof/actions/runs/36812458448),
  completed 2026-10-01; artifacts `memory-gate-windows-gnu-x64-soldr` and
  `memory-gate-windows-msvc-x64-soldr`.
- Eight unfiltered, non-leak runs per compiler; `memory_gate.py update` records
  their minimum, 71.6 MB of **peak commit**, with 0.1% spread. Values have the
  producer's 0.1 MB precision; matching rounded values do not prove byte equality.
- Toolchain keys: `x64` / `soldr-mingw-gcc-15` and `x64` / `soldr-clang-cl-21`.
  These are separate from native Microsoft's `cl` baseline.

The source above and #585's frozen merged baseline
`ee3623f6b7a485754a0a0aa3462d8cba43390e8b` have identical `src/`, `include/`,
`test/`, `CMakeLists.txt`, `cmake/`, Windows workflow, bundle producer/runner and
memory-gate comparison code. The intervening changes concern CI policy,
attestation, documentation and other benchmark workflows. Verify with:

```sh
git diff --exit-code 80a513dfa6f84a16a07b8612c4041acb7cee4091 \
  ee3623f6b7a485754a0a0aa3462d8cba43390e8b -- src include test \
  CMakeLists.txt cmake .github/workflows/windows-bundles.yml \
  ci/bundle_tests.py ci/run_test_bundle.py ci/memory_gate.py
```

Before committing the references, each compiler’s eight-run normal #585 set
passed the existing minimum-of-eight comparator, using results from [run 39535628340](https://github.com/zackees/mimalloc-pprof/actions/runs/39535628340)
and each eight-run injected-leak set failed that comparator as intended. This is the established
historical Windows memory gate, not a same-host rule-12 performance ledger or a
claim of a new memory saving. No tolerance was changed. The earlier green CI
steps had only collected data: with no reference file they skipped both actual
comparison and positive control with warnings. These files activate both paths.

## Historical input SHA256

Only normal historical runs below were passed to `update`. Candidate and leak
records were used for checks, never to generate these reference values.

| artifact input | SHA256 |
|---|---|
| `old-gnu/result-1.json` | `20ce9f2f9ea5bd887e952e73bf4c7dc6f015df3dd7479cc6603f48f0db9226c5` |
| `old-gnu/result-2.json` | `1cd1bbd6ed97a75cf47c7a86a0dcf798c2b372d64f119dc581b3a954a7e9407c` |
| `old-gnu/result-3.json` | `26b65dad2175b5f6bcccd86af633cb10c93ad5fdcd30721d702327ec266d7abf` |
| `old-gnu/result-4.json` | `72aa3ad19fb0addd8248e163f34252e1cef0dbe6bcf380df79733e1b7077e13b` |
| `old-gnu/result-5.json` | `b77896aa12495379fa3882d3168288b22e560699008754f927870c36e39f1559` |
| `old-gnu/result-6.json` | `7ca80dec1fdeecc8b551aa867708f0b6b35a16433f7b58140025127971d3ceb5` |
| `old-gnu/result-7.json` | `57d530b510ae17f29ce163219a63ff3bd99552e5dbafb692f9f9c18d7a5f4614` |
| `old-gnu/result-8.json` | `cb33fc68953d920be2dedac68bd460a11588578ddc4cfe49fec5691ebb50c347` |
| `old-msvc/msvc-result-1.json` | `0b6f467ea1176fae063c5b165b31de7570be6dcb0401512a9899ce5c0d8135f7` |
| `old-msvc/msvc-result-2.json` | `52e64e84746407d291ae2b9ca62c990cee230698c91e0e8db206cd0c410aa8f4` |
| `old-msvc/msvc-result-3.json` | `0d9706ae80a7148985a9c7e1ba5ac4376d3a55c1ddfdb8d2cdc65ca0338d0bbf` |
| `old-msvc/msvc-result-4.json` | `7fa2daf1b2f91b627d457b735b7c41354741c14f610e84cc4353a7f378a4d99b` |
| `old-msvc/msvc-result-5.json` | `99a124e148b9be1c1a3e65555f80e76f8d11b937119ff9fd30e816035d39a4e1` |
| `old-msvc/msvc-result-6.json` | `b80e9b8798addf85705baaee1f453b1faa84faf8a940ba00aed968f0efa91ee8` |
| `old-msvc/msvc-result-7.json` | `1e0805e67bc9d8dd6fd8e3687f6cd48f84d87329b7c06c2ce6062ba7e51ab39b` |
| `old-msvc/msvc-result-8.json` | `1f112edd79aa7ee6cb3b6ecf978e6ac276a1a07c02749c52108d7b7a102bff39` |
