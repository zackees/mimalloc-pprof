use core::ffi::c_void;
use mimalloc_pprof::{sys, MiMalloc};

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

#[test]
fn global_allocator_and_raw_bindings_work() {
    let mut values = Vec::with_capacity(4);
    values.extend(0..1024usize);
    assert_eq!(values.len(), 1024);

    let mut text = String::from("mimalloc");
    text.push_str("-pprof");
    assert_eq!(text, "mimalloc-pprof");

    let boxed = Box::new([42_u8; 4096]);
    assert_eq!(boxed[0], 42);

    unsafe {
        let allocation = sys::mi_malloc(100);
        assert!(!allocation.is_null());
        assert!(sys::mi_usable_size(allocation.cast::<c_void>()) >= 100);
        sys::mi_free(allocation);
    }
}
