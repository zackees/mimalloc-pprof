# Fuzz harnesses

Structure-aware libFuzzer harnesses (issue #87). Built only with `-DMI_FUZZ=ON` and
clang, and driven by the `fuzz` CI workflow rather than ctest — a fuzz run has no fixed
duration, so it does not belong in the normal suite.

| Harness | Target |
|---|---|
| `fuzz_alloc_ops.c` | the allocator API, as an operation sequence over a live-block table |
| `fuzz_prof_dump.c` | the profiler's hand-rolled text and protobuf dump encoders |

The oracle is **AddressSanitizer**. Without a sanitizer, fuzzing an allocator mostly
proves it does not segfault, which is a weak claim — so these were deliberately not
built until ASan was live and proven (#86). Both harnesses also assert properties a
crash would never reveal: `mi_usable_size` bounds, whole-block zeroing, alignment,
realloc content preservation, and protobuf length-prefix framing.

## Running locally

```sh
cmake -S . -B build-fuzz -DMI_FUZZ=ON -DMI_PPROF=ON -DCMAKE_C_COMPILER=clang
cmake --build build-fuzz
./build-fuzz/mimalloc-fuzz-alloc-ops test/fuzz/corpus/alloc-ops -runs=100000
```

## Corpus

`corpus/<harness>/` holds seed inputs that reach each opcode, so the fuzzer starts from
valid-shaped input rather than rediscovering the encoding.

**Any crash found becomes a regression test in `test/`, not just a corpus entry.** A
corpus that grows without promoted test cases is a finding quietly rotting.

## Positive control

`-DMI_FUZZ_PLANT_BUG=ON` compiles a deliberate one-byte overflow into
`fuzz_alloc_ops.c`, and CI requires the fuzzer to find it. A harness that has never
found anything is indistinguishable from one that is not running the code under test.

The plant lives in the harness, never in `src/`, so production code is never built with
a deliberate defect.
