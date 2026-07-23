# mimalloc-pprof

A fork of [microsoft/mimalloc](https://github.com/microsoft/mimalloc) that adds
**pprof-compatible sampled heap profiling** — including on **native Windows**, where no
drop-in allocator offers pprof heap profiles today (gperftools' heap profiler doesn't build
there, jemalloc's `prof` is POSIX-only, and memray/bytehound/heaptrack/heapprofd are
Linux-first).

Profiles are analyzed with the standard [google/pprof](https://github.com/google/pprof)
tooling (`pprof -http`, flamegraphs, `-base` diffs).

> **Status: design phase.** The architecture and phased implementation plan live in
> [issue #1](https://github.com/zackees/mimalloc-pprof/issues/1). Nothing is implemented yet.

## What this adds on top of mimalloc

- Byte-interval **allocation sampling** (tcmalloc-style, ~512 KiB default rate) with
  near-zero cost when disabled and bounded cost when enabled. Allocator-side design based on
  Datadog's draft [microsoft/mimalloc#1266](https://github.com/microsoft/mimalloc/pull/1266).
- **Stack capture** per sample: `RtlCaptureStackBackTrace` on Windows, frame-pointer /
  libunwind on Linux and macOS.
- **Heap profile dumps** in the gperftools `heap_v2` text format (accepted by pprof), with
  the module map (`MAPPED_LIBRARIES`) emitted per-OS so pprof can symbolize offline.
  A binary `profile.proto` encoder and optional dbghelp runtime symbolization come later.
- **Rust crates** (`rust/` workspace): `libmimalloc-pprof-sys` and `mimalloc-pprof`
  (a `#[global_allocator]` plus a safe profiling-control API).

## Repository layout

```
/                      # the C library — a real fork of microsoft/mimalloc (shared git history)
├── include/ src/ test/ CMakeLists.txt
├── readme-upstream.md # upstream's readme.md (renamed so this file can exist on Windows)
└── rust/              # cargo workspace (planned, see issue #1)
    ├── libmimalloc-pprof-sys/
    └── mimalloc-pprof/
```

This is a **mono-repo on purpose**: the profiling feature spans the C core (sampling hooks)
and the Rust API (control/dump), so they change together in atomic commits, and one CI
matrix proves Windows + cross-compilation on every change.

## Relationship to upstream

- The repo root **is** mimalloc — full upstream git history, based on the v2 line
  (upstream `main`, v2.4.1+). The v3 line (`dev3`) is a later milestone.
- Syncing: `git fetch upstream && git merge upstream/main` (the `rust/` directory never
  conflicts). Upstream edits to `readme.md` follow the rename via git rename detection.
- Upstreaming: C-only commits are cherry-picked onto ephemeral `pr/*` branches cut from
  `upstream/main` and submitted to microsoft/mimalloc. Keeping C-core and `rust/` changes
  in separate commits is a repo rule for exactly this reason.

## Building

The C library builds exactly like upstream mimalloc:

```
cmake -B build && cmake --build build && ctest --test-dir build
```

See `readme-upstream.md` for the full upstream documentation. The Rust workspace and the
soldr-based cross-compile CI are specified in issue #1.

## Prior art & credits

- [microsoft/mimalloc](https://github.com/microsoft/mimalloc) — Daan Leijen (MIT).
- [microsoft/mimalloc#1266](https://github.com/microsoft/mimalloc/pull/1266) (Datadog) —
  sampling-hook design this fork builds on; [#1070](https://github.com/microsoft/mimalloc/issues/1070)
  is the upstream discussion asking for sampled allocation events.
- [gperftools](https://github.com/gperftools/gperftools) — the `heap_v2` dump format;
  [google/pprof](https://github.com/google/pprof) still parses it (`profile/legacy_profile.go`).

## License

MIT, same as upstream — see [LICENSE](LICENSE).
