//! Concurrency oracle — RED/GREEN baseline.
//!
//! Proves the harness exercises genuine concurrency:
//! - RED:  a single-worker run has `max_simultaneous_workers < 2`.
//! - GREEN: a multi-worker run has `max_simultaneous_workers >= 2`.

use stress_harness::{run_scenario, ScenarioType, StressConfig};

fn make_config(name: &str, workers: usize) -> StressConfig {
    StressConfig {
        name: name.into(),
        seed: 42,
        worker_count: workers,
        operation_count: 100_000,
        allocation_size_min: 16,
        allocation_size_max: 256,
        max_duration_secs: Some(30),
    }
}

#[test]
fn red_single_worker_reports_no_concurrency() {
    let result = run_scenario(
        make_config("oracle-single-worker", 1),
        ScenarioType::AllocFree,
    );
    assert!(
        result.max_simultaneous_workers < 2,
        "RED FAIL: single worker reported max_simultaneous_workers={}, expected < 2",
        result.max_simultaneous_workers
    );
    assert!(!result.timed_out, "single worker timed out");
    assert!(!result.crashed, "single worker crashed");
}

#[test]
fn green_multi_worker_reports_real_concurrency() {
    let workers = 4;
    let result = run_scenario(
        make_config("oracle-multi-worker", workers),
        ScenarioType::AllocFree,
    );
    assert!(
        result.max_simultaneous_workers >= 2,
        "GREEN FAIL: {} workers reported max_simultaneous_workers={}, expected >= 2",
        workers,
        result.max_simultaneous_workers
    );
    assert!(!result.timed_out, "multi worker timed out");
    assert!(!result.crashed, "multi worker crashed");

    // Sanity: we should have completed roughly the requested ops.
    let expected = result.config.operation_count as u64;
    let completed = result.ops_completed;
    assert!(
        completed >= expected / 2,
        "only {completed}/{expected} ops completed"
    );
}
