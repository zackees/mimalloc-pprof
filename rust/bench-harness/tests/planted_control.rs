//! Planted-slower control — proves the benchmark harness can statistically
//! distinguish a normal concurrent run from a deliberately serialised one.

use bench_harness::{is_significantly_slower, run_benchmark, BenchConfig, ScenarioType};
use mimalloc_pprof::MiMalloc;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

#[test]
fn planted_serialized_control_is_rejected() {
    let base_config = BenchConfig {
        name: "planted-control".into(),
        seed: 0xDECAF,
        worker_count: 4,
        operation_count: 500_000,
        allocation_size_min: 16,
        allocation_size_max: 512,
        warmup_rounds: 2,
        measurement_rounds: 7,
        max_duration_secs: Some(60),
        scenario: ScenarioType::AllocFree,
        serialized: false,
    };

    // Run the normal (unserialised) benchmark.
    let normal = run_benchmark(base_config.clone());

    // Run the planted-slower (serialised) benchmark.
    let mut serial_cfg = base_config.clone();
    serial_cfg.serialized = true;
    serial_cfg.name = "planted-control-serialized".into();
    let serialized = run_benchmark(serial_cfg);

    // The serialised run must be significantly slower (α = 0.05).
    let significantly_slower = is_significantly_slower(&normal, &serialized, 0.05);
    let ratio = serialized.summary.mean_throughput_ops_per_sec
        / normal.summary.mean_throughput_ops_per_sec;

    println!(
        "Planted control: normal={:.0} ops/s, serialized={:.0} ops/s, ratio={:.3}, \
         significantly_slower={significantly_slower}",
        normal.summary.mean_throughput_ops_per_sec,
        serialized.summary.mean_throughput_ops_per_sec,
        ratio,
    );

    // Primary check: serialized throughput must be meaningfully lower.
    // In debug mode the ratio is ~0.2 (5x slowdown); in release mode ~0.7 (1.4x).
    assert!(
        ratio < 0.85,
        "serialized throughput not meaningfully slower: ratio={ratio:.3} \
         (normal={:.0} ops/s, serialized={:.0} ops/s)",
        normal.summary.mean_throughput_ops_per_sec,
        serialized.summary.mean_throughput_ops_per_sec,
    );

    // Secondary check: the t-test should confirm the difference.
    // With 7 measurement rounds we have enough statistical power.
    assert!(
        significantly_slower,
        "planted control FAILED: statistical test did not reject serialised \
         run at α=0.05.\n  normal  throughput = {:.0} ops/s\n  serial  throughput = {:.0} ops/s\n  ratio = {:.3}",
        normal.summary.mean_throughput_ops_per_sec,
        serialized.summary.mean_throughput_ops_per_sec,
        ratio,
    );
}
