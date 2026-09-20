//! What each cargo feature does, and what its absence does (#414).
//!
//! Since 0.12.0 `default = []` and every observability subsystem is opt-in, so this file
//! is the contract for all five features at once: with the feature the subsystem really
//! works, and without it the *same* API still links and reports itself off. That second
//! half is the load-bearing one -- it is what lets a downstream crate call this API with
//! no `#[cfg]` of its own, and what `ci/check_rust_surface.py` and the README's API table
//! assume. No `required-features`: this test runs in every configuration.
//!
//! Deliberately ONE `#[test]`, run in sections, for the same reason t20_memory_events and
//! t21_visit_live are: every subsystem here is switched through PROCESS-global state (the
//! profiler flag, DHAT's, memory-events tracking), and cargo runs the tests of one binary
//! on several threads at once -- so a second test flipping the very flag the first is
//! asserting on is not a test, it is a race. `purge_all` makes that concrete: run
//! concurrently with unrelated allocation it can trip a debug assertion in the hole sweep
//! (issue #417), which reproduces on `main` too and is NOT introduced here.

use mimalloc_pprof::{dhat, memory_events, prof, MiMalloc};

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

#[test]
fn published_feature_contract() {
    dhat_and_pprof();
    memory_events_section();
    diagnostics_section();
    owner_gate_section();
}

fn dhat_and_pprof() {
    if dhat::is_enabled() {
        dhat::stop();
    }

    #[cfg(feature = "dhat")]
    {
        assert!(
            dhat::start(),
            "the dhat feature must compile the observer in"
        );

        let allocation = vec![0x5au8; 64 * 1024];
        std::hint::black_box(&allocation);
        let stats = dhat::stats();
        assert!(stats.enabled);
        assert!(stats.total_blocks > 0);
        assert!(stats.total_bytes >= allocation.len() as u64);

        dhat::stop();
        assert!(!dhat::is_enabled());
    }

    #[cfg(not(feature = "dhat"))]
    {
        assert!(
            !dhat::start(),
            "without the dhat feature DHAT must use the C stubs"
        );
        assert!(!dhat::is_enabled());
        assert!(!dhat::stats().enabled);
    }

    #[cfg(feature = "pprof")]
    {
        assert!(prof::start(4096), "the pprof feature must compile pprof in");
        assert!(prof::is_enabled());
        prof::stop();
    }

    #[cfg(not(feature = "pprof"))]
    {
        assert!(
            !prof::start(4096),
            "without the pprof feature the profiler must use the C stubs"
        );
        assert!(!prof::is_enabled());
    }
}

fn memory_events_section() {
    // `dhat` implies `memory-events`, so this is the same cfg on both sides.
    #[cfg(feature = "memory-events")]
    {
        assert!(
            memory_events::set_enabled(true),
            "the memory-events feature must compile the accounting in"
        );
        assert!(memory_events::is_enabled());
        let before = memory_events::snapshot().expect("snapshot").accum_count;
        let v = vec![0u8; 8192];
        std::hint::black_box(&v);
        let after = memory_events::snapshot().expect("snapshot").accum_count;
        assert!(after > before, "accounting must advance while enabled");
        assert!(memory_events::set_enabled(false));
        assert!(!memory_events::is_enabled());
    }

    #[cfg(not(feature = "memory-events"))]
    {
        assert!(
            !memory_events::set_enabled(true),
            "without the memory-events feature the accounting must use the C stubs"
        );
        assert!(!memory_events::is_enabled());
        assert!(memory_events::snapshot().is_none());
        assert!(!memory_events::clear_callbacks());
    }
}

fn diagnostics_section() {
    let path = std::env::temp_dir().join(format!(
        "mimalloc-pprof-feature-contract-{}.bin",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);

    #[cfg(feature = "diagnostics")]
    {
        let json = mimalloc_pprof::heap_dump_json(false, false)
            .expect("the diagnostics feature must compile the dump in");
        assert!(json.contains("\"heaps\""));
        mimalloc_pprof::heap_snapshot_to_file(&path, false)
            .expect("the diagnostics feature must compile the snapshot writer in");
        assert!(std::fs::metadata(&path).expect("snapshot file").len() > 0);
    }

    #[cfg(not(feature = "diagnostics"))]
    {
        assert!(
            mimalloc_pprof::heap_dump_json(false, false).is_none(),
            "without the diagnostics feature the dump must use the C stubs"
        );
        assert!(mimalloc_pprof::heap_dump_json_ex(true, true, 0).is_none());
        assert!(mimalloc_pprof::heap_snapshot_to_file(&path, false).is_err());
    }

    let _ = std::fs::remove_file(&path);
}

fn owner_gate_section() {
    // `gated` reports how the C code was COMPILED, which is exactly this feature.
    let report = mimalloc_pprof::purge_all(false);
    assert_eq!(report.gated, cfg!(feature = "owner-gate"));
}
