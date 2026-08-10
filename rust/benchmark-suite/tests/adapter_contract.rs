use benchmark_suite::adapter::{
    AdapterError, LinkedAdapter, BUILD_ALLOCATOR_ID, BUILD_ALLOCATOR_VERSION, BUILD_LIBRARY_SHA256,
    BUILD_SOURCE_SHA,
};

#[test]
fn ordinary_workspace_test_build_cannot_masquerade_as_a_linked_child() {
    assert_eq!(LinkedAdapter::load(), Err(AdapterError::Unlinked));
    assert_eq!(BUILD_ALLOCATOR_ID, "unlinked-test-adapter");
    assert_eq!(BUILD_ALLOCATOR_VERSION, "unlinked");
    assert_eq!(BUILD_SOURCE_SHA, "unlinked");
    assert_eq!(BUILD_LIBRARY_SHA256, "unlinked");
}
