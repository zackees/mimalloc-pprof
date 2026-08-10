//! Throughput benchmark — warmup + measurement rounds, machine-readable
//! output, and build/host metadata recording.

use bench_harness::{run_benchmark, BenchConfig, ChildProgram, ScenarioType};
use mimalloc_pprof::MiMalloc;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

fn stress_child() -> ChildProgram {
    ChildProgram {
        program: env!("CARGO_BIN_EXE_stress-child").into(),
        arguments: Vec::new(),
        environment: Vec::new(),
    }
}

#[test]
fn throughput_benchmark_with_metadata() {
    let config = BenchConfig {
        name: "throughput-small-fixed".into(),
        seed: 0xC0FFEE,
        worker_count: 4,
        operation_count: 200_000,
        allocation_size_min: 16,
        allocation_size_max: 64,
        warmup_rounds: 1,
        measurement_rounds: 3,
        max_duration_secs: Some(30),
        scenario: ScenarioType::AllocFree,
        serialized: false,
    };

    let result = run_benchmark(config, &stress_child()).expect("valid benchmark configuration");

    // Verify metadata was captured.
    assert!(
        !result.metadata.commit.is_empty(),
        "commit should be recorded"
    );
    assert!(
        result.metadata.rustc.contains("rustc"),
        "rustc version should be recorded, got: {}",
        result.metadata.rustc
    );
    assert!(
        !result.metadata.cpu_model.is_empty(),
        "cpu model should be recorded"
    );
    assert!(!result.metadata.os.is_empty(), "os should be recorded");

    // Verify measurement rounds.
    assert_eq!(result.samples.len(), 3, "should have 3 measurement rounds");
    assert_eq!(result.summary.rounds, 3);

    // Verify throughput is reasonable (positive, finite).
    assert!(
        result.summary.mean_throughput_ops_per_sec > 0.0,
        "throughput should be positive"
    );
    assert!(
        result.summary.mean_throughput_ops_per_sec.is_finite(),
        "throughput should be finite"
    );

    // CV should be reasonable.  Release-mode runs on hosted CI runners
    // can have high relative variance because the total wall-clock time per
    // round is tiny (sub-100 ms).  Accept up to 100 % CV.
    assert!(
        result.summary.cv_throughput_percent < 100.0,
        "CV too high: {:.1}%",
        result.summary.cv_throughput_percent
    );

    // Emit machine-readable result.
    let json = serde_json::to_string_pretty(&result).unwrap();
    println!("BENCH_RESULT: {json}");
}
