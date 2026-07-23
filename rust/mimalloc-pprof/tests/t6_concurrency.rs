mod common;
use std::thread;
use std::time::{Duration, Instant};

#[test]
fn concurrent_dumps_and_reentrant_vec_writer_do_not_deadlock() {
    common::start(4096, 71);
    let keep_samples: Vec<_> = (0..128)
        .map(|_| Box::leak(Box::new([0_u8; 4096])))
        .collect();
    let workers: Vec<_> = (0..8)
        .map(|_| {
            thread::spawn(|| {
                for _ in 0..100_000 {
                    let value = Box::new([0_u8; 32]);
                    drop(value);
                }
            })
        })
        .collect();
    let deadline = Instant::now() + Duration::from_secs(60);
    for _ in 0..10 {
        let text = common::dump();
        assert!(!common::sample_lines(&text).is_empty());
        assert!(Instant::now() < deadline, "dump watchdog expired");
    }
    for worker in workers {
        worker.join().unwrap();
    }
    std::mem::forget(keep_samples);
    common::stop();
}
