# mimalloc-pprof (v2 engine)

[mimalloc](https://github.com/microsoft/mimalloc) as a Rust global allocator, with
**pprof-compatible sampled heap profiling** built in. Windows is a first-class
target alongside Linux and macOS.

Dumps open directly in [google/pprof](https://github.com/google/pprof) for flame
graphs, call graphs, top reports, and profile diffs.

> **This is the 0.8.x line, built on the mimalloc v2 engine.**
> The current line is [**0.9.x**](https://crates.io/crates/mimalloc-pprof), built on
> mimalloc v3, and is what most users should take. The profiler API, environment
> variables, and output formats are identical, so moving between them is a version
> bump rather than a code change. 0.8.x remains available for anyone who prefers the
> longer-established v2 allocator.

## Upgrade note for 0.8.0 users

**0.8.0 leaks memory on Windows/MinGW and should not be used.** Every exiting thread
leaked its thread-local heap and pages — about 0.24 GB per stress iteration, reaching
23.5 GB at 100 iterations, growing without bound. The process still exited
successfully, so it was invisible on a large-memory machine and only surfaced as an
out-of-memory failure on a smaller one.

0.8.1 fixes it: flat at 0.28 GB across the same run. MSVC builds were never affected.
Details in [issue #47](https://github.com/zackees/mimalloc-pprof/issues/47).

## Usage

```toml
[dependencies]
mimalloc-pprof = "0.8"

[profile.release]
debug = "line-tables-only"
strip = false
```

```rust
use mimalloc_pprof::{prof, MiMalloc};
use std::path::Path;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

fn main() -> std::io::Result<()> {
    assert!(prof::start(0), "profiler already running"); // 0 = default, ~512 KiB

    let retained = vec![0_u8; 1024 * 1024];
    prof::dump_file(Path::new("heap.prof"))?;            // dump while still live
    std::hint::black_box(&retained);

    prof::stop();
    Ok(())
}
```

Then:

```sh
pprof -http=:0 ./target/release/my_app heap.prof
```

Or profile without touching the code at all:

```sh
MIMALLOC_PROF=1 MIMALLOC_PROF_DUMP_AT_EXIT=heap.prof ./my_app
```

## Notes

- On Linux and macOS, keep frame pointers for reliable stack walking:
  `rustflags = ["-Cforce-frame-pointers=yes"]` in `.cargo/config.toml`.
  Windows x64 uses unwind information instead — keep the PDB.
- Do not link a second mimalloc into the same process; this crate vendors its own.
- The per-heap allocator statistics (`ProfStats::heap`) are a v3 feature and are not
  available on this line.

Full documentation is in the
[repository README](https://github.com/zackees/mimalloc-pprof).

## License

MIT, the same as upstream mimalloc.
