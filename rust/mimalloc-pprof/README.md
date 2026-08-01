# mimalloc-pprof

> ## mimalloc with native pprof-compatible heap profiling — on Windows, Linux, and macOS alike.
>
> **The one mimalloc heap profiler that runs natively on Windows.** Upstream mimalloc
> has no profiler at all, and the only other known implementation
> ([Bun's](https://github.com/oven-sh/mimalloc)) is POSIX-only — its stack capture is
> guarded behind glibc/Apple `<execinfo.h>`.

[mimalloc](https://github.com/microsoft/mimalloc) as a Rust global allocator, with
**pprof-compatible sampled heap profiling** built in. Windows is a first-class
target alongside Linux and macOS.

Dumps open directly in [google/pprof](https://github.com/google/pprof) for flame
graphs, call graphs, top reports, and profile diffs.

## Usage

```toml
[dependencies]
mimalloc-pprof = "0.9"

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

## Versions

| | crate | engine |
|---|---|---|
| **0.9.x** — current | `mimalloc-pprof = "0.9"` | mimalloc v3 |
| 0.8.x — previous | `mimalloc-pprof = "0.8"` | mimalloc v2 |

The profiler API, environment variables, and output formats are identical in both,
so moving between them is a version bump rather than a code change.

**0.9.x is recommended.** It has strictly more test coverage, per-heap allocator
statistics, and fixes two upstream mimalloc bugs that 0.8.x still carries —
including an unbounded memory leak on Windows/MinGW where every exiting thread
leaked its heap and pages (23.5 GB at 100 stress iterations, versus flat after the
fix). Note that upstream mimalloc v3 is itself still a pre-release branch.

## Exact statistics alongside sampled ones

On 0.9.x, `prof::stats()` carries the allocator's **exact** counters next to the
sampled ones. A sampled profile alone cannot tell you whether it under-counted;
comparing the two measures the sampling error directly:

```rust
let s = mimalloc_pprof::prof::stats();
println!(
    "sampled live: {} bytes in {} samples; allocator committed: {}, requested: {}",
    s.live_bytes, s.live_samples, s.heap.committed, s.heap.malloc_requested,
);
```

## Notes

- On Linux and macOS, keep frame pointers for reliable stack walking:
  `rustflags = ["-Cforce-frame-pointers=yes"]` in `.cargo/config.toml`.
  Windows x64 uses unwind information instead — keep the PDB.
- Do not link a second mimalloc into the same process; this crate vendors its own.

Full documentation, the C API, and the upstream-bug details are in the
[repository README](https://github.com/zackees/mimalloc-pprof).

## License

MIT, the same as upstream mimalloc.
