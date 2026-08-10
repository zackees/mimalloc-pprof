//! Reusable benchmark-child protocol entrypoint.

use std::io::{Read, Write};

use serde::Serialize;

use crate::adapter::LinkedAdapter;
use crate::execution::execute_child_request;
use crate::model::BenchmarkChildRequest;

#[derive(Serialize)]
struct AdapterSmokeOutput<'a> {
    allocator_id: &'a str,
    allocator_version: &'a str,
    source_sha: &'a str,
    library_sha256: &'a str,
    child_binary_sha256: &'a str,
    checksum: u64,
    usable_size: usize,
}

/// Run the strict child protocol using process stdin/stdout. Exactly one JSON
/// request is accepted and exactly one JSON response is emitted.
pub fn benchmark_child_main() -> Result<(), String> {
    let mut arguments = std::env::args_os().skip(1);
    match arguments.next() {
        None => run_measurement(),
        Some(argument) if argument == "--adapter-smoke" && arguments.next().is_none() => {
            run_adapter_smoke()
        }
        Some(_) => Err("usage: benchmark-child [--adapter-smoke]".into()),
    }
}

fn run_measurement() -> Result<(), String> {
    let adapter = LinkedAdapter::load().map_err(|error| error.to_string())?;
    let mut input = Vec::new();
    std::io::stdin()
        .take(1024 * 1024 + 1)
        .read_to_end(&mut input)
        .map_err(|error| format!("read benchmark child request: {error}"))?;
    if input.len() > 1024 * 1024 {
        return Err("benchmark child request exceeded 1 MiB".into());
    }
    let request: BenchmarkChildRequest = serde_json::from_slice(&input)
        .map_err(|error| format!("expected exactly one benchmark child request: {error}"))?;
    let response = execute_child_request(&adapter, request)?;
    let output = serde_json::to_vec(&response)
        .map_err(|error| format!("serialize benchmark child response: {error}"))?;
    let mut stdout = std::io::stdout().lock();
    stdout
        .write_all(&output)
        .and_then(|()| stdout.write_all(b"\n"))
        .map_err(|error| format!("write benchmark child response: {error}"))
}

fn run_adapter_smoke() -> Result<(), String> {
    let child_binary_sha256 = std::env::var("BENCH_CHILD_BINARY_SHA256")
        .map_err(|_| "BENCH_CHILD_BINARY_SHA256 is required for adapter smoke".to_string())?;
    if child_binary_sha256.len() != 64
        || !child_binary_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("BENCH_CHILD_BINARY_SHA256 must be 64 lowercase hexadecimal bytes".into());
    }
    let adapter = LinkedAdapter::load().map_err(|error| error.to_string())?;
    let smoke = adapter.smoke_test().map_err(|error| error.to_string())?;
    let output = AdapterSmokeOutput {
        allocator_id: smoke.identity.allocator_id,
        allocator_version: smoke.identity.allocator_version,
        source_sha: smoke.identity.source_sha,
        library_sha256: smoke.identity.library_sha256,
        child_binary_sha256: &child_binary_sha256,
        checksum: smoke.checksum,
        usable_size: smoke.usable_size,
    };
    let encoded = serde_json::to_vec(&output)
        .map_err(|error| format!("serialize adapter smoke response: {error}"))?;
    let mut stdout = std::io::stdout().lock();
    stdout
        .write_all(&encoded)
        .and_then(|()| stdout.write_all(b"\n"))
        .map_err(|error| format!("write adapter smoke response: {error}"))
}
