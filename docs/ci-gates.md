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
| **ctest matrix** | correctness on ubuntu / windows-MSVC / windows-MinGW / macos, `MI_PPROF` on and off, `MI_DEBUG_FULL`, and shared-library builds on all three of ubuntu, MSVC and MinGW. **macOS and windows-MinGW are gated from Linux-built bundles run on one runner each** (`macos-bundles.yml`, #277 phase B; `windows-bundles.yml`, phase C — which also moves that lane from msvcrt to UCRT); the native macOS and MinGW jobs are informational during the comparison window | `ci/bundle_coverage.py` fails if any test the native `ctest` would run is missing from a bundle |
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

Every lowered test is then scanned for absolute paths that the bundle does not carry --
across `argv`, every `env` value (split on `os.pathsep`) and `cwd`, not just `argv[0]` --
and any hit is a hard error naming the test and the field. `PathRewriter` only rewrites
paths under the *build* directory, so a source-tree, toolchain or runner-home path would
otherwise be replayed verbatim on a machine that does not have it. Windows drive-qualified
and UNC paths count as absolute even though `os.path.isabs` says otherwise on Linux, which
is where every cross bundle is built.

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

## macOS via cross-built bundles

Issue #277 phase B. The macOS gates no longer build on macOS. `macos-bundles.yml` builds
the arm64 test binaries on **ubuntu-latest** through soldr's Darwin toolchain, packs them
with `ci/bundle_tests.py`, and one `macos-latest` job runs everything serially.

The reason is queue wait, not build time. Over 20 runs per workflow, `cross.yml`'s Apple
rows executed in 8–14 s and waited up to 5h26m (arm64) / 6h13m (Intel), p90 1h29m / 2h32m,
while every ubuntu row waited 4–5 s. Six macOS jobs per push become one.

| Linux job | produces | replaces |
|---|---|---|
| `build-macos (macos-arm64-release)` | `bundle-macos-arm64-release` | `ctest (macos-latest)` |
| `build-macos (macos-arm64-debug-full)` | `bundle-macos-arm64-debug-full` | `ctest-debug-full (macos-latest)` |
| `build-macos (macos-arm64-leak)` | `bundle-macos-arm64-leak` | the `memory-gate` positive control |
| `build-rust (aarch64-apple-darwin)` | `rust-test-bins-…` | `cross.yml` `test (aarch64-apple-darwin)` |
| `build-rust (x86_64-apple-darwin)` | `rust-test-bins-…` | `cross.yml` `test (x86_64-apple-darwin)`, run under Rosetta 2 |

### The toolchain

`cmake/toolchains/soldr-aarch64-apple-darwin.cmake` consumes only what `soldr prepare
--target aarch64-apple-darwin` exports (`CC_`/`CXX_`/`CFLAGS_`/`CXXFLAGS_`/`AR_`/`RANLIB_`
per triple, plus `SDKROOT`), so no path, SDK version or compiler version is written down
in the repository. One line in it is load-bearing:

```cmake
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
```

`CMakeLists.txt`'s `find_link_library()` falls back to `find_library()` when
`check_linker_flag()` rejects `-l<name>`, and a Darwin link rejects both `-lrt` and
`-latomic` — so without confining the search to the SDK, the host's ELF `librt.so` and
`libatomic.so` are found and handed to a Mach-O link. `build-macos` asserts on the
resolved list (`Link libraries : pthread`, exactly), not on the flag that was passed in.

### What the build job proves before shipping a bundle

Cross-compilation can produce a plausible-looking file that does nothing on the target, so
the artifact is inspected rather than trusted:

- arm64 Mach-O with `LC_BUILD_VERSION` (platform, `minos`, `sdk` printed into the job
  summary — soldr's SDK moves, so it is recorded, never asserted against a constant)
- `__DATA,__interpose` and `__DATA,__thread_vars` present: `MI_OSX_INTERPOSE` puts the
  malloc replacements in the first, and a dylib that quietly lost the section would load
  on the runner and override nothing
- `LC_CODE_SIGNATURE` present (ad-hoc, from `ld64.lld`) — arm64 macOS kills an unsigned
  image before `main()` with no diagnostic
- `tests.json` contains no absolute path, and the bundle carries a `.dylib`

The bundle is shipped as a **tar**, not as a plain artifact upload: `upload-artifact@v4`
drops the executable bit and does not preserve symlinks, and the bundle needs both (every
test executable, and the `libmimalloc.dylib → libmimalloc.3.dylib` chain).

### Coverage accounting

`ci/bundle_coverage.py` runs on every `run-macos`. The runner configures the same two
CMake trees with **Xcode's** clang (`--show-only=json-v1` needs a configured tree, not a
built one) and the script fails if any test name the native `ctest` would run is absent
from the bundles. A name only the bundle has is reported, not fatal.

Two coverage facts are stated rather than gated, because nothing can gate them:

- the **8 doctests** in `rust/mimalloc-pprof/src/lib.rs` run under `cargo test` and cannot
  run from a `--tests` binary; after phase B they are Linux-only (#277 §4)
- the native compile-compat build uses Xcode clang and runs **no tests**: it catches
  compile-only breakage, not codegen or runtime differences between Apple clang and
  soldr's clang

### Memory-gate baselines are per toolchain now

`macos-latest` now executes two differently-built arm64 binaries, and they report the same
`platform` and the same `platform.machine()`. `ci/memory_gate.py` therefore accepts
`--arch` and `--compiler`, which join the baseline file's name. Both are optional and
absent by default, so `linux-pprof1.json`, `windows-pprof1.json` and `macos-pprof1.json`
keep their names and keep matching the runs that produced them — the ubuntu baseline is
untouched. The soldr lane asks for `macos-arm64-soldr-clang-21-pprof1.json`, which does
not exist yet, so its first run takes the existing "no baseline → bootstrap it" path and
uploads its JSON. Its positive control is skipped with a warning until that baseline is
committed, because `control` requires `check` to *fail* and a missing baseline is not a
failure.

Which of those two states a run is in is decided by `memory_gate.py where`, not by
`check`'s status. `check` exits 2 both for "no baseline" and for "this run's JSON could
not be read", so a step that treated its 2 as "bootstrap me" would turn a **crashed gate
binary** into a green run with a reassuring warning. `where` answers **3** for "no
baseline" and keeps **2** for "cannot read this run", and `run-macos` additionally
requires all eight result files to exist before it calls the gate at all.

### Rollout

The native jobs stay as a control arm, `continue-on-error: true`, for at least ten pushes:
`c-unit.yml`'s `ctest`, `ctest-debug-full` and `memory-gate` macOS rows, `cross.yml`'s two
Apple `test` rows, and `rust-native.yml`'s macOS row. Each carries a dated TODO naming
what deletes it. A permanently-yellow job is worse than an absent one, so the comparison
window is meant to end.

## windows-gnu via cross-built bundles

Issue #277 phase C. The MinGW gates no longer build on Windows. `windows-bundles.yml`
builds the x86-64 test binaries on **ubuntu-latest** through soldr's mingw-w64 toolchain,
packs them with `ci/bundle_tests.py`, and one `windows-latest` job runs everything
serially — including the Rust `x86_64-pc-windows-gnu` test binaries, whose *build* also
moves to Linux (`cross.yml`'s `build-win-gnu` ran on `windows-latest`).

| Linux job | produces | replaces |
|---|---|---|
| `build-windows-gnu (windows-gnu-x64-release)` | `bundle-windows-gnu-x64-release` | `ctest-win-gnu` |
| `build-windows-gnu (windows-gnu-x64-debug-full)` | `bundle-windows-gnu-x64-debug-full` | `ctest-debug-full-win-gnu` |
| `build-windows-gnu (windows-gnu-x64-shared)` | `bundle-windows-gnu-x64-shared` | `ctest-shared-win-gnu` |
| `build-windows-gnu (windows-gnu-x64-leak)` | `bundle-windows-gnu-x64-leak` | *(new)* the memory gate's positive control |
| `build-rust (x86_64-pc-windows-gnu)` | `rust-test-bins-…` | `cross.yml` `build-win-gnu` + `test (x86_64-pc-windows-gnu)`, and `rust-native.yml` `test-win-gnu` |

### The CRT changes: msvcrt → UCRT

This is the one place where the bundle lane is **not** a like-for-like replacement, so it
is stated rather than buried.

The three native jobs use `msys2/setup-msys2` with `msystem: MINGW64`, which is a
**msvcrt** environment. soldr provisions mingw-w64-gcc 15.3.0 built
`--with-default-msvcrt=ucrt`, which is **UCRT**: `libmsvcrt.a` in its sysroot is an archive
of `lib64_libucrt_*.o`, `_mingw.h` defines `_UCRT`, and a hello-world imports
`api-ms-win-crt-*-l1-1-0.dll` rather than `msvcrt.dll`. soldr offers no msvcrt variant of
the lane — `libmsvcrt-os.a` exists in the sysroot, but the headers are UCRT unconditionally,
so `-mcrtdll=msvcrt-os` would pair UCRT declarations with a legacy runtime. **The decision
is to accept UCRT**, which is also the environment MSYS2 itself now defaults to (`UCRT64`)
and the one Microsoft ships on every supported Windows.

Accepting it has two consequences, because upstream detects UCRT from `$ENV{MSYSTEM}` and
that can only ever be set by an MSYS2 shell:

1. `cmake/toolchains/soldr-x86_64-pc-windows-gnu.cmake` sets `MI_MINGW_UCRT64 ON`, and
   `CMakeLists.txt` now honours a caller-set value alongside the `MSYSTEM` probe. Without
   it, a UCRT build would be compiled *as if* it were msvcrt:
   `src/prim/windows/prim.c:794` would register initialisation through a constructor
   attribute instead of the `.CRT$XLB` TLS-callback table. `build-windows-gnu` greps the
   configure log for `MI_MINGW_UCRT64=1` in the resolved compiler defines — a flag that
   never reaches the compiler is this repository's most-repeated CI bug.
2. `mimalloc-test-stress-dynamic.exe` imports `mimalloc.dll` **after** the UCRT api-sets,
   which is exactly the case `bin/minject.exe` exists for. Upstream runs it as a CMake
   `POST_BUILD` command; minject is a PE binary, so that command is now guarded with
   `AND CMAKE_HOST_WIN32` and `run-windows-gnu` runs minject itself, deriving
   `--postfix` from the bundle's own library name (the Debug config renames it
   `mimalloc-debug.dll`).

   minject runs **without `--inplace`**, so it writes a sibling `*-mi.exe` and the
   bundle's own executable is left exactly as linked. `ctest-win-gnu` does not minject
   anything either (MSYS2 MINGW64 is msvcrt, so `MI_MINGW_UCRT64` is off there), so the
   bundle run stays like-for-like and the injected binary is an extra artifact for the
   probe below rather than a change to what the suite runs.

### Is the override actually exercised? (measured, and gated comparatively)

`test-stress-dynamic` asserts nothing about redirection — it links `mi_version()` so the
DLL loads, then allocates through the CRT. `MIMALLOC_VERBOSE=1` makes `src/init.c:530`
print `malloc is redirected.` exactly when `_mi_is_redirected()` is true, which is the only
direct evidence that the redirection module patched the runtime. `run-windows-gnu` collects
that for three binaries and puts them in the job summary:

| lane | binary |
|---|---|
| MSYS2 MINGW64 (**msvcrt**) — what `ctest-win-gnu` runs today | built by the native compile-compat step |
| soldr mingw-w64 (**UCRT**) bundle, as linked | `mimalloc-test-stress-dynamic.exe` |
| soldr mingw-w64 (**UCRT**) bundle, after `minject` | `mimalloc-test-stress-dynamic-mi.exe` |

The gate is the **comparison**: if the msvcrt lane redirects and neither UCRT binary does,
the job fails. Requiring the bundle to redirect where the job it replaces never did would
be a new gate smuggled in under the word "parity"; if none of the three redirects, that is
reported as a `::warning::` and recorded here, because it says the dynamic override is not
exercised on win-gnu *at all* today — a pre-existing gap, not something this phase caused.

### The runtime DLL

soldr's mingw links `mimalloc.dll` against `libgcc_s_seh-1.dll`, which no plain
`windows-latest` runner has, and a missing DLL is a dialog-free `0xC0000135` exit rather
than a test failure that says what happened. `bundle_tests.py --dll-search-dir` turns on a
transitive PE import scan (`objdump -p`) that copies every non-system DLL the bundle's
executables import — and **refuses to write a bundle** whose executables import something
neither carried nor in its Windows system-DLL allowlist.

Shipping the DLL was preferred over building `-static-libgcc`: the DLL is a property of
the *bundle*, so the binary under test stays byte-for-byte the one an ordinary mingw link
produces. Only `libgcc_s_seh-1.dll` is actually needed — this gcc's thread model is
`win32`, so nothing pulls in `libwinpthread-1.dll`, and the C build never links
`libstdc++-6.dll`.

### What the build job proves before shipping a bundle

- `file format pei-x86-64`
- `.CRT$XLB`, `.CRT$XLY` and `.CRT$XIB` present in `prim.c.obj`, **and** a non-empty
  Thread Storage Directory (data-directory entry 9) in the linked DLL — the section being
  compiled is not the same as the callback surviving the link
- `mimalloc-redirect.dll` in the DLL's import table: the Windows override goes through the
  redirection module, so a DLL that lost that import overrides nothing
- resolved `Link libraries : psapi;shell32;user32;advapi32;bcrypt`, exactly
- `tests.json` contains no absolute path; `libgcc_s_seh-1.dll` and `mimalloc-redirect.dll`
  are in the bundle

`CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY` is set on this lane too, but unlike Darwin it is
**belt-and-braces rather than load-bearing**: CMake's Windows-GNU
`CMAKE_FIND_LIBRARY_SUFFIXES` are `.dll.a;.a;.lib`, so the host's `librt.so` is not a
candidate for `find_link_library()` even with the search unconfined (verified both ways).
#277's claim that all three lanes need it is true only for Darwin.

### Coverage accounting

`ci/bundle_coverage.py` runs on every `run-windows-gnu`. The runner installs MSYS2
MINGW64, configures the same three CMake trees with **msvcrt gcc**, and the script fails if
any test name the native `ctest` would run is absent from the bundles. That same MSYS2 step
is the native compile-compat check: it *builds* the Release tree (no tests — the bundles do
that), so an msvcrt-only or MSYS2-header-only compile break is still caught.

Stated rather than gated:

- the `windows-gnu-x64-leak` bundle and the memory gate on this lane are **new** coverage:
  no native win-gnu job runs the memory gate today (only `memory-gate (windows-latest)`
  does, on MSVC). It bootstraps yellow — see below.
- `rust-native.yml`'s `test-win-gnu` runs `cargo test`, which includes doctests; the
  cross-built `--tests` binaries do not. Those 8 doctests were already Linux-only after
  phase B.

### Memory-gate baseline

The win-gnu lane asks for `ci/memory-baselines/windows-x64-soldr-mingw-gcc-15-pprof1.json`
via `--arch x64 --compiler soldr-mingw-gcc-15`, so it cannot borrow
`windows-pprof1.json`, which MSVC recorded on the same runner. That file does not exist
yet, so the first runs take the "no baseline → bootstrap it" path (`memory_gate.py where`
exits 3) and upload their JSON; the positive control is skipped with a warning until the
baseline is committed, because `control` requires `check` to *fail* and a missing baseline
is not a failure.

### Rollout

The native jobs stay as a control arm, `continue-on-error: true`, for at least ten pushes:
`c-unit.yml`'s `ctest-win-gnu`, `ctest-debug-full-win-gnu` and `ctest-shared-win-gnu`,
`rust-native.yml`'s `test-win-gnu`, and `cross.yml`'s `build-win-gnu` +
`test (x86_64-pc-windows-gnu)`. Each carries a dated TODO naming what deletes it. Because
this phase changes the CRT, that window is the only place the two runtimes are exercised
side by side — do not delete the msvcrt arm before the comparison has actually happened.

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
