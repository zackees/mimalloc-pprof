# Native local verification (Linux)

`ci/verify_local.py` is a fast, parallel local mirror of the Linux-runnable subset of
CI: `.github/workflows/c-unit.yml`, `rust-native.yml`, `python-lint.yml`, and
`asan.yml`. It exists because the alternative -- running Release `MI_PPROF=ON` ctest,
`MI_PPROF=OFF` ctest, Debug `MI_DEBUG_FULL=ON` ctest, the guarded job, the shared-lib
job, the memory-gate, the diagnostic gates, the Rust workspace, and python-lint one at
a time by hand -- takes a long time and is easy to shortcut under time pressure. Each
config's cmake flags and env are copied verbatim from the workflow file it mirrors
(not re-derived), and `ci/tests/test_verify_local.py` parses the workflow files itself
and fails if a job's flags drift out of sync with the script.

```bash
python ci/verify_local.py                     # everything fast (slow ctest tier excluded)
python ci/verify_local.py --only release,lint  # just these configs
python ci/verify_local.py --slow               # also run the long-tail ctest tier
python ci/verify_local.py --list                # print the config table and exit
python ci/verify_local.py --jobs 8              # override the total build/ctest job budget
python ci/verify_local.py --keep-going          # run every config even after one fails
python ci/verify_local.py --selftest            # trivially fast dry-run, no real builds
```

Ten configs run concurrently (`release`, `off`, `debug-full`, `guarded`, `shared`,
`memory-gate`, `diag`, `rust`, `lint`, `asan`), each building into its own directory
under `out/verify/<config>/` (gitignored, incremental across invocations) with Ninja
and ccache when available. `asan` needs `clang`/`clang++` on `PATH` and reports
SKIPPED with a reason otherwise. The long tests (`test-profile-race`,
`test-subproc-lifecycle`, `test-zero-tracking*`) are excluded from ctest by default
via `-E`; pass `--slow` to include them. On a 16-core machine with a warm cache,
expect on the order of a few minutes wall-clock for the fast tier -- well under the
sum of the per-config times, which the final table reports alongside the wall clock so
the parallel speedup is visible. A failed config prints the last ~40 lines of its full
log (`out/verify/<config>/verify.log`) inline.

# Fast local Linux build loop

On Windows, run the C and Rust test loops through `python ci/dev_linux.py`.
The source tree is a live, read-only Docker bind mount: its NTFS mtimes are
shared with the container and remain stable across container restarts. Build
trees and caches are Docker named volumes. Do not build into a host bind mount:
writing build output through that layer is what causes the costly mtime and
filesystem translation problems.

```powershell
python ci/dev_linux.py doctor
python ci/dev_linux.py c-test
python ci/dev_linux.py rust-test
python ci/dev_linux.py bench
python ci/dev_linux.py bench --reuse  # verify volumes after docker stop/start
```

`bench` is the acceptance check. It measures a cold run, three warm no-op C
runs, and one source edit. It fails when the warm median exceeds 60 seconds,
the edit exceeds 60 seconds, or the volume/no-op/cache invariants are absent.
The local C profile runs CTest's API tests and both upstream stress binaries
with at most four threads (their supported CLI arguments), matching the Docker
Desktop CPU allocation. Native CI remains responsible for the unmodified full
CTest suite.

Use PowerShell. Git Bash requires `MSYS_NO_PATHCONV=1`. Docker Desktop must use
the WSL2 backend and have at least four CPUs and 8 GiB available. `doctor`
checks container/host clock skew; over one second makes comparisons between
host-stamped source files and VM-stamped build output appear stale. A branch
switch also rewrites source mtimes; run `git restore-mtime` if the cache
matters. zccache hashes contents, so an mtime-driven rebuild should remain a
cache-hit path rather than a full recompilation.

For a cold-start recovery, run `python ci/dev_linux.py clean`, then
`python ci/dev_linux.py c-test`. `clean` deliberately removes the Docker
container and named volumes, so it should not be part of normal iteration.
Use `bench --reuse` after restarting the named container to verify that volumes
survive without deliberately wiping them first.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| warm no-op takes minutes | host/VM clock skew over 1 s | Run `doctor`; enable Docker Desktop clock=host. |
| everything rebuilds after a branch switch | checkout rewrote mtimes | Run `git restore-mtime`. |
| every run builds an image | container reuse was bypassed | Use the script; only `up` builds images. |
| first run each session is cold | container/volumes were recreated | Restart the named container; do not run `clean`. |
| `docker: invalid working directory` | MSYS path conversion | Use PowerShell or set `MSYS_NO_PATHCONV=1`. |
| C compilation misses cache | launcher was omitted | Reconfigure with `CMAKE_C_COMPILER_LAUNCHER=zccache`. |
| Ninja rebuilds a fixed subset | generated outputs are retouched | Run `ninja -d explain -n` in `/target/c-build`. |

If Docker Desktop is unavailable, the documented fallback is a host-side soldr
cross-build followed by a slim Linux runtime container. It is slower because
the build runs on the Windows filesystem; run the Docker recovery tool first.
