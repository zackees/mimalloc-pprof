#[path = "../build_target.rs"]
mod build_target;

#[test]
fn windows_arm64_uses_c11_atomics() {
    assert!(build_target::needs_c11_atomics(Some(
        "aarch64-pc-windows-msvc"
    )));
}

#[test]
fn other_targets_keep_default_atomics() {
    for target in [
        None,
        Some("x86_64-pc-windows-msvc"),
        Some("aarch64-unknown-linux-gnu"),
        Some("aarch64-apple-darwin"),
    ] {
        assert!(!build_target::needs_c11_atomics(target));
    }
}
