mod common;
use std::alloc::{alloc, dealloc, realloc, Layout};
use std::thread;

#[test]
fn allocator_handles_sizes_alignment_realloc_and_cross_thread_free() {
    common::start(4096, 11);
    for size in [8, 16, 64, 4096, 65_536, 1_048_576] {
        let mut value = vec![0_u8; size];
        value[0] = 1;
        value.resize(size.saturating_add(17), 2);
        assert_eq!(value[0], 1);
    }
    unsafe {
        for alignment in [64, 4096] {
            let layout = Layout::from_size_align(8192, alignment).unwrap();
            let ptr = alloc(layout);
            assert!(!ptr.is_null());
            assert_eq!((ptr as usize) % alignment, 0);
            let bigger = realloc(ptr, layout, 16_384);
            assert!(!bigger.is_null());
            dealloc(bigger, Layout::from_size_align(16_384, alignment).unwrap());
        }
    }
    let value = Box::new([9_u8; 4096]);
    let joined = thread::spawn(move || {
        assert_eq!(value[0], 9);
        drop(value);
    });
    joined.join().unwrap();
    common::stop();
}
