//! Reusable benchmark-child protocol entrypoint.

use std::io::{BufRead, BufReader, Read, Write};

use serde::Serialize;

use crate::adapter::LinkedAdapter;
use crate::execution::{
    execute_child_request, execute_child_request_with_observer, ExecutionResult,
    MeasurementObserver,
};
use crate::latency::{execute_latency_child_request, LatencyChildRequest};
use crate::memory::{read_control_record, write_control_record, ControlKind, ControlRecord};
use crate::model::BenchmarkChildRequest;
use crate::scaling::{execute_scaling_child_request, ScalingChildRequest};

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
        Some(argument) if argument == "--memory" && arguments.next().is_none() => {
            run_memory_measurement()
        }
        Some(argument) if argument == "--latency" && arguments.next().is_none() => {
            run_latency_measurement()
        }
        Some(argument) if argument == "--scaling" && arguments.next().is_none() => {
            run_scaling_measurement()
        }
        Some(_) => {
            Err("usage: benchmark-child [--adapter-smoke|--memory|--latency|--scaling]".into())
        }
    }
}

fn run_scaling_measurement() -> Result<(), String> {
    let adapter = LinkedAdapter::load().map_err(|error| error.to_string())?;
    let mut input = Vec::new();
    std::io::stdin()
        .take(1024 * 1024 + 1)
        .read_to_end(&mut input)
        .map_err(|error| format!("read scaling child request: {error}"))?;
    if input.len() > 1024 * 1024 {
        return Err("scaling child request exceeded 1 MiB".into());
    }
    let request: ScalingChildRequest = serde_json::from_slice(&input)
        .map_err(|error| format!("expected exactly one scaling child request: {error}"))?;
    let response = execute_scaling_child_request(&adapter, request)?;
    let output = serde_json::to_vec(&response)
        .map_err(|error| format!("serialize scaling child response: {error}"))?;
    std::io::stdout()
        .lock()
        .write_all(&output)
        .map_err(|error| format!("write scaling child response: {error}"))
}

fn run_latency_measurement() -> Result<(), String> {
    let adapter = LinkedAdapter::load().map_err(|error| error.to_string())?;
    let mut input = Vec::new();
    std::io::stdin()
        .take(1024 * 1024 + 1)
        .read_to_end(&mut input)
        .map_err(|error| format!("read latency child request: {error}"))?;
    if input.len() > 1024 * 1024 {
        return Err("latency child request exceeded 1 MiB".into());
    }
    let request: LatencyChildRequest = serde_json::from_slice(&input)
        .map_err(|error| format!("expected exactly one latency child request: {error}"))?;
    let response = execute_latency_child_request(&adapter, request)?;
    let output = serde_json::to_vec(&response)
        .map_err(|error| format!("serialize latency child response: {error}"))?;
    std::io::stdout()
        .lock()
        .write_all(&output)
        .map_err(|error| format!("write latency child response: {error}"))
}

struct MemoryChildObserver<'a, R: Read, W: Write> {
    reader: &'a mut R,
    writer: &'a mut W,
}

impl<R: Read, W: Write> MeasurementObserver for MemoryChildObserver<'_, R, W> {
    fn baseline_ready_and_wait_for_begin(&mut self) -> Result<(), String> {
        write_control_record(
            self.writer,
            ControlRecord::empty(ControlKind::BaselineReady),
        )?;
        let begin = read_control_record(self.reader)?;
        if begin != ControlRecord::empty(ControlKind::Begin) {
            return Err("memory child expected an empty begin message".into());
        }
        Ok(())
    }

    fn workload_active(&mut self) -> Result<(), String> {
        write_control_record(
            self.writer,
            ControlRecord::empty(ControlKind::WorkloadActive),
        )
    }

    fn workload_drained(&mut self, outcome: &ExecutionResult) -> Result<(), String> {
        write_control_record(
            self.writer,
            ControlRecord {
                kind: ControlKind::WorkloadDrained,
                current_live_requested_bytes: 0,
                peak_live_requested_bytes: outcome.peak_live_requested_bytes,
                checksum: outcome.checksum,
            },
        )
    }
}

fn run_memory_measurement() -> Result<(), String> {
    let adapter = LinkedAdapter::load().map_err(|error| error.to_string())?;
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut reader = BufReader::new(stdin.lock());
    let mut writer = stdout.lock();
    let mut input = Vec::with_capacity(16 * 1024);
    (&mut reader)
        .take(1024 * 1024 + 1)
        .read_until(b'\n', &mut input)
        .map_err(|error| format!("read memory child request: {error}"))?;
    if input.len() > 1024 * 1024 || !input.ends_with(b"\n") {
        return Err("memory child request must be one newline-terminated value below 1 MiB".into());
    }
    let request: BenchmarkChildRequest = serde_json::from_slice(&input)
        .map_err(|error| format!("expected exactly one memory child request: {error}"))?;
    let mut observer = MemoryChildObserver {
        reader: &mut reader,
        writer: &mut writer,
    };
    let response = execute_child_request_with_observer(&adapter, request, &mut observer)?;
    let exit = read_control_record(observer.reader)?;
    if exit != ControlRecord::empty(ControlKind::ExitResult) {
        return Err("memory child expected an empty exit-result request".into());
    }
    // Serialize only after the parent completed the five-second post-drain
    // window, so response allocation cannot perturb any reported RSS value.
    let output = serde_json::to_vec(&response)
        .map_err(|error| format!("serialize memory child response: {error}"))?;
    let length = u32::try_from(output.len())
        .map_err(|_| "memory child response exceeds the framing limit".to_string())?;
    if output.len() > 1024 * 1024 {
        return Err("memory child response exceeded 1 MiB".into());
    }
    write_control_record(
        observer.writer,
        ControlRecord::empty(ControlKind::ExitResult),
    )?;
    observer
        .writer
        .write_all(&length.to_le_bytes())
        .and_then(|()| observer.writer.write_all(&output))
        .and_then(|()| observer.writer.flush())
        .map_err(|error| format!("write memory child result: {error}"))
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
