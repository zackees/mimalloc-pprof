#[path = "../build_target.rs"]
mod build_target;

#[test]
fn windows_arm64_uses_cxx_compilation() {
    assert!(build_target::needs_cxx_compilation(Some(
        "aarch64-pc-windows-msvc"
    )));
}

#[test]
fn other_targets_remain_c_compilation() {
    for target in [
        None,
        Some("x86_64-pc-windows-msvc"),
        Some("aarch64-unknown-linux-gnu"),
        Some("aarch64-apple-darwin"),
    ] {
        assert!(!build_target::needs_cxx_compilation(target));
    }
}
