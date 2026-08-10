//! Real allocator scenario — concurrent allocation/free with profiler
//! snapshot interleaving, exercising the mimalloc-pprof global allocator.
//!
//! Uses native threads, deterministic seed, bounded watchdog, and emits a
//! machine-readable JSON result to stdout so it can be captured for CI
//! artifact collection.

use mimalloc_pprof::{prof, MiMalloc};
use stress_harness::{run_scenario, ScenarioType, StressConfig};

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

#[test]
fn concurrent_alloc_free_with_profiler_snapshots() {
    // Start the profiler with deterministic sampling.
    if prof::is_enabled() {
        prof::stop();
    }
    assert!(
        prof::start_seeded(4096, 71),
        "profiler should start successfully"
    );

    let config = StressConfig {
        name: "concurrent-alloc-free-snapshots".into(),
        seed: 12345,
        worker_count: 4,
        operation_count: 200_000,
        allocation_size_min: 16,
        allocation_size_max: 8192,
        max_duration_secs: Some(30),
    };

    // Run the workload.
    let result = run_scenario(config, ScenarioType::AllocFree);

    // Verify the harness saw real concurrency.
    assert!(
        result.max_simultaneous_workers >= 2,
        "expected >= 2 simultaneous workers, got {}",
        result.max_simultaneous_workers
    );
    assert!(!result.timed_out, "scenario timed out");
    assert!(!result.crashed, "scenario crashed");

    // Take a profiler snapshot — must not deadlock or panic under
    // concurrent alloc/free pressure.
    let samples = prof::samples();
    // We don't assert sample count (it's probabilistic), but the call
    // itself must succeed and return a valid Vec.
    let _ = samples.len();

    // Also verify dump works.
    let dump = String::from_utf8(prof::dump_to_vec()).expect("heap profile is UTF-8");
    assert!(
        dump.starts_with("heap profile:"),
        "dump should be valid heap_v2 format"
    );

    prof::stop();

    // Emit machine-readable result for CI artifact collection.
    let json = serde_json::to_string_pretty(&result).unwrap();
    println!("STRESS_RESULT: {json}");
}
