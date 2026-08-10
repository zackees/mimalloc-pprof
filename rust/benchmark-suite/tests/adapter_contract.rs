use benchmark_suite::adapter::{
    validate_runtime_identity, AdapterError, LinkedAdapter, BUILD_ALLOCATOR_ID,
    BUILD_ALLOCATOR_VERSION, BUILD_LIBRARY_SHA256, BUILD_SOURCE_SHA,
};

#[test]
fn ordinary_workspace_test_build_cannot_masquerade_as_a_linked_child() {
    assert_eq!(LinkedAdapter::load(), Err(AdapterError::Unlinked));
    assert_eq!(BUILD_ALLOCATOR_ID, "unlinked-test-adapter");
    assert_eq!(BUILD_ALLOCATOR_VERSION, "unlinked");
    assert_eq!(BUILD_SOURCE_SHA, "unlinked");
    assert_eq!(BUILD_LIBRARY_SHA256, "unlinked");
}

#[test]
fn runtime_adapter_identity_must_match_the_compiled_envelope() {
    assert_eq!(
        validate_runtime_identity("wrong-adapter", BUILD_ALLOCATOR_VERSION),
        Err(AdapterError::IdentityMismatch {
            field: "ID",
            expected: BUILD_ALLOCATOR_ID,
            actual: "wrong-adapter".into(),
        })
    );
    assert_eq!(
        validate_runtime_identity(BUILD_ALLOCATOR_ID, "wrong-version"),
        Err(AdapterError::IdentityMismatch {
            field: "version",
            expected: BUILD_ALLOCATOR_VERSION,
            actual: "wrong-version".into(),
        })
    );
    assert_eq!(
        validate_runtime_identity(BUILD_ALLOCATOR_ID, BUILD_ALLOCATOR_VERSION),
        Ok(())
    );
}
