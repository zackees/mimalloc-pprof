# Changelog

## 0.8.0

First real release of `mimalloc-pprof`.

- mimalloc allocator (Windows-first fork of microsoft/mimalloc), drop-in `mi_malloc`/`mi_free`/`mi_realloc`.
- pprof-compatible statistical sampling heap profiler (`mi_prof_start`/`mi_prof_start_ex`,
  `mi_prof_dump`/`mi_prof_dump_proto`).
- Opt-in memory-event accounting/callback API (`mi_memory_tracking_set_enabled`,
  `mi_memory_set_callbacks`, issue #20).
- Single-file C amalgamation (`vendor/mimalloc-pprof-amalgamated.{c,h}`) for non-CMake
  consumers, shipped as a release ZIP alongside the crates.io publish.

0.9.0 is reserved for a v3 mimalloc port (issue #29, in progress); 1.0 lands after further
stress testing.
