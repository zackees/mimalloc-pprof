#[cfg(debug_assertions)]
use std::time::Instant;
use stress_harness::{run_scenario, ScenarioType, StressConfig};

fn tiny_config() -> StressConfig {
    StressConfig {
        name: "timing-contract".into(),
        seed: 1,
        worker_count: 1,
        operation_count: 1,
        allocation_size_min: 16,
        allocation_size_max: 16,
    }
}

// #295: `STRESS_HARNESS_TEST_SETUP_DELAY_MS` is a `#[cfg(debug_assertions)]` hook in
// src/lib.rs -- "never present in release builds", by design, so a release build has
// nothing to delay and the first assertion below ("test hook must delay setup") fails.
// The failure was never about the 50 ms tolerance: it is this test asserting a hook that
// its own build profile compiled out. It runs where the hook exists.
#[cfg(debug_assertions)]
#[test]
fn timed_interval_excludes_setup_delay() {
    std::env::set_var("STRESS_HARNESS_TEST_SETUP_DELAY_MS", "100");
    let outer_started = Instant::now();
    let result = run_scenario(tiny_config(), ScenarioType::AllocFree).expect("valid configuration");
    std::env::remove_var("STRESS_HARNESS_TEST_SETUP_DELAY_MS");

    assert!(
        outer_started.elapsed().as_millis() >= 100,
        "test hook must delay setup"
    );
    assert!(
        result.elapsed_secs < 0.050,
        "reported interval included pre-start setup: {}",
        result.elapsed_secs
    );
}

// #295: same -- `STRESS_HARNESS_TEST_PRE_PARK_DELAY_MS` is debug-only (src/lib.rs:282).
#[cfg(debug_assertions)]
#[test]
fn timed_interval_excludes_delay_between_ready_and_start_gate() {
    let mut config = tiny_config();
    config.worker_count = 2;
    config.operation_count = 2;
    std::env::set_var("STRESS_HARNESS_TEST_PRE_PARK_DELAY_MS", "100");
    let outer_started = Instant::now();
    let result = run_scenario(config, ScenarioType::AllocFree).expect("valid configuration");
    std::env::remove_var("STRESS_HARNESS_TEST_PRE_PARK_DELAY_MS");

    assert!(
        outer_started.elapsed().as_millis() >= 100,
        "test hook must delay pre-start parking"
    );
    assert!(
        result.elapsed_secs < 0.050,
        "reported interval included pre-start gate delay: {}",
        result.elapsed_secs
    );
}

// #295: no hook, so this one runs in every profile -- and release is where a fixed
// grace/poll floor would actually matter, since that is what the benchmarks measure with.
#[test]
fn timed_interval_has_no_fixed_grace_floor() {
    let result = run_scenario(tiny_config(), ScenarioType::AllocFree).expect("valid configuration");
    assert!(
        result.elapsed_secs < 0.004,
        "reported interval has a fixed grace/poll floor: {}",
        result.elapsed_secs
    );
}
