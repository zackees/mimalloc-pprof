# CI gates

*Part of the [mimalloc-pprof](../README.md) documentation.*

Every gate below runs on each PR and is a **hard failure**. Where a gate can have a
*positive control* — a deliberately broken input it must catch — it has one, because a
gate that has never been observed to fire proves nothing.

That is not a hypothetical standard. **Seven gates in this repository were found to be
verifying nothing**, each discovered by asking "has this ever actually failed?":

- the arm64 instruction scanner matched nothing at all and reported "clean"
- the memory-gate's leak control existed but was never invoked
- the ASan job's branch filter excluded every branch we work on
- the cross-build pipeline discarded its own diagnostics
- `MI_TRACK_ASAN` silently self-disabled when its header was missing
- `MI_GUARDED` was documented as default-on in debug builds and had **never once** been
  enabled — two independent dead CMake constructs
- the fuzz job's "did ASan report this?" check matched libFuzzer's own boilerplate line
  *"Combine libFuzzer with AddressSanitizer…"* — so the string proving ASan worked was
  libFuzzer saying it was not in use

| Gate | What it catches | Positive control |
|---|---|---|
| **memory-gate** (`ci/memory_gate.py`) | peak memory or thread-count regressions vs a committed per-platform baseline | builds a copy with an injected leak; the gate must fail — verified at +212% / +98% / +27% on linux/windows/macos |
| **isa-baseline** (`ci/check_isa_baseline.py`) | binaries containing instructions above the CPU baseline, which SIGILL on older hardware | builds with `MI_OPT_ARCH=ON`; the scanner must fire. The parser also self-tests against x86 and arm64 fixtures on every run |
| **ctest matrix** | correctness on ubuntu / windows-MSVC / windows-MinGW / macos, `MI_PPROF` on and off, `MI_DEBUG_FULL`, and shared-library builds on all three of ubuntu, MSVC and MinGW | — |
| **ctest-guarded** | the `MI_GUARDED` guard-page path, run twice: at the default sample rate and again with `MIMALLOC_GUARDED_SAMPLE_RATE=1` so every eligible allocation is guarded | configure step greps the resolved compiler defines for `MI_GUARDED=1`, since the original bug was the flag never reaching the compiler |
| **asan** | use-after-free, overflow and leaks under AddressSanitizer | — |
| **fuzz** (`test/fuzz/`) | crashes from structured random allocator-API sequences, with ASan as the oracle | builds with a planted use-after-free and requires an anchored `(ERROR\|SUMMARY): AddressSanitizer:` report naming it |
| **amalgamation-drift** | a C change that never reached the vendored copy the Rust crate compiles — which broke `main` twice before this gate existed | — |
| **doc-snippets** (`ci/check_doc_snippets.py`) | fenced ```c examples in `README.md`/`docs/*.md` that don't compile against the real headers — caught one in PR #259 (a snippet using `mi_prof_config_t_decl`/`mi_prof_start_ex` with no `#include` at all) | `--self-test` plants a snippet calling an undeclared mimalloc function; the checker must reject it, on both gcc and clang |
| **python-lint** | the gate scripts themselves — `ruff` + `pyright --strict` | — |
| **zero-tracking** | correctness and footprint of `mi_option_purge_zeroes`, reported as paired interleaved A/B medians with the within-arm spread alongside | — |

## Test bundles

Issue #277 wants the macOS and Windows test binaries built once on Linux (through the
soldr lanes, #277 §2) and executed on one runner per OS, instead of one fresh VM per job.
That needs a *test bundle*: a directory a runner can execute with **no CMake and no repo
checkout**.

`uv run ci/bundle_tests.py <build-dir> <out-dir> [--config Debug]` writes one.
`uv run ci/run_test_bundle.py <bundle> [--only NAME…] [--env K=V…] [--timeout-scale F]
[--junit out.xml] [--compare-junit ctest.xml]` replays it serially and prints a
ctest-shaped summary.

### `tests.json`

One entry per test: `name`, `argv`, `env`, `cwd`, `timeout`, `expect_nonzero`,
`expect_text`, `labels`. Paths inside `argv` and `env` values are written as
`${BUNDLE}/<file>`, which the runner expands to the bundle's own absolute path; files are
flattened into the bundle root so a Windows DLL sits beside its exe and a Linux `.so` is
found through `LD_LIBRARY_PATH` rather than a build-tree RPATH. A basename collision is a
hard error, never a silent overwrite.

### Lowering rules

`ctest --show-only=json-v1` does not emit a list of plain executables. On the
`MI_DEBUG_FULL` tree, **7 of 31 tests invoke `cmake` itself**, and both shapes are lowered
into manifest fields:

| ctest command | lowered to |
|---|---|
| `cmake -E env K=V … <exe> [args]` | `env` entries plus the real `argv`; leading `K=V` tokens are consumed up to the first token that is not one |
| `cmake -D TEST_EXE=… -D TEST_ARG=… -D EXPECTED_TEXT=… -P test/run-negative.cmake` | `argv = [TEST_EXE, TEST_ARG?]`, `expect_nonzero: true`, `expect_text: EXPECTED_TEXT`, `timeout: 10` |

The negative-control semantics come from `test/run-negative.cmake` and are reproduced
exactly, including the part that is easy to get backwards: **a timeout is a failure**, not
the expected non-zero exit — the script's own words are "negative control timed out
instead of failing fast" — and the expected substring is searched in the *combined*
stdout+stderr, so a control that fails for the wrong reason stays red.

Test properties are an **allowlist** (`ENVIRONMENT`, `TIMEOUT`, `WORKING_DIRECTORY`,
`LABELS`, `WILL_FAIL`, `DISABLED`). Anything else — `PASS_REGULAR_EXPRESSION`,
`SKIP_RETURN_CODE`, `RESOURCE_LOCK` — is a hard error naming the test, as is any `cmake`
argv shape not in the table, and so is an empty suite. All of that is the same principle
as the rest of this document: a bundle that quietly carries fewer tests than ctest ran
would report green while verifying less, which is precisely the failure mode the seven
dead gates above shared.

### The gate

`bundle-roundtrip` (in `c-unit.yml`, ubuntu, `MI_PPROF=ON MI_DEBUG_FULL=ON`) builds, runs
`ctest --output-junit`, bundles, **moves the build tree away**, replays the bundle, and
requires the same test names with the same pass/fail (`--compare-junit`). Moving the build
tree is the load-bearing step: the executables carry an RPATH into it, so without that
`mv` a broken bundle would pass on the build machine by loading the original libraries.

## Concurrency: superseded runs are cancelled

Every workflow now carries a `concurrency:` group. A second push to the same ref cancels
the first push's still-queued jobs instead of letting both compete for a runner. This is
issue #277 phase 0, and the reason it is worth a paragraph is that the cost it removes is
not build time:

| workflow | runner | jobs | queue p50 | queue p90 | queue max | exec p50 |
|---|---|---|---|---|---|---|
| `c-unit.yml` | `ubuntu-latest` | 160 | 4s | 19s | 1m10s | 45s |
| `c-unit.yml` | `macos-latest` | 60 | 21s | 2m03s | 3m05s | 48s |
| `cross.yml` | `macos-latest` | 20 | 9m25s | 1h29m | **5h26m** | 8s |
| `cross.yml` | `macos-15-intel` | 20 | 9m23s | 2h32m | **6h13m** | 9s |

Those Apple rows *execute* in under fifteen seconds and *wait* for hours. Cancelling a
superseded push does not make any job faster; it stops a dead push from holding a macOS
slot that a live one needs.

Not every workflow may be cancelled. Anything that publishes keeps `cancel-in-progress:
false`, because a half-finished publish is worse than a redundant one:

- `auto-release.yml`, `release.yaml` — upload release assets / `cargo publish`
- `benchmark-stats.yml`, `benchmark-latency.yml`, `benchmark-memory.yml`,
  `benchmark-scaling.yml` — push the sealed `benchmark-stats` branch and deploy Pages;
  these deliberately **share** one group (`benchmark-stats-production`) to serialise
  against each other, so their group is not per-workflow
- `star-history.yml` — commits a regenerated chart
- `stale.yaml` — closes and labels issues; a half-run leaves the repo partly swept

`fuzz.yml` is the one subtle case: it has a `schedule:` trigger, and a scheduled run
carries `github.ref == refs/heads/main`. Its group therefore includes `github.event_name`,
so the nightly long run and a `main` push cannot cancel each other.

**Measuring it.** `uv run ci/ci_queue_wait.py --limit 20 c-unit.yml cross.yml
rust-native.yml` reads the Actions API through `gh` and prints, per `runs-on` label,
p50/p90/max of `started_at - created_at` (queue wait) and `completed_at - started_at`
(execution), with the job count. One caveat is baked into the tool's output and repeated
here: a job with `needs:` is *created* when the run is created, so a `cross.yml`
`test (*)` row measures "pool wait + upstream build", while `c-unit.yml` and
`rust-native.yml` have no `needs:` edges and measure pool wait alone. `--jobs-json` replays
a saved payload, which is how `ci/tests/test_ci_queue_wait.py` checks the arithmetic
without touching the network.

Three details worth stating, because each was an assumption that measurement overturned:

- **Peak memory is not a low-variance signal.** Repeated runs of the same unchanged
  binary span 6–12% on CI runners. The memory gate therefore compares the **minimum of
  four runs** and prints the observed spread every time, warning if it ever approaches
  the tolerance. A gate that flakes gets ignored, and an ignored gate is worse than none.
- **The gate scripts are gating code.** Several of the silent failures above were Python
  or YAML bugs rather than C bugs — the arm64 instruction scanner matched nothing at all
  and reported "clean". Hence `pyright --strict` over `ci/`, with the result schema
  declared rather than indexed by hope.
- **Match report headers, not prose.** The fuzz control's `grep -qiE "AddressSanitizer"`
  was satisfied by libFuzzer's advice *to use* AddressSanitizer. Assertions about tool
  output should anchor on that tool's actual report format (`^(==[0-9]+==)?(ERROR|SUMMARY):`),
  never on a keyword that can appear in an explanatory sentence.
