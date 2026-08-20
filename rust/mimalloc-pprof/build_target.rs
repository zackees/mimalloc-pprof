pub(crate) fn needs_cxx_compilation(target: Option<&str>) -> bool {
    target == Some("aarch64-pc-windows-msvc")
}
