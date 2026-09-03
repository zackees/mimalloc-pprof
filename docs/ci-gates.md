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
| **ctest matrix** | correctness on ubuntu / windows-MSVC / windows-MinGW / macos, `MI_PPROF` on and off, `MI_DEBUG_FULL`, and shared-library builds on all three of ubuntu, MSVC and MinGW. **macOS uses no Apple hardware** (#277 phase B2): both arches are cross-built on Linux, x86_64 is executed under `dockurr/macos` on a Linux runner, arm64 is compile-only. **windows-MinGW is gated from Linux-built bundles run on one Windows runner** (`windows-bundles.yml`, #277 phase C — which also moves that lane from msvcrt to UCRT); the native MSYS2 MinGW jobs are informational during the comparison window | `ci/bundle_coverage.py` fails if any test a build job's `ctest --show-only` listed was not executed by the run stage (#307), if a test in the arm64 bundle is missing from the executed x86_64 one, or if any test the native MinGW `ctest` would run is missing from a bundle; `ci/lint_no_macos_runners.py` fails if any workflow names a macOS runner |
| **guarded** (a `build` row + its scoped env variant in `run-linux`) | the `MI_GUARDED` guard-page path, run twice: at the default sample rate and again with `MIMALLOC_GUARDED_SAMPLE_RATE=1` so every eligible allocation is guarded | configure step greps the resolved compiler defines for `MI_GUARDED=1`, since the original bug was the flag never reaching the compiler |
| **asan** | use-after-free, overflow and leaks under AddressSanitizer | — |
| **fuzz** (`test/fuzz/`) | crashes from structured random allocator-API sequences, with ASan as the oracle | builds with a planted use-after-free and requires an anchored `(ERROR\|SUMMARY): AddressSanitizer:` report naming it |
| **amalgamation-drift** | a C change that never reached the vendored copy the Rust crate compiles — which broke `main` twice before this gate existed | — |
| **doc-snippets** (`ci/check_doc_snippets.py`) | fenced ```c examples in `README.md`/`docs/*.md` that don't compile against the real headers — caught one in PR #259 (a snippet using `mi_prof_config_t_decl`/`mi_prof_start_ex` with no `#include` at all) | `--self-test` plants a snippet calling an undeclared mimalloc function; the checker must reject it, on both gcc and clang |
| **python-lint** | the gate scripts themselves — `ruff` + `pyright --strict` | — |
| **zero-tracking** | correctness and footprint of `mi_option_purge_zeroes`, reported as paired interleaved A/B medians with the within-arm spread alongside. Linux runs it in its own path-filtered workflow; Windows runs it as a gated step inside `run-windows` (#277 phase E), and the *correctness* half is unconditional there because `test-zero-tracking*` are ordinary ctest tests carried in every bundle | — |
| **bun-surface** (`ci/check_bun_surface.py`) | whether `oven-sh/bun`'s exact `scripts/build/deps/mimalloc.ts` DirectBuild (`src/static.c` compiled as C++ with Bun's define set) can actually link against every `mi_*` symbol Bun's Rust FFI and C++ consumers reference, plus `MI_MAX_ALIGN_SIZE` / `mi_heap_area_t` / `mi_option_t` slot-0-42 ABI drift via `test/test-bun-surface.cpp`'s `static_assert`s | the script's own exit code already reflects reality (1 on any missing symbol or failed assert) |

### `bun-surface` is a hard gate

Issue #274 (Bun parity P9a). `mi_on_thread_idle` is part of Bun's linked surface
(`mimalloc_sys.rs:30`) and merged to `main` with Bun parity P7a (issue #299, `1dbbb8df`).
`ci/check_bun_surface.py` now reports zero missing symbols on both matrix rows
(`bun-surface (glibc)` and `bun-surface (musl)`, the latter run inside `alpine:3.20`),
so the job in `.github/workflows/c-unit.yml` runs with no `continue-on-error`: a
regression here blocks merge, the same as every other row in the table above.
`docs/bun-gap-analysis-2026-09-02.md` tracks the rest of what's still open (hole
purging, #302) before the Bun issue itself (P9c) can be filed.

## Test bundles

Issue #277 wants the macOS and Windows test binaries built once on Linux (through the
soldr lanes, #277 §2) and executed on one runner per OS, instead of one fresh VM per job.
That needs a *test bundle*: a directory a runner can execute with **no CMake and no repo
checkout**.

`uv run ci/bundle_tests.py <build-dir> <out-dir> [--config Debug]` writes one.
`uv run ci/run_test_bundle.py <bundle> [--only NAME…] [--env K=V…] [--timeout-scale F]
[--junit out.xml] [--compare-junit ctest.xml]` replays it serially and prints a
ctest-shaped summary.

Issue #307 made bundles the model for the Linux suite too, so the runner also takes
`--bundles dir1 dir2 … --jobs N [--env-variant [BUNDLE:]LABEL K=V …] [--junit-dir DIR]`:
several bundles in one wave, tests inside them running alongside each other, bounded by
`N`, with one JUnit file per bundle. See [c-unit: build once, run
everything](#c-unit-build-once-run-everything-at-once) below.

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
`LABELS`, `WILL_FAIL`, `DISABLED`, `RUN_SERIAL`). Anything else — `PASS_REGULAR_EXPRESSION`,
`SKIP_RETURN_CODE`, `RESOURCE_LOCK` — is a hard error naming the test, as is any `cmake`
argv shape not in the table, and so is an empty suite. All of that is the same principle
as the rest of this document: a bundle that quietly carries fewer tests than ctest ran
would report green while verifying less, which is precisely the failure mode the seven
dead gates above shared.

### The gate

The pass/fail equivalence check — build, `ctest --output-junit`, bundle, **move the build
tree away**, replay, require the same test names with the same results (`--compare-junit`)
— now lives in `uv run ci/verify_local.py --only bundle`, which is where a build tree
exists to compare against. Moving the build tree is the load-bearing step there: the
executables carry an RPATH into it, so without that `mv` a broken bundle would pass on the
build machine by loading the original libraries.

CI's version of the same claim is stronger in one way and weaker in another, and the
difference is worth stating plainly. `run-linux` has no build tree at all — nothing to
compare against, and nothing for an RPATH to fall back on, so the escape hatch the `mv`
guarded against cannot exist. What CI checks instead is coverage: for every configuration,
every name the build job's `ctest --show-only=json-v1` listed was **executed**, read back
out of the JUnit the run wrote (`ci/bundle_coverage.py`, which accepts a `.xml` candidate
side for exactly this). The reference side comes from ctest and the candidate side from
the run; neither is derived from the other.

## c-unit: build once, run everything at once

Issue #307. `c-unit.yml` used to be 16 jobs that each checked out, configured and **rebuilt
the library** for one configuration before running one slice of the suite. Run
33699111919 was 20 jobs and most of its wall time was the same compile repeated. The owner's
direction: *"All the tests need to be created all at once, all the time, and then the
runners need to run all the tests all at the same time, not a bunch of tests that rebuild
the product over and over."*

It is now two stages, the same model `windows-bundles.yml` and `macos-bundles.yml` use.

### Stage 1 — `build`: every configuration, once, in parallel

One matrix row per configuration, all on `ubuntu-latest` (the two musl rows inside
`container: alpine:3.20`), plus `build-bun-objects` and `build-windows-native`. Each row
runs no tests. `kind` says what it produces:

| kind | rows | artifact |
|---|---|---|
| `bundle` | `release`, `pprof-off`, `debug-full`, `guarded`, `shared`, `musl`, `musl-pprof-off` | a `ci/bundle_tests.py` bundle, tarred, **plus** `show-only-<config>.json` straight from `ctest --show-only=json-v1` |
| `control` | `memory-gate-leak` (`MI_BENCH_INJECT_LEAK=600000`) | a bundle that is never executed — only its `mimalloc-test-memory-gate` binary is, as the memory gate's positive control |
| `lib` | `isa-portable`, `isa-arch`, `diag-pprof-on`, `diag-pprof-off` | `libmimalloc*.a` (+ `compile_commands.json`) for the run stage to scan |

The bundles are **tarred**, not uploaded as loose files: `upload-artifact@v4` drops the
executable bit and flattens the `libmimalloc-debug.so → .so.3` SONAME symlink chain, either
of which produces a bundle that cannot start.

`show-only-<config>.json` is written by ctest itself and uploaded beside the bundle, rather
than derived from `tests.json`. That is deliberate: it is the reference side of the coverage
check, and a reference derived from the candidate proves nothing.

### Stage 2 — `run-linux` + `run-linux-serial`: every test, at once

No CMake, no compiler, no build tree on either. `run-linux` unpacks every Linux bundle and
runs the five glibc ones in a single wave:

```
uv run ci/run_test_bundle.py \
  --bundles bundle/release bundle/pprof-off bundle/debug-full bundle/guarded bundle/shared \
  --jobs 4 --select parallel \
  --env-variant guarded:sample-rate-1 MIMALLOC_GUARDED_SAMPLE_RATE=1 \
  --junit-dir results
```

`run-linux-serial` runs the same bundles with `--jobs 1 --select serial`, **on a runner of
its own, at the same time**. A separate VM is exclusive by construction, which is exactly
the guarantee "hold them back and run them last, alone" provides — without adding their
duration to the critical path. Measured on run 33704383536, before the split: the serial
group was 508 s of 2199 single-threaded test-seconds (23 %), about 8.5 of `run-linux`'s
16 minutes. The list of serial tests is *not* relaxed by the split; it still lives in
`CMakeLists.txt`.

Bundles run alongside each other *and* tests run alongside each other inside a bundle,
bounded by `--jobs 4` (the runner's vCPU count). The guarded config's second pass — every
eligible allocation guarded instead of 1-in-4000 — used to be a whole second serial `ctest`
run of that configuration; it is now an **env variant scoped to that one bundle**, reported
as `test-x [sample-rate-1]` with the JUnit `classname` left at `test-x`. The scoping is not
cosmetic: an unscoped variant would double all five bundles, including the serial group,
which is the part that does not divide by `--jobs`.

Then, on `run-linux`, as separate steps so each has that machine to itself: the musl
bundles, the memory gate and its leak control, the diagnostic and ISA scans, and the Bun
surface link.

Splitting the run stage in two means neither half can account for the whole suite on its
own, so the coverage check is a third job (`coverage`) that needs both and compares each
configuration against the **union** of the two JUnit sets — `ci/bundle_coverage.py` reads a
directory as the union of the files in it. Without that, each half would report green on
its own share, which is the "gate that verifies less" shape this document exists to stop.

### The serial group, and why it is in CMakeLists.txt

Concurrency must not change what a test asserts. Some tests measure the **machine**, not
just themselves:

- **process-wide RSS** — `test-memory-gate` (min-of-8 peak RSS against a committed
  baseline), `test-degenerate` (RSS must not grow per iteration), `test-process-rss` and
  `test-thread-idle-rss`. Under memory pressure from a parallel wave, the kernel's reclaim
  decisions, not the allocator's, move those numbers.
- **a wall-clock deadline** — `test-park-handoff{,-no-scavenger,-eager}` wait a bounded time
  for a hole sweep to land, and `test-profile-race{,-scavenger}` are oversubscribed thread
  races whose windows close on time rather than on work. On an oversubscribed 4-vCPU runner
  a missed deadline is indistinguishable from the regression the test exists to catch.
- **a subprocess lifecycle** — `test-subproc-lifecycle`.

Those carry `RUN_SERIAL TRUE` in `CMakeLists.txt` — ctest's own word for "run this with
nothing else running". `ci/bundle_tests.py` lowers the property (or a `serial` label) into
the manifest's `serial` flag, and `ci/run_test_bundle.py` runs those items one at a time
with nothing else alongside: after the parallel wave in a single `--select all` invocation
(what `--like-ci` and a local run do), or on a runner of their own via `--select serial`
(what CI does).

Encoding it there rather than in the workflow or in a name list inside the runner is the
point: a new test declares its own scheduling requirement where it is defined, and `ctest
-j` honours the same declaration locally. **A test that passes serially and fails under the
wave belongs in that list, with a comment saying which of the three properties above it
depends on — not behind a retry.**

The trade the serial group makes is explicit: the parallel wave divides by `--jobs`, the
serial group does not. That is why it gets its own runner rather than being appended to
`run-linux` — and why, if it grows further, the lever is another runner rather than a
shorter list. **Shortening the list is not the lever**: every entry is there because its
assertion is about the machine, and a test that only passes when nothing else is running
does not become correct by being run alongside something else.

### Windows

`build-windows-native` compiles the tree with Microsoft's `cl` (Release, `MI_PPROF=ON`) and
bundles it; `run-windows-native` — still named **`ctest (windows-latest)`** — executes that
bundle and checks its coverage. Same toolchain, same runner OS, same hard gate as before
(CLAUDE.md rule 3); the only change is that the compile and the run are separate jobs, and
that this is the first bundle produced from a native Visual Studio multi-config tree. All
win-gnu work, and the cross-built MSVC-ABI comparison arms, stay in `windows-bundles.yml`.

### musl runs inside `alpine:3.20`, and that was measured

A musl-linked executable from this tree requests `/lib/ld-musl-x86_64.so.1` as its program
interpreter, so a glibc host cannot execute it at all — `readelf -l` says so and the loader
agrees. The musl bundles are therefore replayed in a container. `python3` and `libstdc++`
are the complete runtime set; the whole suite was run in a container with nothing else
installed to confirm it. `ci/run_test_bundle.py` is stdlib-only by design, which is what
makes that possible:

```
docker run --rm -v "$PWD":/w -w /w alpine:3.20 sh -c '
  apk add --no-cache python3 libstdc++ &&
  python3 ci/run_test_bundle.py --bundles bundle/musl bundle/musl-pprof-off --jobs 4'
```

The same command reproduces the musl lane locally — it is the one row of the build matrix
`ci/verify_local.py --like-ci` does not cover, because that script has no container runner.

### `isa-baseline (ubuntu-24.04-arm)` stays one job

The x64 ISA scan was split into a build row and a run-stage scan like everything else. The
arm64 one was not, and cannot be: `ci/check_isa_baseline.py` disassembles the library with
the host's `objdump`, which is single-target — an x86_64 runner cannot disassemble aarch64.
There is no run stage to hand that artifact to, so it stays a self-contained build+scan job
on `ubuntu-24.04-arm`.

### Reproducing the whole layout locally

`uv run ci/verify_local.py --like-ci` builds every configuration once (in parallel), then
replays every bundle in one wave, then runs the run-stage gates — coverage comparison,
memory gate + leak control, diagnostic and ISA scans, and both halves of the Bun surface
check. `ci/tests/test_verify_local.py` parses `c-unit.yml`'s matrix `include:` rows as well
as its `run:` blocks, so a cmake flag that moves into the matrix cannot silently escape the
drift guard.

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

- **Nothing compiles this code with Apple's clang any more.** Header compatibility with
  Apple's SDK *is* retained — soldr provisions a real Apple SDK (15.5 as of soldr 0.9.11)
  and both toolchain files build against it, so a header-level incompatibility still
  fails here. What is lost is narrower and worth naming precisely: **Apple-clang codegen**
  (soldr's clang 21 and Apple's fork can differ in what they emit), and **Xcode drift** —
  a change in a newer Xcode's SDK than the one soldr pins surfaces downstream, not here.
  The native compile-compat build that would have caught the second went with the runners.
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
clicked through in dockur's web viewer.

That viewer is an **unauthenticated noVNC console with keyboard and mouse control of the
VM**, so driving it from a runner would mean publishing it to the internet for the hours
an emulated install takes, from a public repository. An earlier draft did exactly that
behind a cloudflared quick tunnel and printed the URL into the run log. It does not exist
any more. Instead:

1. A maintainer runs **`ci/macos_golden_local.sh`** on their own Linux box — `boot` (guest
   on `127.0.0.1:8006` only, with the click-by-click list), `check` (poll for ssh), `pack`
   (shut down, `tar -cSf | zstd -19`, sha256, 9 GB gate).
2. They upload the result somewhere private and run **`macos-golden-upload.yml`** with the
   URL and sha256. Both inputs are `::add-mask::`ed before any step can echo them; the job
   verifies the checksum, re-checks the size, proves the archive is readable, and saves it
   to the cache. Neither the expected nor the actual digest is printed on mismatch — that
   would let the masked value be recovered by comparison.
3. **`macos-golden-touch.yml`** restores it weekly with `lookup-only: true`. Actions caches
   are evicted after **7 days without a read**, and this is the one entry in the repository
   CI cannot rebuild by itself.

A cache miss in `run-macos-x64-dockur` is a hard error naming both, never a silent skip —
so **until that image has been built and uploaded once, `run-macos-x64-dockur` is red**, on
the PR that introduced it and on every push to `main` afterwards. That is deliberate: a
gate that says "I have no disk to run on" is worth more than one that skips itself into a
green tick. It fails in about two minutes.

**Two archive flags are load-bearing, and both were wrong first time.** Packing uses
`tar -cSf`: the guest disk is a raw image that is mostly holes, and without `-S` GNU tar
stores every hole as literal zeros. zstd still compresses those to almost nothing, so the
archive looks fine — but *extraction* then writes them as real blocks, and passing `-S` at
extraction time does not help, because sparseness has to be recorded at creation.
Measured on a 2 GB/21 MB sparse file: 21 MB extracted with `-S` at pack time, **2.1 GB
without**. Scaled to a 64 GB disk that exhausts the runner, which has ~87 GB free in total.
Unpacking uses `zstd -dc`, **not** `zstd -d --sparse … -c`: sparse mode cannot apply to a
pipe and zstd does not ignore the flag, it fails with `zstd: error 92 : Sparse skip error`
(reproduced on zstd 1.5.7) and hands `tar` a truncated stream.

**The cache budget is the open risk.** GitHub gives a repository **10 GB of Actions cache
in total**, shared with the soldr toolchain caches. The golden image is gated at 9 GB, and
an image near that ceiling will LRU-evict those caches continuously — slower runs that
never actually fail. If that happens the fix is a smaller image or a self-hosted runner.

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
2. `mimalloc-test-stress-dynamic.exe` linked `mimalloc.dll` **last** in its import table,
   so the redirection module was initialised after the CRT it patches and
   `mi_is_redirected()` stayed false. Upstream's workaround for that is
   `bin/minject.exe`, run as a CMake `POST_BUILD` when `MI_MINGW_UCRT64` is set; minject
   is a PE binary, so that command is now guarded with `AND CMAKE_HOST_WIN32`.

   The cross lane does not use it. See below.

### Import order is a link-order property, not a CRT property

This was the phase's one surprise and it is worth writing down, because #277 and this
document both said "it's the CRT" before it was measured.

`ld`'s PE linker script emits the import descriptors under `SORT(*)(.idata$2)`, sorted by
the **input file path as spelled on the link line** — archive path first, member name only
as a tie-break *within* one archive. CMake names the DLL's import library relative to the
build directory (`libmimalloc.dll.a`) while every CRT archive arrives as an absolute
sysroot path, and `/` (0x2f) sorts before `l`:

```
libmimalloc.dll.a     -> KERNEL32.dll, api-ms-win-crt-*.dll, ..., mimalloc.dll   (last)
./libmimalloc.dll.a   -> mimalloc.dll, KERNEL32.dll, api-ms-win-crt-*.dll        (first)
```

Those two lines were produced by linking the *same object file* against **byte-identical**
archives; only the spelling of the path differs. Renaming the archive's members changes
nothing, because member names only break ties inside one archive.

That also explains the msvcrt/UCRT difference that looked like a CRT effect: under msvcrt
the CRT arrives as `libmsvcrt.a` and `mimalloc.dll` happens to sort ahead of `msvcrt.dll`;
under UCRT it arrives as `api-ms-win-crt-*`, which sorts earlier, and mimalloc loses. The
CRT only changes what mimalloc is being sorted against.

`CMakeLists.txt` therefore spells the same file `./…` for `mimalloc-test-stress-dynamic`
on MinGW — in the *libraries* position, since an archive named before anything needs it
contributes nothing — using `$<TARGET_LINKER_FILE_NAME:mimalloc>` so the Debug build's
`mimalloc-debug.dll` is handled too. Verified cross-built for all three configs, and
gated on every run.

That fixes the import order in the *executable*. It is necessary but not sufficient: the
same defect one level down, inside `mimalloc.dll`'s own table, is what actually decided
the outcome. See below.

### The override is gated on the bundle's own binaries

`run-windows` (`run-windows-gnu` before phase D) asserts all of this, as hard failures, on the artifacts it ships:

- **the exe's table:** `mimalloc*.dll` is import #0 of `mimalloc-test-stress-dynamic.exe`
  in the release, debug-full and shared bundles. Read with `ci/pe_imports.py`, a PE
  import-table reader — `dumpbin` is MSVC-only and MSYS2 is not installed at that point in
  the job (and depending on it would be wrong anyway: the bundle is meant to run with no
  toolchain).
- **the DLL's table**, in the Linux build job that produces each bundle:
  `mimalloc-redirect.dll` is import #0 of `mimalloc.dll`, and `mimalloc.dll` does **not**
  import `__emutls_get_address`.
- **the behaviour**, on all three bundles: `mimalloc-test-redirect-probe` must print
  `REDIRECT_BEHAVIOURAL=1`.

That last one is deliberately *not* `mi_is_redirected()`. That flag only reports what the
redirection module believed it did, and on the MSYS2 msvcrt binary it reports success
while the binary's own `msvcrt.dll!malloc` was never touched — `mimalloc-redirect.dll`
v1.3.3 names only `ucrtbase`/`ucrtbased` and contains no reference to `msvcrt`; what it
patched there was a `ucrtbase.dll` some system DLL had loaded. So the gate takes a pointer
from the CRT's own `malloc` and asks `mi_is_in_heap_region`. Only a real override makes
that true. The flag is still printed, and the native row is still gated on it, because
while that MSYS2 build exists a regression in it is a regression — but it is a witness,
not a control.

### Why it did not redirect, and what fixed it

Two bugs in series, both now fixed:

1. **`mimalloc-redirect.dll` refuses to patch a CRT that has already initialised.** It
   imports only `ntdll` and has no output API at all: its entire diagnostic channel is the
   `const char**` returned by `mi_allocator_init`, which `src/init.c` prints *after* the
   option dump. Printed, it says `mimalloc-redirect.dll seems to be initialized after
   ucrtbase.dll`. The check (disassembled at `0x180003720` in the v1.3.3 module) is
   `LdrGetDllHandle` followed by `LDR_DATA_TABLE_ENTRY.Flags & 0x00080000`
   (`LDRP_PROCESS_ATTACH_CALLED`). The loader initialises a module's dependencies in *that
   module's* import-descriptor order, so `mimalloc.dll` has to import the redirect module
   before its own `api-ms-win-crt-*`. MSVC gets that for free (explicit libraries precede
   `/DEFAULTLIB` ones); with `ld` it was decided by where the checkout happened to live.
   `MI_MINGW_REDIRECT_FIRST` (default ON) spells it `./…`.

2. **That layout then died at load — GCC emulated TLS.** soldr's conda-forge
   `x86_64-w64-mingw32-gcc 15.3.0` has no native TLS: `__thread int x;` compiles to
   `__emutls_v.x` plus a call to `__emutls_get_address`, and `__declspec(thread)` only
   warns and emits a plain global. `__emutls_get_address` allocates its per-thread table
   with `malloc` — which, once the override is live, is `mi_malloc`. cdb on the runner
   showed the cycle, one `libgcc_s_seh_1!__emutls_get_address` frame per turn, until the
   stack was gone. That is the "rc 139 / rc 127" recorded on earlier runs: a stack
   overflow before `main`, **not** a corrupt PE. Fixed by moving the two thread-locals on
   the allocation path — `src/threadlocal.c`'s `mi_thread_locals`/`mi_slot_fast` and
   `src/options.c`'s output-recursion guard — onto dynamic Win32 TLS keys
   (`_mi_prim_tls_key_*`, `MI_WIN_TLS_SLOTS`), which never allocate.

`minject` is **not** used on the cross lane, and is not needed on any lane. It is a
Windows PE utility, so it cannot run on the Linux builder at all; and when it was run on
the Windows runner instead it produced an image that would not start (exit 127). That is
now understood: minject builds exactly the redirect-first layout, which was correct all
along — the image died of the emulated-TLS recursion above, not of anything wrong with the
PE minject wrote. Superseded by `MI_MINGW_REDIRECT_FIRST`, which does the same thing at
link time and works on a cross build.

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
- resolved `Link libraries : psapi;shell32;user32;advapi32;bcrypt;synchronization`, exactly
- `tests.json` contains no absolute path; `libgcc_s_seh-1.dll` and `mimalloc-redirect.dll`
  are in the bundle

`CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY` is set on this lane too, but unlike Darwin it is
**belt-and-braces rather than load-bearing**: CMake's Windows-GNU
`CMAKE_FIND_LIBRARY_SUFFIXES` are `.dll.a;.a;.lib`, so the host's `librt.so` is not a
candidate for `find_link_library()` even with the search unconfined (verified both ways).
#277's claim that all three lanes need it is true only for Darwin.

### Coverage accounting

`ci/bundle_coverage.py` runs on every `run-windows` (called `run-windows-gnu` before
phase D). The runner installs MSYS2
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
`rust-native.yml`'s `test-win-gnu`, and `cross.yml`'s `build-win-gnu` +
`test (x86_64-pc-windows-gnu)`. Each carries a dated TODO naming what deletes it. Because
this phase changes the CRT, that window is the only place the two runtimes are exercised
side by side — do not delete the msvcrt arm before the comparison has actually happened.

`c-unit.yml`'s three MSYS2 rows (`ctest-win-gnu`, `ctest-debug-full-win-gnu`,
`ctest-shared-win-gnu`) were that workflow's share of this control arm. They were **deleted
by #307**, after the window their TODOs named had elapsed: added 2026-09-02, deleted
2026-09-03 with 19 `c-unit` runs on `main` in between and `windows-bundles.yml`'s
`run-windows` green throughout. They are not coming back — win-gnu is gated from
`windows-bundles.yml`, per CLAUDE.md rule 3.

## windows-msvc via cross-built bundles, and the one native `cl` gate that stays

Issue #277 phase D. The same `windows-bundles.yml` that carries the MinGW lane now also
builds the **MSVC-ABI** test binaries on **ubuntu-latest**, through soldr's `blessed-msvc`
toolchain — clang-cl + lld-link + llvm-lib against an xwin-materialised Microsoft CRT and
Windows SDK — and the single `windows-latest` job (`run-windows-gnu` is now
**`run-windows`**) runs them alongside the win-gnu ones.

| Linux job | produces | replaces |
|---|---|---|
| `build-windows-msvc (windows-msvc-x64-release)` | `bundle-windows-msvc-x64-release` | *(comparison arm for the retained native `ctest`)* |
| `build-windows-msvc (windows-msvc-x64-debug-full)` | `bundle-windows-msvc-x64-debug-full` | `ctest-debug-full (windows-latest)` |
| `build-windows-msvc (windows-msvc-x64-shared)` | `bundle-windows-msvc-x64-shared` | `ctest-shared (windows-latest)` |
| `build-windows-msvc (windows-msvc-x64-leak)` | `bundle-windows-msvc-x64-leak` | the positive control half of `memory-gate (windows-latest)` |
| `build-rust (x86_64-pc-windows-msvc)` | `rust-test-bins-…`, `rust-sentinel-…` | `cross.yml` `test (x86_64-pc-windows-msvc)`, `rust-native.yml` `test (windows-latest)`, `benchmark-sentinel.yml` `benchmark-sentinel (windows-latest)` |

### One native `cl` job is retained, deliberately

CLAUDE.md rule 3 makes MSVC a priority platform. **A clang-cl binary is not a `cl`
binary**: clang-cl accepts `__attribute__`s that `cl` rejects, and `cl`'s codegen, TLS
lowering and DLL runtime are its own. So `c-unit.yml`'s **`ctest (windows-latest)`
(Release) stays native and stays a hard gate** — it is not `continue-on-error`, and it is
not scheduled for deletion. #307 split it into `build-windows-native` (the `cl` compile)
and `run-windows-native` (the run, still reporting under the name `ctest
(windows-latest)`); both halves are hard gates and neither is `continue-on-error`.

Every *other* `windows-latest` row in the repository was informational with a dated TODO.
`c-unit.yml`'s share of those — the `windows-latest` rows of `ctest-debug-full`,
`ctest-shared` and `memory-gate` — was **deleted by #307** once that window had elapsed
(see the phase C rollout note above for the run counts). Windows runner jobs per push from
`c-unit.yml`: **7 → 2**, and those two are the retained `cl` gate's build and run halves.

`run-windows` additionally configures all three MSVC configs with `cl` and builds the
Release tree. That is not a duplicate gate; it is what gives the coverage comparison a real
native `ctest` name set, and what lets the job print the native and cross `mimalloc.dll`
import tables and redirect probes side by side.

### `/MD` only: today's `/MDd` debug-full config cannot be reproduced

soldr's CRT splat carries the **release** import libraries only — `msvcrt.lib`,
`libcmt.lib`, `ucrt.lib`, `libucrt.lib` — and no `msvcrtd.lib`, `libcmtd.lib` or
`ucrtd.lib`. `cmake/toolchains/soldr-x86_64-pc-windows-msvc.cmake` therefore pins
`CMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL`. Measured, with that one line removed:

```
lld-link: error: could not open 'msvcrtd.lib': No such file or directory
```

`/MD` is also the only runtime the `mimalloc-redirect` override can work against at all: a
statically-linked CRT has no `ucrtbase.dll` for the module to patch.

**What that costs, precisely.** The `windows-msvc-x64-debug-full` bundle is configured
`-DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON -DMI_DEBUG_FULL=ON`, *not* `Debug`. That is not
only the CRT constraint — it is also what reproduces the native job. `ctest-debug-full
(windows-latest)` runs `cmake -B build -DMI_PPROF=ON -DMI_DEBUG_FULL=ON` with **no `-G`**,
which selects the Visual Studio generator; `CMAKE_BUILD_TYPE` is empty at configure time,
so `CMakeLists.txt` (~142-150) defaults it from the binary directory's name — `build` →
Release. The native job's *resolved* configuration is therefore:

```
MI_DEBUG=3  MI_GUARDED=1  MI_FREE_IS_CHECKED=1  MI_BUILD_RELEASE   library: mimalloc
```

which is byte-for-byte what this bundle configures. Passing `-DCMAKE_BUILD_TYPE=Debug`
would have been *less* faithful: it drops `MI_BUILD_RELEASE` and renames the library
`mimalloc-debug`. The genuine loss is the **compile flavour** that `--config Debug` selects
on the multi-config generator: native compiles `/MDd /Od /RTC1`, the bundle `/MD /O2`. The
assertions and expensive invariant checks — what `MI_DEBUG_FULL` exists for — are
identical, and the test-name sets are compared on every run.

(#277 phase C correction 4 said phase B's "debug-full is really Release + `MI_DEBUG=3`"
finding was macOS-only. It is not: it holds for windows-MSVC too, for the different reason
above. windows-GNU remains the one lane that really does configure `Debug`.)

### Import order: the redirect module must precede the CRT, not be index 0

`mimalloc-redirect.dll` reads `ucrtbase.dll`'s `LDR_DATA_TABLE_ENTRY.Flags` and refuses to
patch a runtime whose `DllMain` has already run (`LDRP_PROCESS_ATTACH_CALLED`, `0x00080000`
— disassembled at `0x180003720` in v1.3.3). The loader initialises a module's dependencies
in *that module's* import-descriptor order, so the requirement is an ordering **inside
`mimalloc.dll`**.

#277 phase D asked for the redirect module at **import #0**. It is not, on either arm.
Measured, identically for cross clang-cl and native `cl`:

```
ADVAPI32.dll  mimalloc-redirect.dll  KERNEL32.dll  MSVCP140.dll  VCRUNTIME140.dll
api-ms-win-crt-stdio-…  api-ms-win-crt-runtime-…  …
```

`ADVAPI32.dll` is #0 because `${mi_libraries}` is attached to the target before the
redirect import library is, and lld-link emits descriptors in link order. It is harmless:
ADVAPI32 is a KnownDLL whose own subtree uses legacy `msvcrt.dll`, not `ucrtbase`, so
initialising it first does not set the flag the module checks. **The gate is therefore
"redirect before every CRT module", not a fixed index** — asserting index 0 would fail a
layout that works. MinGW needed the `MI_MINGW_REDIRECT_FIRST` trick because `ld` sorted the
redirect module *behind* `api-ms-win-crt-*`; lld-link needs no equivalent.

### No emulated TLS, asserted

The windows-gnu lane's second bug was GCC emulated TLS: `__thread` became
`__emutls_get_address`, which allocates with `malloc`, which is `mi_malloc` once the
override is live — unbounded recursion, dead before `main`, with no diagnostic. clang-cl
emits real `__declspec(thread)`, so this cannot happen; the build job **asserts** it
(`__emutls` must not appear in the linked DLL) rather than assuming it, because the failure
mode is silent.

### The override is gated behaviourally, on the cross-built binary

Same standard as phase C, and the same reason: `mi_is_redirected()` reports only what the
redirection module *believed* it did, and that flag has been observed true on a binary
whose own allocations were never touched. `mimalloc-test-redirect-probe` takes a pointer
from the CRT's own `malloc` and asks `mi_is_in_heap_region`. All three shared-library MSVC
bundles must print `REDIRECT_BEHAVIOURAL=1`; it is a **hard failure**. The native `cl`
Release build runs the same probe on the same commit and the two are printed together.

### The runtime DLLs are the runner's, and that is checked

Unlike the mingw lane there is nothing to ship: soldr's xwin splat is import libraries
only. But `mimalloc.dll` imports **`VCRUNTIME140.dll` and `MSVCP140.dll`** — the latter
because `CMakeLists.txt` (~205-212) detects clang-cl as MSVC-like and turns `MI_USE_CXX` on
by itself, exactly as it does for `cl`. Those are the **Visual C++ redistributable, not
part of Windows**; `windows-latest` has them because Visual Studio is installed.

So the assumption is declared and then checked, in two places:

- `bundle_tests.py --check-dll-closure --allow-msvc-runtime` runs the transitive PE import
  scan with nothing to copy from, so a bundle that imports anything *else* unresolvable is
  still refused at build time. `--allow-msvc-runtime` is opt-in and lane-scoped: the
  win-gnu lane cannot silently acquire a VC++ dependency it could not satisfy.
- `run-windows` runs `where VCRUNTIME140.dll` / `where MSVCP140.dll` before anything else,
  so a runner image that dropped them fails with a sentence instead of a dialog-free
  `0xC0000135`.

### `llvm-rc` is absent, so manifests are off

CMake's Windows-MSVC module wraps every link in `cmake -E vs_link_exe --rc=… --mt=…
--manifests`, which compiles and embeds a default side-by-side manifest. soldr's LLVM ships
**neither `llvm-rc` nor `llvm-mt`**. Without an opt-out the lane's outcome depends on
whether the *host* happens to have some other `llvm-rc` ahead on `PATH`: with one, CMake
bakes that absolute host path into the cache; without one, `CMAKE_RC_COMPILER` falls back to
`rc` and configure dies inside `CMakeTestCCompiler` with `RC Pass 1: command "rc /fo
…/manifest.rc" failed`. The toolchain file passes `/MANIFEST:NO`, which `cmVSLink` parses
and which skips the rc/mt pass entirely. The tree registers no `.rc` source and none of its
test executables needs a manifest; hardcoding the `llvm-rc` that lives outside soldr's
exported `PATH` would have broken the "consume only soldr's env" rule.

### What the build job proves before shipping a bundle

- `file format coff-x86-64`
- the `/MD` pin **on the artifact**: `prim.c.obj`'s `.drectve` requests `msvcrt.lib` and no
  `*d.lib` — a flag that never reaches the compiler is this repository's most-repeated CI
  bug
- clang-cl was detected as MSVC-like, via `/Zc:__cplusplus` in the resolved compiler flags
  (the toolchain deliberately does not pass `MI_USE_CXX`; if the detection stopped firing
  the library would quietly switch from C++ atomics to C11 ones with no other symptom)
- `.CRT$XLB`, `.CRT$XLY`, `.CRT$XIB` in `prim.c.obj`, **and** a non-empty Thread Storage
  Directory in the linked DLL
- no `__emutls` reference anywhere in the DLL
- `mimalloc-redirect.dll` imported, and ahead of every `api-ms-win-crt-*` / `ucrtbase` /
  `MSVCP140` / `VCRUNTIME140` descriptor
- resolved `Link libraries : psapi;shell32;user32;advapi32;bcrypt;synchronization`, exactly
- `tests.json` contains no absolute path

`CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY` is set here as well, and as on windows-gnu it is
belt-and-braces rather than load-bearing: CMake's Windows-MSVC
`CMAKE_FIND_LIBRARY_SUFFIXES` is `.lib;.a`, so a host `librt.so` is not a candidate.

### Coverage accounting

`ci/bundle_coverage.py` compares all six bundles against native `ctest --show-only`
enumerations produced on the runner — three from MSYS2 gcc, three from `cl` — and fails if
a name the native job would run is missing.

The Rust side is where the accounting needed care. `cross.yml`'s
`test (x86_64-pc-windows-msvc)` runs the 19 `mimalloc-pprof` test binaries;
`rust-native.yml`'s `test (windows-latest)` runs `cargo test` across the **whole
workspace**, which is 110 further tests in `bench-harness`, `benchmark-suite`, `dashboard`
and `stress-harness`. They also differ in **profile**, and collapsing that would quietly change what is tested.
So `build-rust (x86_64-pc-windows-msvc)` runs cargo **twice**, staging into
`dist/msvc-tests/{debug,release}/`:

| build | stands in for | staged / built |
|---|---|---:|
| `cargo test --workspace --no-run` (**debug**) | `rust-native.yml` `test (windows-latest)` | 49 / 53 |
| `cargo test -p mimalloc-pprof --no-run --release` | `cross.yml` `test (x86_64-pc-windows-msvc)` | 19 / 19 |

**68 test binaries execute on the runner.** Building only the narrow set would have cut
Windows Rust coverage by more than half while the table claimed parity; building the
*workspace* in release instead — which this job did first — is not a free "stricter" choice
either. `stress-harness`'s `timing_contract` tests assert
`outer_started.elapsed().as_millis() >= 100` around a 100 ms test-hook sleep, and with the
surrounding work optimised away that lands on 99. Measured, and **not** a Windows or
clang-cl property: the same test binary built natively for **Linux** fails 3/3 in release
at `--test-threads=1` and passes 3/3 in debug, on unmodified `main`. `cargo test` is a
debug build, which is why no native job has ever run it. Reported on #277 rather than
fixed here — "changing what any test asserts" is out of scope for that issue, and a test
fix is a `rust/` commit (rule 2).

The two profiles stage into separate directories rather than one flat one because
`t3_stats` and `t12_proto` exist in both, and the pprof dump checks stand in for a
*release* row — a flat directory would leave `ls … | head -1` picking a profile by hash
ordering.

**Four cannot be executed anywhere but the machine that built them, and this is
structural.** `env!("CARGO_BIN_EXE_<name>")` is expanded by cargo *at compile time* into
the absolute path of a companion binary in the builder's target directory. Four tests spawn
`stress-child` that way — `bench-harness`'s `planted_control`, `rejections` and
`throughput`, and `dashboard`'s `stress_child` — so their images carry a literal
`/home/runner/work/…/target/x86_64-pc-windows-msvc/release/stress-child.exe`. There is no
environment override (the string is frozen into the binary), and a Linux path cannot be
reconstructed on a Windows runner, so shipping `stress-child.exe` beside them would not
help either. They fail with `expect("valid normal benchmark")` — measured, on the first
green-everywhere-else run of this lane.

They are excluded by **detecting the property**, not by a hardcoded list of names that
would rot the moment a test is added or renamed: the build job greps each staged image for
the builder's own target path, which is the same "a portable artifact contains no
build-tree absolute path" rule `ci/bundle_tests.py` enforces on the C bundles. Two guards
around it:

- the scanner **self-tests on a known-positive first** (a control file containing that path
  *and a NUL byte*, so it is genuinely binary). `grep -F` without `-a` reports no match on
  binary input, which would silently stage an unrelocatable binary and surface much later
  as a panic that says nothing about paths — the scan's negatives are only worth having if
  its positives are proven;
- a `mimalloc-pprof` test binary landing in the excluded set is a **hard error**. That
  crate is the reason the lane exists, and losing it silently is exactly the failure this
  phase is meant to make impossible.

Those four tests still run on `ubuntu-latest`, where builder and runner are the same
machine. Restoring them on Windows needs an upstream change to the tests (resolve
`stress-child` relative to `current_exe()` instead of `CARGO_BIN_EXE_*`), which is a Rust
change and out of scope for a CI phase (rule 2, rule 7).

Stated rather than gated:

- **Doctests.** `cargo test` includes the 8 doctests in `rust/mimalloc-pprof/src/lib.rs`;
  cross-built `--tests` binaries cannot. Linux-only since phase B, and now on Windows too.
- **`xtask check` / `cargo publish --dry-run` / `check_crate_package.py`** are platform-
  independent (they compare the vendored amalgamation against `src/` and inspect a
  `.crate` archive). Per #277's review addendum item 5 they are now skipped on the Windows
  row rather than carried into a bundle; the ubuntu row still runs them.
- **The PR benchmark sentinel** moves into `run-windows` as a cross-built `dashboard.exe`,
  guarded on `github.event_name == 'pull_request'` so folding it into a push-triggered
  workflow does not turn a PR-only benchmark into a per-push one. It is
  `continue-on-error`: #171/#170 already label hosted-runner numbers informational and no
  gate reads them, which is also what makes it acceptable to measure on a VM that has just
  run four ctest suites.

### Memory-gate baseline

The MSVC bundle lane asks for
`ci/memory-baselines/windows-x64-soldr-clang-cl-21-pprof1.json` via
`--arch x64 --compiler soldr-clang-cl-21`. It must **not** borrow `windows-pprof1.json`,
which native `cl` recorded on the same runner — that would be a cross-toolchain comparison
dressed as a regression check. That file does not exist yet, so the first runs take the
"no baseline → bootstrap it" path (`memory_gate.py where` exits 3) and upload their JSON;
the positive control is skipped with a warning until the baseline is committed, because
`control` requires `check` to *fail* and a missing baseline is not a failure. Same
follow-up as phase B's correction 8 and phase C's correction 7.

### Rollout

Informational (`continue-on-error`) for at least ten pushes, each with a dated TODO naming
what deletes it: `c-unit.yml`'s `ctest-debug-full`, `ctest-shared` and `memory-gate`
**windows rows only** (deleted by #307 on 2026-09-03, window elapsed),
`rust-native.yml`'s `test (windows-latest)`, `cross.yml`'s
`test (x86_64-pc-windows-msvc)`, and `benchmark-sentinel.yml`'s
`benchmark-sentinel (windows-latest)`. When `cross.yml`'s msvc row goes, `build-cross`'s
`x86_64-pc-windows-msvc` row goes with it — but **not** `aarch64-pc-windows-msvc`, which is
build-only and has no runner.

`c-unit.yml`'s `ctest (windows-latest)` is **not** on that list and must not be added to it
without the owner amending CLAUDE.md rule 3.

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

- **Peak memory is not a low-variance signal — but a workload can be made one.** See
  [the memory gate](#the-memory-gate-what-it-measures-298) below: the first version of
  this bullet said the spread was 6–12% and that a minimum-of-N would absorb it. Measured
  over twelve `main` runs it was 16–44%, no statistic absorbed it, and the fix had to be
  in the workload rather than in the threshold.
- **The gate scripts are gating code.** Several of the silent failures above were Python
  or YAML bugs rather than C bugs — the arm64 instruction scanner matched nothing at all
  and reported "clean". Hence `pyright --strict` over `ci/`, with the result schema
  declared rather than indexed by hope.
- **Match report headers, not prose.** The fuzz control's `grep -qiE "AddressSanitizer"`
  was satisfied by libFuzzer's advice *to use* AddressSanitizer. Assertions about tool
  output should anchor on that tool's actual report format (`^(==[0-9]+==)?(ERROR|SUMMARY):`),
  never on a keyword that can appear in an explanatory sentence.

## The memory gate: what it measures (#298)

`memory-gate` builds `test/test-memory-gate.c` with `MI_PPROF=ON` in Release, runs it
eight times, and compares the **minimum** peak of those runs against a committed
per-platform/arch/compiler baseline in `ci/memory-baselines/`. On Windows the gated
number is peak *commit* (the working set is trimmed under pressure and would hide
committed-but-untouched growth); everywhere else it is peak RSS, from `ru_maxrss` via
`mi_process_info`. A second build with `-DMI_BENCH_INJECT_LEAK=600000` runs the identical
comparison through `memory_gate.py control`, which passes only if the gate *fails*.

**What went wrong.** The gate was red on `main` for commits that changed no allocator
code (docs-only #275, ci-only #278/#297), and its own output warned that the observed
spread exceeded the tolerance it was being compared under. The `memory-gate-ubuntu-latest`
artifacts of the last twelve `main` runs of `c-unit.yml`, against a 22.3 MB baseline with
25.6 MB allowed:

| run | min-of-8 | median | within-run spread |
|---|---|---|---|
| 33618717188 | 26.7 | 29.6 | 32.6% |
| 33629562858 | 24.7 | 27.4 | 19.0% |
| 33631613324 | 28.0 | 29.2 | 15.7% |
| 33635722154 | 26.0 | 28.0 | 19.6% |
| 33636560081 | 22.3 | 26.9 | 30.0% |
| 33646494496 | 24.3 | 28.2 | 44.4% |
| 33647332787 | 24.5 | 26.5 | 17.6% |
| 33654892179 | 27.9 | 31.2 | 24.4% |
| 33660325957 | 26.5 | 27.6 | 18.5% |
| 33689409217 | 22.7 | 25.6 | 25.6% |
| 33692432919 | 25.5 | 27.9 | 28.2% |
| 33699070304 | 27.8 | 30.2 | 19.8% |

min-of-8 spans 25.6% across runs and the median 21.9%, so **no statistic was going to
rescue this** — the signal was not there to extract. The `profiler_arena_mb`,
`purged_gb` and liveness counters in those same artifacts are constant across every run,
which rules out the profiler's arena and the P7a/P7b purge machinery: the variance is
entirely in peak RSS. A per-scenario decomposition (idle host, `taskset -c 0-3`) located
it: scenario 1, the 8-thread churn, alone ranged 14.1 → 24.6 MB run to run, and every
later scenario added ≤ 2 MB on top of it. Peak RSS is a high-water mark, so what the gate
was actually measuring was **how many of the churn workers the runner's scheduler
happened to overlap** — a property of the runner, not of the allocator.

**The fix is in the workload.** `test/test-memory-gate.c` now rendezvouses the churn
workers on a portable condition-variable barrier: every worker in a round allocates its
live set, waits for all of them, then churns and waits again before freeing. "All N live
at once" becomes the only reachable state, so the peak is the structural cost of N live
sets. Scenario 3's `while (ready == 0) {}` handoff became the same barrier, so six
consumers no longer burn the vCPUs the producers need. Wall time roughly halved and
stopped scattering: 0.26 / 0.47 / 0.52 s before, 0.27 / 0.18 / 0.18 s after (same
library, three runs each, `taskset -c 0-3` on an otherwise idle host).

**The numbers.** Three independent `ubuntu-latest` runner VMs (`nproc=4`,
THP=`always`), 16 runs each, identical object code:

| lane | min | median | max | spread |
|---|---|---|---|---|
| new workload, run A | 58.0 | 58.3 | 58.5 | **0.9%** |
| new workload, run B | 58.0 | 58.5 | 58.5 | **0.9%** |
| new workload, run C | 58.0 | 58.3 | 58.6 | **1.0%** |
| old workload, same three VMs | 26.4 / 27.4 / 25.5 | — | — | 26.1% / 24.5% / 23.1% |

min-of-16 is 58.0 MB on all three VMs — 0.0% across-run variation of the estimator.
Variants that changed nothing: `taskset -c 0-1` (0.5%), `taskset -c 0` (0.7%),
`MIMALLOC_SCAVENGER=0 MIMALLOC_PURGE_HOLES=0` (1.2%). An unpinned 16-core workstation
reads 58.2–58.3 MB — the same number as the 4-vCPU runner, where the *old* workload read
58 MB unpinned against 23 MB pinned. The gate is now host-independent, which is why
`memory_gate.py`'s local CPU pinning is no longer load-bearing (it is kept as a guard
against a future scenario reintroducing the sensitivity).

**Tolerance.** `PEAK_TOLERANCE = 0.05`, down from 0.15. That is 5× the largest observed
spread and 2.9 MB of absolute headroom on the 58.0 MB baseline. The measurement would
support 0.03; the difference is reserved for runner-image drift, which three runs on one
image cannot bound — tighten it once ten `main` runs on a new image have been observed,
with those numbers written into `ci/memory_gate.py`. `RUNS_EXPECTED` stays 8: min-of-8 and
min-of-16 of this distribution are the same number, so more runs buy nothing and fewer
would need a matching edit to `c-unit.yml`'s run loops.

**Control margin.** At the `MI_BENCH_INJECT_LEAK=200000` this control was built with when
#298 landed, it read min-of-8 82.6 / 82.4 / 82.5 MB on those same three VMs — **+42.1%**,
or 8.4× the tolerance, against a rule that the control must fire by at least 2×. That is
comfortably over the rule but well below where this control used to sit: the injected leak
is the same ~32 MB, while the structural peak it has to clear went from ~22 MB to 58 MB,
so the *ratio* fell from ~3× the baseline to ~1.4×. #307 therefore raised the knob (it is
set in `c-unit.yml`'s `memory-gate-leak` build row, not here) to **600000**, measured
locally at min-of-4 166.1 MB and confirmed by CI: run 33707744137's control reports
`peak_rss regressed: 156.5 MB vs baseline 58.0 MB (+169.8%, allowed +5%)` — **+169.8%, or
34× the tolerance**. `ci/tests/test_memory_gate.py` asserts the 2× rule against the
conservative 200000 figure,
so raising the tolerance past half the measured control margin is a failing test rather
than a judgement call. Leak injection now `memset`s the whole block: RSS counts *resident*
pages, and a leak whose pages were committed-but-untouched moved the peak by an amount
that depended on purge timing — one measured batch had a control run land within 1% of
the clean baseline, i.e. the control came within one sample of silently not firing.

**Baselines.** `linux-pprof1.json` re-recorded at 58.0 MB (min-of-16, 0.9% spread) from
run 33700518749; independently confirmed on two further runner VMs by runs 33701098707
(min-of-8 58.2 MB, 0.7% spread) and 33701827771 (58.1 MB, 0.7%), for five independent
`ubuntu-latest` VMs in total, all reading 58.0-58.2 MB.

`windows-pprof1.json` re-recorded at **74.4 MB (min-of-8, 0.1% spread)** from run
33701098707, and reproduced exactly by run 33701827771 (min-of-8 74.4 MB, 0.1% spread) on
a second Windows runner — two runs, not one. Re-recorded because a workload change invalidates every baseline, not just the flaky one.
Windows was never the *failing* lane but it was the noisy one: across the same twelve
`main` runs it read min-of-8 51.2–57.0 MB (11.3% across runs) with within-run spreads of
3.9–17.5%, and its committed baseline had itself been recorded at 9.9% spread. The
rendezvous fixed it too, and by more than it fixed ubuntu — 17.5% → 0.1%, which is the
clearest evidence available that the scheduler-overlap diagnosis was the right one: the
same source code change collapses the spread on two unrelated kernels and two different
gated metrics (peak RSS and peak commit).

`macos-pprof1.json` is **left
untouched and is stale**: it records a native-Xcode arm64 lane that #277 phase B2 deleted,
and no lane compares against it any more (the dockur x86_64 lane keys on
`macos-x86_64-soldr-clang-21-pprof1.json` and bootstraps its own). Delete or regenerate it
if a macOS lane ever compares against `macos-pprof1.json` again.

`ci/tests/test_memory_gate.py` additionally asserts that **every** committed baseline's
own recorded `baseline_spread_pct` is below the tolerance it will be compared under —
baselining from a measurement noisier than the threshold is the #298 failure mode in
miniature.


## zero-tracking on Windows: a gated step, not a VM (#277 phase E)

`zero-tracking.yml` used to be a two-row matrix, `ubuntu-latest` and `windows-latest`. The
Windows row checked out, configured and built the tree with MSVC, ran
`ctest -R zero-tracking`, then ran the interleaved A/B — a whole extra Windows VM per PR
that touched `src/arena.c` or `test/bench-zero-tracking.c`. That is precisely the cost
#277 exists to remove, so both halves moved into `windows-bundles.yml`'s single
`run-windows` job and the matrix row is gone.

The two halves did not move the same way, and the difference matters:

| half | before | after |
|---|---|---|
| correctness (`ctest -R zero-tracking`) | Windows row, only on a matching PR | **unconditional, every push** — `test-zero-tracking` and `test-zero-tracking-enabled` are registered ctest tests, so `ci/bundle_tests.py` puts them in every bundle manifest and `run-windows` replays the whole manifest for `windows-gnu-x64-release` **and** `windows-msvc-x64-release` |
| interleaved A/B timing | Windows row, only on a matching PR | a gated step on `run-windows`, keyed to the same paths |

So the correctness gate got *stronger* (two lanes, every push, instead of one lane on a
path-filtered PR) while the Windows job count went **down by one**.

**The quiet-runner property survives.** `zero-tracking.yml`'s header explains why the A/B
is its own workflow: a first local attempt measured a 13% regression that was really
measurement order on a loaded machine. `run-windows` is entirely serial, so nothing else
executes on that VM while the A/B runs, and the mitigation that actually did the work —
interleaved, paired arms from a single binary, raw numbers rather than a verdict — is
unchanged.

**Two costs, stated:**

- The Windows arm is now built by soldr's **clang-cl**, not by `cl`. It is the
  `windows-msvc-x64-release` bundle's `mimalloc-test-zero-tracking.exe`.
- The gate step asks the API for the PR's changed files rather than using a workflow
  `paths:` filter (a step cannot have one). It keys on `src/arena.c`,
  `test/bench-zero-tracking.c` and `.github/workflows/windows-bundles.yml` — deliberately
  **not** on `zero-tracking.yml`, because editing that file does not trigger
  `windows-bundles.yml` at all, so keying on it would promise a run the trigger cannot
  deliver. A PR touching only `zero-tracking.yml` therefore exercises the Linux arm alone.
  The API call **fails open**: if it errors, the A/B runs.

## Release assets are cross-built, all of them (#277 phase E)

`auto-release.yml` ships the shared library, the static library, the object file, the
CMake package and pkg-config files, and the headers — including this fork's own
`mimalloc/profile.h` and `mimalloc/memory-events.h` — for four targets, plus the
architecture-independent vendored C amalgamation ZIP it always shipped:

| asset | target | built by | consumer still needs |
|---|---|---|---|
| `mimalloc-pprof-macos-arm64-<tag>.tar.gz` | `aarch64-apple-darwin` | soldr clang 21 + ld64.lld, soldr's Apple SDK, on `ubuntu-latest` | — |
| `mimalloc-pprof-macos-x86_64-<tag>.tar.gz` | `x86_64-apple-darwin` | same | — |
| `mimalloc-pprof-windows-x64-gnu-<tag>.zip` | `x86_64-pc-windows-gnu` | soldr mingw-w64 gcc 15 (UCRT), on `ubuntu-latest` | nothing — `libgcc_s_seh-1.dll` is **shipped in the archive** |
| `mimalloc-pprof-windows-x64-msvc-<tag>.zip` | `x86_64-pc-windows-msvc` | soldr clang-cl + lld-link, `/MD` pinned, on `ubuntu-latest` | the Visual C++ redistributable (`VCRUNTIME140.dll`, `MSVCP140.dll`) |
| `mimalloc-pprof-c-<tag>.zip` | any | `ubuntu-latest` (source, not binaries) | a C compiler |

### The decision, and the row it overrides

#277's phase table said "shipped release assets stay built by the platform's own
toolchain (state the decision)". **They are not.** Every binary asset is cross-built on
Linux. Two owner decisions taken after that row was written force it:

1. **No macOS build or test may run on a native Mac runner**, anywhere in this repository
   (`ci/lint_no_macos_runners.py` fails the `python-lint` gate if a macOS label reappears).
   There is no Apple toolchain available to build an Apple asset with.
2. **Cross-compilation is a product requirement**, not a CI cost measure: mimalloc-pprof
   ships inside cross-compiled code, so the Linux-cross-built artifact *is* the product.

The second is the one that settles Windows, where a native runner does still exist.
Building the shipped MSVC-ABI binary with `cl` on `windows-latest` while
`windows-bundles.yml` executes the test suite from a clang-cl bundle would mean the thing
tested and the thing shipped are different binaries. So the assets come off the same
toolchains, the same `cmake/toolchains/soldr-<triple>.cmake` files and the same commit as
the bundles that are actually executed — the bundles are the evidence for the assets.

`c-unit.yml` keeps one native `cl` Release `ctest` job as the correctness gate for `cl`
(CLAUDE.md rule 3). **That job ships nothing, and never did**: before this phase
`auto-release.yml` had no binary assets at all, only the amalgamation ZIP, so there is no
natively-built asset here to "retain".

### What the release job proves before attaching anything

Each lane, on the artifact rather than on the flags that produced it:

- the resolved `-- Link libraries` list is exactly `pthread` (Darwin) or
  `psapi;shell32;user32;advapi32;bcrypt` (Windows) — on Darwin this is load-bearing, since
  without the toolchain file's find-root confinement `find_link_library()` hands the host's
  ELF `librt.so` to a Mach-O link;
- the Mach-O header says `ARM64` / `X86_64`, or the PE header says `pei-x86-64` /
  `coff-x86-64`;
- `mimalloc-redirect.dll` is installed on both Windows lanes;
- the **whole PE import closure resolves**, via the same `ci/bundle_tests.py`
  `resolve_runtime_dlls()` the test bundles use. This is what puts `libgcc_s_seh-1.dll` in
  the win-gnu archive: `mimalloc.dll` imports it for `__popcountdi2`, and a missing DLL on
  the consumer's machine is a dialog-free `0xC0000135`, not a legible error. Shipping it
  rather than relinking `-static-libgcc` keeps the shipped binary byte-identical to the one
  `windows-bundles.yml` tests.

Every archive carries a `PROVENANCE.txt` naming the commit, the target, the compiler and
the runtime the consumer still needs, because a user who downloads `windows-x64-msvc`
should learn about the VC++ redistributable from the archive rather than from a loader
error.

`release` `needs:` both build jobs, `release-outcome` reports both, and the rename step
hard-fails if fewer than five assets are present — a release that published to crates.io
and then shipped three of its four platform archives would be a release nobody can tell is
incomplete (issue #55 is that failure once already). `auto-release.yml` never runs on a
pull request, so none of this wiring is exercised by CI before a tag:
`ci/tests/test_auto_release_workflow.py` is the substitute, asserting the shape from the
YAML itself.

## Reproducing a macOS or Windows bundle from Linux (#277 phase F)

```bash
uv run ci/verify_local.py --list                            # config table + bundle table
uv run ci/verify_local.py --bundle macos-arm64-release      # build one lane locally
```

`--bundle <name>` runs the same `soldr prepare --target <triple>` (from `rust/`, where the
`rust-toolchain.toml` pin lives), the same `cmake/toolchains/soldr-<triple>.cmake`, the same
matrix `cmake` flags, and the same `ci/bundle_tests.py` invocation — including the
lane-specific `--objdump` / `--dll-search-dir` / `--check-dll-closure` /
`--allow-msvc-runtime` arguments — that `macos-bundles.yml` and `windows-bundles.yml` use.
It asserts the resolved `-- Link libraries` line the build jobs assert, prints the bundle
path, the manifest's test count and every test's lowered argv, and ends with the exact
`ci/run_test_bundle.py` command to replay it on the target OS.

It stops **before** execution, which is the one step a Linux box cannot do. That is still
most of the failure surface: a configure that picked up a host library, a link that lost
`__interpose` or a TLS directory, an emulated-TLS import, a bundle missing a runtime DLL,
a manifest with a leaked absolute path — all of those are build-side and all of them
reproduce here in about a minute, instead of a push-and-wait cycle against a runner.

The fourteen names are exactly the two workflows' matrices
(`macos-{arm64,x64}-{release,debug-full,leak}`,
`windows-{gnu,msvc}-x64-{release,debug-full,shared,leak}`), and
`ci/tests/test_verify_local.py` parses those matrices and fails if a name, triple, cmake
flag or `bundle_tests.py` argument drifts out of sync — the same contract the
`--only` config table has had since phase A.
