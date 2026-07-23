use std::env;

fn main() {
    println!("cargo:rerun-if-changed=../../src");
    println!("cargo:rerun-if-changed=../../include");

    let mut build = cc::Build::new();
    build
        .include("../../include")
        .file("../../src/static.c")
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
