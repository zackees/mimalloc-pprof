mod common;
use mimalloc_pprof::prof;
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

#[test]
fn snapshot_visit_is_safe_under_concurrent_alloc_free() {
    common::start(4096, 103);
    let run_for = Duration::from_millis(50);
    let ready = Arc::new(Barrier::new(9));
    let workers: Vec<_> = (0..8)
        .map(|_| {
            let ready = Arc::clone(&ready);
            thread::spawn(move || {
                let mut live = Vec::with_capacity(64);
                for _ in 0..64 {
                    live.push(vec![0_u8; 4096]);
                }
                ready.wait();
                let deadline = Instant::now() + run_for;
                while Instant::now() < deadline {
                    live.remove(0);
                    live.push(vec![0_u8; 4096]);
                }
                std::hint::black_box(live);
            })
        })
        .collect();

    // Do not race the first snapshot against worker startup. Every worker now
    // retains a live allocation set while concurrent frees/replacements run.
    ready.wait();
    let deadline = Instant::now() + run_for;

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
