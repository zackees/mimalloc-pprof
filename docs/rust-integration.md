# Rust integration

*Part of the [mimalloc-pprof](../README.md) documentation.*

```toml
[dependencies]
mimalloc-pprof = "0.9"          # v3 engine (current)
# mimalloc-pprof = "0.8"        # v2 engine

[profile.release]
debug = "line-tables-only"
strip = false
```

Or against a checkout:

```toml
mimalloc-pprof = { path = "../mimalloc-pprof/rust/mimalloc-pprof" }
```

Install the allocator once, start profiling before the workload, and dump while
the allocations of interest are still live:

```rust
use mimalloc_pprof::{prof, MiMalloc};
use std::path::Path;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

fn main() -> std::io::Result<()> {
    if !prof::start(0) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "heap profiler already active",
        ));
    }

    let retained = vec![0_u8; 1024 * 1024];
    prof::dump_file(Path::new("heap.prof"))?;
    std::hint::black_box(&retained);
    prof::stop();
    Ok(())
}
```

## Allocator statistics

On v3 (0.9.x), `prof::stats()` carries the exact allocator counters alongside the
sampled ones:

```rust
let s = mimalloc_pprof::prof::stats();
println!(
    "sampled live: {} bytes in {} samples; allocator committed: {}, requested: {}",
    s.live_bytes, s.live_samples, s.heap.committed, s.heap.malloc_requested,
);
```

See [allocator statistics in the profile](profiler.md#allocator-statistics-in-the-profile-v3-only)
for what these counters mean and their caveats.

## Frame pointers and symbols

On Linux and macOS, retain frame pointers for reliable stack walking. This is the same
requirement as [the C build flags](c-integration.md#build-flags-for-usable-stacks),
expressed for cargo:

```toml
# .cargo/config.toml
[build]
rustflags = ["-Cforce-frame-pointers=yes"]
```

Windows x64 uses unwind information instead; keep the generated PDB for
symbolization.

## Cross-compilation

The crate vendors mimalloc as a single amalgamated C translation unit with no
autotools or CMake step, so it builds wherever `cc-rs` can reach a C compiler,
cross-compiled builds included.

`aarch64-pc-windows-msvc` needs one adjustment, and `build.rs` makes it itself.
In plain C mode mimalloc models C11 atomics with a deprecated MSVC
`Interlocked` wrapper whose `_acq`/`_rel` ARM64 intrinsics `clang-cl` does not
declare, so a `cargo-xwin` cross build cannot compile it. Since **0.9.3** the
build script compiles that target against clang's C11 `stdatomic`
implementation instead.

Selecting this inside `build.rs` is the point. `CFLAGS` applies to every
`cc-rs` build script in a build, so a consumer forcing mimalloc's C++ atomics
path from the outside with `CFLAGS=-TP` also flips the language mode of every
other native dependency in the graph — `ring`, for one, fails to compile as
C++. No consumer should have to know how this crate selects its atomics.
