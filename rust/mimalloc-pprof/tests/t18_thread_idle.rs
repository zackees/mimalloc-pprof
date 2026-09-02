//! Issue #272 (Bun parity P7a): `on_thread_idle`, `park_while_idle`, `scavenger_stop`.

use std::sync::mpsc;
use std::thread;

#[test]
fn on_thread_idle_is_safe_on_any_thread() {
    // a thread that never allocated: documented as a no-op, must not crash
    thread::spawn(mimalloc_pprof::on_thread_idle).join().unwrap();
    // ... and on a thread that has
    let v: Vec<u8> = vec![7u8; 4096];
    assert_eq!(v[0], 7);
    drop(v);
    mimalloc_pprof::on_thread_idle();
}

#[test]
fn park_guard_round_trips() {
    // Allocate and free enough to make the park worth taking, then park and wake. The guard
    // may legitimately be `None` (the scavenger is lazily started and may not be running in
    // this test process yet), which is exactly the contract: `_end` must NOT be called then.
    let mut blocks: Vec<Vec<u8>> = (0..64).map(|i| vec![i as u8; 64 * 1024]).collect();
    blocks.truncate(8);
    for _ in 0..4 {
        {
            let _park = mimalloc_pprof::park_while_idle();
            thread::yield_now();
        } // drop -> mi_on_thread_idle_end
        // allocating immediately after the wake must be safe
        let p: Vec<u8> = vec![1u8; 1024];
        assert_eq!(p.len(), 1024);
    }
    drop(blocks);
}

#[test]
fn park_from_several_threads() {
    let (tx, rx) = mpsc::channel();
    let handles: Vec<_> = (0..4)
        .map(|id| {
            let tx = tx.clone();
            thread::spawn(move || {
                for _ in 0..25 {
                    let live: Vec<Vec<u8>> = (0..16).map(|i| vec![(id + i) as u8; 8192]).collect();
                    drop(live);
                    let _park = mimalloc_pprof::park_while_idle();
                    thread::yield_now();
                }
                tx.send(id).unwrap();
            })
        })
        .collect();
    drop(tx);
    let mut seen: Vec<i32> = rx.iter().collect();
    seen.sort_unstable();
    assert_eq!(seen, vec![0, 1, 2, 3]);
    for h in handles {
        h.join().unwrap();
    }
}

#[test]
fn scavenger_stop_is_idempotent_and_leaves_the_process_usable() {
    mimalloc_pprof::scavenger_stop();
    mimalloc_pprof::scavenger_stop();
    let v: Vec<u8> = vec![3u8; 16 * 1024];
    assert_eq!(v[42], 3);
    // and the inline path still works with no scavenger to hand off to
    mimalloc_pprof::on_thread_idle();
}
