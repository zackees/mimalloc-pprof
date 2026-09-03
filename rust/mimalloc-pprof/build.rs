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
    // The ABI layout probe (see layout_probe.c) is compiled into the same static
    // library. It is referenced only from tests/t19_layout.rs, so the linker drops it
    // from ordinary consumers; keeping it in this one `cc::Build` means it always sees
    // exactly the defines the library itself was built with.
    println!("cargo:rerun-if-changed=layout_probe.c");

    let mut build = cc::Build::new();
    build
        .include("vendor")
        .file("vendor/mimalloc-pprof-amalgamated.c")
        .file("layout_probe.c")
        .define("MI_STATIC_LIB", None)
        // Preserve the crate's default profiler-enabled behavior, while
        // allowing allocator-only consumers to compile hooks out with
        // `default-features = false`.
        .define(
            "MI_PPROF",
            if env::var_os("CARGO_FEATURE_PPROF").is_some() {
                "1"
            } else {
                "0"
            },
        );
    if env::var("PROFILE").as_deref() == Ok("release") {
        build.define("NDEBUG", None);
    }
    build.compile("mimalloc");

    if env::var("TARGET").is_ok_and(|target| target.contains("windows")) {
        // `synchronization` is WaitOnAddress/WakeByAddressSingle, used by the background
        // scavenger (src/scavenger.c, issue #272). The `#pragma comment(lib, ...)` in that
        // file only reaches MSVC/clang-cl, so the *-pc-windows-gnu targets need this.
        for library in ["psapi", "shell32", "user32", "advapi32", "bcrypt", "synchronization"] {
            println!("cargo:rustc-link-lib={library}");
        }
    }
}
