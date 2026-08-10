//! Test/development stress-child protocol adapter.
//!
//! Production calls use `dashboard --stress-child`; this tiny binary lets the
//! harness integration tests exercise the same strict stdin/stdout protocol.

fn main() {
    if let Some(mode) = std::env::var_os("BENCH_HARNESS_TEST_RESPONSE") {
        match mode.to_string_lossy().as_ref() {
            "partial" => {
                print!("{{");
                return;
            }
            "nonfinite" => {
                print!("{{\"elapsed_secs\":1e999}}");
                return;
            }
            "extra" => {
                print!("{{}}\n{{}}");
                return;
            }
            "zero" | "zero-elapsed" | "checksum-mismatch" | "timeout-report" | "crash-report" => {
                let mut input = String::new();
                use std::io::Read;
                std::io::stdin()
                    .read_to_string(&mut input)
                    .expect("read request");
                let request: stress_harness::StressChildRequest =
                    serde_json::from_str(&input).expect("parse request");
                let mut result = stress_harness::run_child_request(request).expect("run request");
                match mode.to_string_lossy().as_ref() {
                    "zero" => result.ops_completed = 0,
                    "zero-elapsed" => result.elapsed_secs = 0.0,
                    "checksum-mismatch" => {
                        result.observed_checksum = result.expected_checksum.wrapping_add(1)
                    }
                    "timeout-report" => result.timed_out = true,
                    "crash-report" => result.crashed = true,
                    _ => unreachable!(),
                }
                print!(
                    "{}",
                    serde_json::to_string(&result).expect("serialize result")
                );
                return;
            }
            _ => {}
        }
    }
    if let Err(error) = stress_harness::run_stdio_child() {
        eprintln!("stress-child rejected request: {error}");
        std::process::exit(1);
    }
}
