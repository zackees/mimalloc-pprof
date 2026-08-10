use std::io::Write;
use std::process::{Command, Stdio};
use stress_harness::{
    ExecutionMode, ScenarioType, StressChildRequest, StressConfig, CHILD_PROTOCOL_VERSION,
};

#[test]
fn dashboard_stress_child_accepts_one_request_and_emits_one_response() {
    let request = StressChildRequest {
        protocol_version: CHILD_PROTOCOL_VERSION,
        config: StressConfig {
            name: "dashboard-child-e2e".into(),
            seed: 42,
            worker_count: 2,
            operation_count: 10,
            allocation_size_min: 16,
            allocation_size_max: 32,
        },
        scenario: ScenarioType::AllocFree,
        execution_mode: ExecutionMode::Normal,
    };
    let mut child = Command::new(env!("CARGO_BIN_EXE_dashboard"))
        .arg("--stress-child")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn dashboard child");
    child
        .stdin
        .take()
        .expect("piped stdin")
        .write_all(serde_json::to_string(&request).unwrap().as_bytes())
        .expect("send request");
    let output = child.wait_with_output().expect("reap dashboard child");
    assert!(output.status.success());
    let result: stress_harness::StressResult =
        serde_json::from_slice(&output.stdout).expect("one strict JSON response");
    assert_eq!(result.ops_completed, 10);
    assert!(result.elapsed_secs.is_finite() && result.elapsed_secs > 0.0);
}

#[test]
fn dashboard_has_no_false_cross_thread_or_log_distribution_claim() {
    let source = include_str!("../src/main.rs");
    assert!(!source.contains("log-distributed"));
    assert!(!source.contains("id: \"cross-thread-free\","));
    assert!(source.contains("cross-thread-free-pending"));
}
