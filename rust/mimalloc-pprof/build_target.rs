pub(crate) fn needs_c11_atomics(target: Option<&str>) -> bool {
    target == Some("aarch64-pc-windows-msvc")
}
