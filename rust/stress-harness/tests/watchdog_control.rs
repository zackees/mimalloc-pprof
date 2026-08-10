//! Watchdog positive-control tests.
//!
//! Proves the child-process isolation + watchdog path:
//! - A planted hang is killed by the watchdog and reports `timed_out: true`.
//! - A clean short run exits normally and reports `timed_out: false`.

use stress_harness::{run_in_child_process, ScenarioType, StressConfig, CHILD_ENV_VAR};

// ---------------------------------------------------------------------------
// Child-mode entry point — invoked when CHILD_ENV_VAR is set
// ---------------------------------------------------------------------------

#[test]
fn child_process_isolation() {
    if std::env::var(CHILD_ENV_VAR).is_ok() {
        stress_harness::run_child_mode();
        // unreachable — run_child_mode exits the process
    }

    // Not in child mode — run the actual tests below.
    watchdog_kills_planted_hang();
    watchdog_passes_clean_short_run();
}

// ---------------------------------------------------------------------------
// Tests (called from the parent path of `child_process_isolation`)
// ---------------------------------------------------------------------------

fn watchdog_kills_planted_hang() {
    let config = StressConfig {
        name: "planted-hang-control".into(),
        seed: 0xDEAD,
        worker_count: 1,
        operation_count: 1,
        allocation_size_min: 16,
        allocation_size_max: 16,
    };

    let result = run_in_child_process(config, ScenarioType::Hang, 10);

    assert!(
        result.timed_out,
        "watchdog should have killed the planted hang, got timed_out={}",
        result.timed_out
    );
    assert!(
        !result.crashed,
        "planted hang should be killed (timeout), not crash"
    );
}

fn watchdog_passes_clean_short_run() {
    let config = StressConfig {
        name: "clean-short-run".into(),
        seed: 0xBEEF,
        worker_count: 2,
        operation_count: 10_000,
        allocation_size_min: 16,
        allocation_size_max: 64,
    };

    let result = run_in_child_process(config, ScenarioType::AllocFree, 30);

    assert!(!result.timed_out, "clean short run should not time out");
    assert!(!result.crashed, "clean short run should not crash");
    assert!(
        result.ops_completed > 0,
        "clean run should complete some operations"
    );
}
