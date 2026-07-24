use std::env;

fn main() {
    // Everything the compiler needs lives in vendor/: the amalgamated
    // translation unit plus two verbatim support headers it opens via
    // `#include <...>` (see rust/xtask/src/main.rs's
    // ANGLE_BRACKET_SUPPORT_HEADERS for why). No path outside vendor/ is
    // referenced, so this crate no longer reads ../../src or ../../include
    // at build time -- only `cargo run -p xtask -- amalgamate-c/-h` does.
    println!("cargo:rerun-if-changed=vendor/mimalloc-pprof-amalgamated.c");
    println!("cargo:rerun-if-changed=vendor/mimalloc-pprof-amalgamated.h");
    println!("cargo:rerun-if-changed=vendor/mimalloc.h");
    println!("cargo:rerun-if-changed=vendor/mimalloc-stats.h");

    let mut build = cc::Build::new();
    build
        .include("vendor")
        .file("vendor/mimalloc-pprof-amalgamated.c")
        .define("MI_STATIC_LIB", None)
        // This is a no-op until Phase 1; keeping the define here ensures the
        // Rust build exercises profiling code as soon as it exists.
        .define("MI_PPROF", "1");
    if env::var("PROFILE").as_deref() == Ok("release") {
        build.define("NDEBUG", None);
    }
    build.compile("mimalloc");

    if env::var("TARGET").is_ok_and(|target| target.contains("windows")) {
        for library in ["psapi", "shell32", "user32", "advapi32", "bcrypt"] {
            println!("cargo:rustc-link-lib={library}");
        }
    }
}
