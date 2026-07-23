mod common;
use mimalloc_pprof::prof;
use std::thread;
use std::time::{Duration, Instant};

#[test]
fn snapshot_visit_is_safe_under_concurrent_alloc_free() {
    common::start(4096, 103);
    let deadline = Instant::now() + Duration::from_millis(50);
    let workers: Vec<_> = (0..8)
        .map(|_| {
            thread::spawn(move || {
                while Instant::now() < deadline {
                    let value = vec![0_u8; 4096];
                    drop(value);
                }
            })
        })
        .collect();

    let mut saw_samples = false;
    for _ in 0..20 {
        if !prof::samples().is_empty() {
            saw_samples = true;
        }
        if Instant::now() >= deadline {
            break;
        }
        thread::sleep(Duration::from_millis(2));
    }

    for worker in workers {
        worker.join().unwrap();
    }
    assert!(saw_samples, "expected at least one non-empty snapshot");

    common::stop();
    assert!(prof::samples().is_empty());
}
