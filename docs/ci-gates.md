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
| **memory-gate** (`ci/memory_gate.py`) | peak memory or thread-count regressions vs a committed per-platform/arch/compiler baseline | builds a copy with an injected leak; the gate must fail — verified at +212% / +98% / +27% on linux/windows/macos. The macOS lane now measures inside the `dockurr/macos` guest and bootstraps its own `macos-x86_64-soldr-clang-21-pprof1.json` |
| **isa-baseline** (`ci/check_isa_baseline.py`) | binaries containing instructions above the CPU baseline, which SIGILL on older hardware | builds with `MI_OPT_ARCH=ON`; the scanner must fire. The parser also self-tests against x86 and arm64 fixtures on every run |
| **ctest matrix** | correctness on ubuntu / windows-MSVC / windows-MinGW / macos, `MI_PPROF` on and off, `MI_DEBUG_FULL`, and shared-library builds on all three of ubuntu, MSVC and MinGW. **macOS uses no Apple hardware** (#277 phase B2): both arches are cross-built on Linux, x86_64 is executed under `dockurr/macos` on a Linux runner, arm64 is compile-only | `ci/bundle_coverage.py` fails if a test in the arm64 bundle is missing from the executed x86_64 one; `ci/lint_no_macos_runners.py` fails if any workflow names a macOS runner |
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

## macOS: cross-built on Linux, x86_64 executed under emulation

Issue #277 phase B, then **phase B2**. **No workflow in this repository schedules a job
onto a macOS runner.** Both Apple architectures are cross-compiled on `ubuntu-latest`
through soldr's Darwin toolchains, and the x86_64 bundle is *executed* on an
`ubuntu-24.04` runner inside a [`dockurr/macos`](https://github.com/dockur/macos) guest
(QEMU + KVM). `ci/lint_no_macos_runners.py` fails `python-lint` if a macOS runner label
reappears in any workflow.

Phase B's motivation was queue wait: over 20 runs per workflow, `cross.yml`'s Apple rows
executed in 8–14 s and waited up to 5h26m (arm64) / 6h13m (Intel) — p90 1h29m / 2h32m —
while every ubuntu row waited 4–5 s. Phase B2's motivation is a product requirement:
mimalloc-pprof ships inside cross-compiled code, so the Linux-cross-built artifact is the
one that must be proven to work, not a convenience.

| arch | built | executed | gate |
|---|---|---|---|
| `x86_64-apple-darwin` | Linux, soldr clang 21 | **yes** — `dockurr/macos` guest on `ubuntu-24.04` | full: ctest-equivalent bundles, Rust test binaries, memory gate + leak control |
| `aarch64-apple-darwin` | Linux, soldr clang 21 | no | **compile-only**, plus the Mach-O evidence below and a suite-parity check against x86_64 |

### Why arm64 is compile-only, and what that costs

dockur publishes no arm64 image, and Apple ships no arm64 macOS that boots under QEMU on
Linux; the only way to execute arm64 Mach-O binaries is Apple hardware, which is exactly
what phase B2 removes. So arm64 gets a build, the header assertions below, and
`ci/bundle_coverage.py` comparing its test-name set against the x86_64 bundle that *is*
executed — which stops an arm64-only test from silently never running anywhere, but does
not catch an arm64 runtime bug.

Two further things are stated rather than gated, because nothing here can gate them:

- **Nothing compiles this code with Apple's clang or against Xcode's SDK any more.** The
  native compile-compat build went with the runners. An Apple-clang-specific rejection or
  an Xcode header change now surfaces downstream, not here.
- The **8 doctests** in `rust/mimalloc-pprof/src/lib.rs` run under `cargo test` and cannot
  run from a `--tests` binary; they are Linux-only (#277 §4).

### The toolchains

`cmake/toolchains/soldr-aarch64-apple-darwin.cmake` and
`cmake/toolchains/soldr-x86_64-apple-darwin.cmake` consume only what `soldr prepare
--target <triple>` exports (`CC_`/`CXX_`/`CFLAGS_`/`CXXFLAGS_`/`AR_`/`RANLIB_` per triple,
plus `SDKROOT`), so no path, SDK version or compiler version is written down in the
repository. soldr 0.9.11 reports `dispatch=blessed-darwin` for both triples. One line in
each is load-bearing:

```cmake
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
```

`CMakeLists.txt`'s `find_link_library()` falls back to `find_library()` when
`check_linker_flag()` rejects `-l<name>`, and a Darwin link rejects both `-lrt` and
`-latomic` — so without confining the search to the SDK, the host's ELF `librt.so` and
`libatomic.so` are found and handed to a Mach-O link. This matters *more* on the x86_64
lane, where host and target share an architecture and a leaked host library is not even
rejected for the obvious reason. `build-macos` asserts on the resolved list
(`Link libraries : pthread`, exactly), not on the flag that was passed in.

Neither file sets `CMAKE_OSX_ARCHITECTURES`: it is the switch `CMakeLists.txt` reads to
choose universal-binary `-Xarch_*` flags. On x86_64 it decides nothing else either, since
`CMakeLists.txt` leaves `MI_OPT_ARCH` **off** for `MI_ARCH=x64` and force-enables it only
for arm64 — so the Intel build emits no `-march` and stays runnable on the guest's
emulated CPU.

### What the build job proves before shipping a bundle

Cross-compilation can produce a plausible-looking file that does nothing on the target, so
the artifact is inspected rather than trusted. Common to both arches:

- `__DATA,__interpose` and `__DATA,__thread_vars` present: `MI_OSX_INTERPOSE` puts the
  malloc replacements in the first, and a dylib that quietly lost the section would load
  in the guest and override nothing
- `tests.json` contains no absolute path, and the bundle carries a `.dylib`

Per arch, and the differences are real rather than an oversight:

| | arm64 | x86_64 |
|---|---|---|
| header | `MH_MAGIC_64 ARM64` | `MH_MAGIC_64 X86_64` |
| version load command | `LC_BUILD_VERSION` | `LC_VERSION_MIN_MACOSX` **or** `LC_BUILD_VERSION` |
| `LC_CODE_SIGNATURE` | **required** | not required |

soldr's x86_64 lane targets `-mmacosx-version-min=10.12`, which predates
`LC_BUILD_VERSION`, so `ld64.lld` emits the older `LC_VERSION_MIN_MACOSX`; the job accepts
either rather than pinning today's, so a soldr bump that raises `minos` does not break the
build for no reason. And only arm64 macOS refuses to run an unsigned image — on arm64 the
kernel kills the process before `main()` with no diagnostic, which is why that assertion
exists there and would be noise here.

The bundle is shipped as a **tar**, not as a plain artifact upload: `upload-artifact@v4`
drops the executable bit and does not preserve symlinks, and the bundle needs both (every
test executable, and the `libmimalloc.dylib → libmimalloc.3.dylib` chain).

### Running macOS on a Linux runner

`run-macos-x64-dockur` boots a `dockurr/macos` guest, waits for ssh, copies the bundles
and the Rust test binaries in with `ci/dockur_guest.sh`, and runs everything serially.
Three things about it are not obvious:

**There is no unattended macOS install.** dockur/windows takes an answer file; the macOS
image has no equivalent — `/run/install.sh` exposes no automation hook and no
unattended/answer-file variable exists. Apple's Recovery installer is a GUI that has to be
clicked through in dockur's web viewer. So the disk is built **once, by a human**, by
`macos-golden-bootstrap.yml` (`workflow_dispatch`, `timeout-minutes: 350`, tunnels the
viewer out with cloudflared and prints the click-by-click procedure), compressed, and
stored under an `actions/cache` key. A cache miss in `run-macos-x64-dockur` is a hard
error naming that workflow, never a silent skip — so **until that bootstrap has been run
once, `run-macos-x64-dockur` is red**, on the PR that introduced it and on every push to
`main` afterwards. That is deliberate: a gate that says "I have no disk to run on" is
worth more than one that skips itself into a green tick. It fails in about two minutes.

**`CPU_MODEL` is not dockur's default, and it is the difference between working and not.**
Measured on an AMD Zen 2 host (Ryzen 7 3700X): with dockur's default profile for macOS 13
(`Haswell-noTSX`) the guest resets immediately after `HANDOFF TO XNU` and loops forever —
**987 resets in 7 hours, 685 MB ever written, no installer screen**. Holding everything
else identical and setting `CPU_MODEL=Skylake-Client-v4` boots macOS Recovery in about
**5 minutes**. dockur#268 documents the AMD symptom and the single-core mitigation
(`CPU_CORES=1`, which both jobs apply after detecting `AuthenticAMD`); the CPU model is
the other half of it. A guest that is *slow* and a guest that is *looping* look identical
from outside, so the ssh-wait step prints the `HANDOFF TO XNU` count on timeout — a
climbing count is a panic loop, a flat one is a slow boot.

**The guest ships its own Python.** macOS's `/usr/bin/python3` is a Command Line Tools
stub that opens a GUI dialog on a machine with nobody to click it, and the golden image
deliberately carries no Xcode (every GB is a GB of cache on every run). The job downloads
a relocatable `python-build-standalone` interpreter on the Linux side and copies it in;
`run_test_bundle.py` and `memory_gate.py` are stdlib-only, so that is enough.

`ci/dockur_guest.sh` is the only place the ssh incantation lives. It authenticates with a
fixed local password rather than a repository secret: the guest is created fresh from a
disk image on that runner, reachable only over loopback, and holds nothing but test
binaries.

> Apple's macOS Software Licence Agreement permits running macOS in a virtual machine only
> on Apple-branded hardware; these jobs run it under QEMU on non-Apple Linux runners, and
> dockur/macos states the same caveat in its own README. Recorded as a fact for whoever
> owns that decision. The golden disk is kept in a private `actions/cache` entry, not
> published as a release asset.

### Coverage accounting

`ci/bundle_coverage.py` runs on every `run-macos-x64-dockur`. There is no macOS runner
left to enumerate a native ctest suite on, so the reference side is now the **arm64
bundle** and the candidate side the executed **x86_64** one; the script fails if a name on
the reference side is missing from the candidate. It reads either shape — `ctest
--show-only=json-v1` or a bundle `tests.json` — because `bundle_tests.py` derives one from
the other, and `--heading`/`--names` make the report say which comparison was actually
made. Both configs currently agree exactly: **27 tests** release, **34** debug-full.

### Memory-gate baselines are per toolchain now

Binaries built by different toolchains report the same `platform` and the same
`platform.machine()`, so `ci/memory_gate.py` accepts `--arch` and `--compiler`, which join
the baseline file's name. Both are optional and absent by default, so `linux-pprof1.json`
and `windows-pprof1.json` keep their names and keep matching the runs that produced them.
The dockur lane asks for `macos-x86_64-soldr-clang-21-pprof1.json`, which does not exist
yet, so its first run takes the "no baseline → bootstrap it" path and uploads its JSON;
its positive control is skipped with a warning until that baseline is committed, because
`control` requires `check` to *fail* and a missing baseline is not a failure. **These
numbers are taken under emulation**, which is a further reason the lane must not borrow
`macos-pprof1.json` — that file was recorded by Apple clang on native Intel hardware and
is now orphaned.

Which of those two states a run is in is decided by `memory_gate.py where`, not by
`check`'s status. `check` exits 2 both for "no baseline" and for "this run's JSON could
not be read", so a step that treated its 2 as "bootstrap me" would turn a **crashed gate
binary** into a green run with a reassuring warning. `where` answers **3** for "no
baseline" and keeps **2** for "cannot read this run", and the job additionally requires
all eight result files to exist before it calls the gate at all.

### What was deleted

Phase B2 removed, not muted: `c-unit.yml`'s `ctest` / `ctest-debug-full` / `memory-gate`
macOS rows, `cross.yml`'s two Apple `test` rows, `rust-native.yml`'s macOS row,
`zero-tracking.yml`'s and `auto-release.yml`'s macOS rows, `macos-bundles.yml`'s
`run-macos` job (including its native compile-compat build), and the macOS rows of the
inherited upstream `release.yaml` and `test.yaml` — which also ends the dormant
`bin/bundle.sh` macOS tarballs those would have produced on a manual dispatch.

`auto-release.yml` uploads **no** macOS asset and never did: only its `ubuntu-latest` row
packages the release ZIP (the vendored C amalgamation, which is architecture-independent),
and its macOS row ran `xtask check`, `cargo test` and `cargo publish --dry-run` — all
platform-independent. Removing it changes no published artifact.

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
