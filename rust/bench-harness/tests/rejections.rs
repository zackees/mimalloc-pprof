use bench_harness::{run_benchmark, BenchConfig, BenchError, ChildProgram, ScenarioType};
use std::ffi::OsString;

fn config() -> BenchConfig {
    BenchConfig {
        name: "rejection-control".into(),
        seed: 7,
        worker_count: 2,
        operation_count: 10,
        allocation_size_min: 16,
        allocation_size_max: 32,
        warmup_rounds: 0,
        measurement_rounds: 2,
        max_duration_secs: Some(10),
        scenario: ScenarioType::AllocFree,
        serialized: false,
    }
}

fn child(response: Option<&str>) -> ChildProgram {
    let mut environment = Vec::new();
    if let Some(response) = response {
        environment.push((
            OsString::from("BENCH_HARNESS_TEST_RESPONSE"),
            response.into(),
        ));
    }
    ChildProgram {
        program: env!("CARGO_BIN_EXE_stress-child").into(),
        arguments: Vec::new(),
        environment,
    }
}

#[test]
fn benchmark_rejects_zero_measurement_rounds() {
    let mut invalid = config();
    invalid.measurement_rounds = 0;
    assert!(matches!(
        run_benchmark(invalid, &child(None)),
        Err(BenchError::InvalidConfig(_))
    ));
}

#[test]
fn benchmark_rejects_partial_or_extra_child_output() {
    assert!(matches!(
        run_benchmark(config(), &child(Some("partial"))),
        Err(BenchError::InvalidChildResponse(_))
    ));
    assert!(matches!(
        run_benchmark(config(), &child(Some("extra"))),
        Err(BenchError::InvalidChildResponse(_))
    ));
    assert!(matches!(
        run_benchmark(config(), &child(Some("nonfinite"))),
        Err(BenchError::InvalidChildResponse(_))
    ));
}

#[test]
fn benchmark_rejects_zero_completed_operations() {
    assert!(matches!(
        run_benchmark(config(), &child(Some("zero"))),
        Err(BenchError::InvalidSample(_))
    ));
}

#[test]
fn benchmark_rejects_zero_elapsed_or_checksum_mismatch() {
    assert!(matches!(
        run_benchmark(config(), &child(Some("zero-elapsed"))),
        Err(BenchError::InvalidSample(_))
    ));
    assert!(matches!(
        run_benchmark(config(), &child(Some("checksum-mismatch"))),
        Err(BenchError::InvalidSample(_))
    ));
}

#[test]
fn benchmark_rejects_reported_timeout_or_crash() {
    assert!(matches!(
        run_benchmark(config(), &child(Some("timeout-report"))),
        Err(BenchError::InvalidSample(_))
    ));
    assert!(matches!(
        run_benchmark(config(), &child(Some("crash-report"))),
        Err(BenchError::InvalidSample(_))
    ));
}

#[test]
fn benchmark_kills_and_reaps_hanging_child() {
    let mut hanging = config();
    hanging.scenario = ScenarioType::Hang;
    hanging.worker_count = 1;
    hanging.operation_count = 1;
    hanging.measurement_rounds = 1;
    hanging.max_duration_secs = Some(1);
    assert!(matches!(
        run_benchmark(hanging, &child(None)),
        Err(BenchError::Timeout { seconds: 1 })
    ));
    assert!(
        run_benchmark(config(), &child(None)).is_ok(),
        "a killed child must not leak work into the next sample"
    );
}

#[test]
fn successful_result_preserves_every_raw_sample() {
    let result = run_benchmark(config(), &child(None)).expect("valid child samples");
    assert_eq!(result.samples.len(), 2);
    assert!(result
        .samples
        .iter()
        .all(|sample| sample.ops_completed == 10));
}
