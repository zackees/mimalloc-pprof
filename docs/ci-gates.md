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
| **ctest matrix** | correctness on ubuntu / windows-MSVC / windows-MinGW / macos, `MI_PPROF` on and off, `MI_DEBUG_FULL`, and shared-library builds on all three of ubuntu, MSVC and MinGW. **macOS uses no Apple hardware** (#277 phase B2): both arches are cross-built on Linux, x86_64 is executed under `dockurr/macos` on a Linux runner, arm64 is compile-only. **windows-MinGW is gated from Linux-built bundles run on one Windows runner** (`windows-bundles.yml`, #277 phase C — which also moves that lane from msvcrt to UCRT); the native MSYS2 MinGW jobs are informational during the comparison window | `ci/bundle_coverage.py` fails if a test in the arm64 bundle is missing from the executed x86_64 one, or if any test the native MinGW `ctest` would run is missing from a bundle; `ci/lint_no_macos_runners.py` fails if any workflow names a macOS runner |
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
- resolved `Link libraries : psapi;shell32;user32;advapi32;bcrypt`, exactly
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
`c-unit.yml`'s `ctest-win-gnu`, `ctest-debug-full-win-gnu` and `ctest-shared-win-gnu`,
`rust-native.yml`'s `test-win-gnu`, and `cross.yml`'s `build-win-gnu` +
`test (x86_64-pc-windows-gnu)`. Each carries a dated TODO naming what deletes it. Because
this phase changes the CRT, that window is the only place the two runtimes are exercised
side by side — do not delete the msvcrt arm before the comparison has actually happened.

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
not scheduled for deletion. Every *other* `windows-latest` row in the repository is now
informational with a dated TODO. Windows runner jobs per push: **13 → 2**.

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
- resolved `Link libraries : psapi;shell32;user32;advapi32;bcrypt`, exactly
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
**windows rows only**, `rust-native.yml`'s `test (windows-latest)`, `cross.yml`'s
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
