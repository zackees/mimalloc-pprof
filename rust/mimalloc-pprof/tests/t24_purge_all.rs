//! `purge_all` / `purge_all_ex` (include/mimalloc.h, issue #366).
//!
//! Runs in both feature modes. With `owner-gate` the sweeper can claim every thread; without
//! it only parked threads are claimable and a running main thread is reported as pending.
//! Either way the call must never come back `Busy` when it is the only purge in flight, and
//! `report.gated` must tell the truth about how the C code was compiled.

use std::sync::mpsc;
use std::thread;

#[global_allocator]
static ALLOCATOR: mimalloc_pprof::MiMalloc = mimalloc_pprof::MiMalloc;

fn churn() -> Vec<Vec<u8>> {
    let mut held: Vec<Vec<u8>> = (0..1024).map(|i| vec![i as u8; 4096]).collect();
    // leave scattered survivors so there are holes and live pages on this thread
    held.retain(|v| (v.as_ptr() as usize / 4096).is_multiple_of(3));
    std::hint::black_box(&held);
    held
}

#[test]
fn purge_all_from_a_second_thread_with_live_allocations() {
    let held = churn();

    let (tx, rx) = mpsc::channel();
    let sweeper = thread::spawn(move || {
        // its own live state too, so the caller's theap is part of what is swept
        let mine = churn();
        let outcome = mimalloc_pprof::purge_all_ex(mimalloc_pprof::PurgeFlags::FORCE, 100);
        drop(mine);
        tx.send(outcome).unwrap();
    });
    let (status, report) = rx.recv().unwrap();
    sweeper.join().unwrap();

    assert_ne!(
        status,
        mimalloc_pprof::PurgeStatus::Busy,
        "the only purge in flight must never be reported busy: {report:?}"
    );
    assert!(
        matches!(
            status,
            mimalloc_pprof::PurgeStatus::Ok | mimalloc_pprof::PurgeStatus::Partial
        ),
        "{status:?} {report:?}"
    );
    assert_eq!(
        report.gated,
        cfg!(feature = "owner-gate"),
        "report.gated must mirror the crate feature the C code was built with: {report:?}"
    );
    assert!(
        report.theaps_swept >= 1,
        "the caller sweeps itself: {report:?}"
    );
    assert_eq!(
        report.complete,
        report.theaps_pending == 0 && report.theaps_orphaned == 0,
        "{report:?}"
    );
    if status == mimalloc_pprof::PurgeStatus::Ok {
        assert_eq!(report.theaps_pending, 0, "{report:?}");
    } else {
        assert!(report.theaps_pending > 0, "{report:?}");
    }

    // still usable afterwards
    drop(held);
    let after: Vec<u8> = vec![9u8; 1 << 16];
    assert_eq!(after[0], 9);
}

#[test]
fn purge_all_convenience_form() {
    let held = churn();
    let report = mimalloc_pprof::purge_all(true);
    assert_eq!(report.gated, cfg!(feature = "owner-gate"));
    assert!(report.theaps_swept >= 1, "{report:?}");
    let report = mimalloc_pprof::purge_all(false);
    assert_eq!(report.gated, cfg!(feature = "owner-gate"));
    drop(held);
}
