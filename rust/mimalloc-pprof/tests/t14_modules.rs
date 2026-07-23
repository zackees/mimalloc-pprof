mod common;
use mimalloc_pprof::prof;

#[test]
fn modules_lists_loaded_modules_including_this_test_binary() {
    let modules = prof::modules();
    assert!(!modules.is_empty());

    for module in &modules {
        assert!(module.size > 0, "module has zero size: {module:?}");
        assert!(!module.path.is_empty(), "module has an empty path");
    }

    // Cargo names integration-test binaries `<stem>-<hash>[.exe]`, so we
    // cannot match on the crate name ("t14_modules") alone. Instead, take
    // the *running* executable's own file stem (file name minus one
    // extension) via `current_exe` and check that it is a substring of some
    // enumerated module's path. On MSVC and MinGW alike the main executable
    // is reported with a `.exe` extension, so stripping exactly one
    // extension leaves `t14_modules-<hash>`, which is exactly the fragment
    // that appears inside that module's full path; on Unix-like targets
    // there is no extension to strip and the same containment check still
    // holds.
    let exe = std::env::current_exe().expect("current_exe");
    let exe_stem = exe
        .file_stem()
        .and_then(|s| s.to_str())
        .expect("exe file stem is valid UTF-8");

    assert!(
        modules.iter().any(|m| m.path.contains(exe_stem)),
        "no module path contains {exe_stem:?}; paths: {:?}",
        modules.iter().map(|m| &m.path).collect::<Vec<_>>()
    );
}
