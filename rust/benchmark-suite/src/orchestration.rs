use std::ffi::OsString;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::model::{
    AllocatorIdentity, BenchmarkChildRequest, BenchmarkChildResponse, RawRun, RawSample,
};

pub const ALLOCATOR_IDS: [&str; 5] = [
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "bun-mimalloc",
    "mimalloc-pprof",
];

#[derive(Debug, Clone)]
pub struct ChildProgram {
    pub allocator: AllocatorIdentity,
    pub program: PathBuf,
    pub arguments: Vec<OsString>,
    pub toolchain: crate::model::ToolchainMetadata,
    /// Deliberate environment allowlist. The process starts from an empty
    /// environment; allocator runtime features are forced off below.
    pub environment: Vec<(OsString, OsString)>,
}

#[derive(Debug, Clone)]
pub struct CellRunPlan {
    pub request_template: BenchmarkChildRequest,
    pub children: Vec<ChildProgram>,
    pub blocks: u32,
    pub timeout: Duration,
    /// When present, persist every exact child request before launch so the
    /// raw record's reproduction command can replay it byte-for-byte.
    pub request_directory: Option<PathBuf>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CalibrationResult {
    pub transactions_per_worker: u64,
    pub operation_count: u64,
    pub elapsed_ns: u64,
}

/// Calibrate only against the pinned upstream-mimalloc child. The realized
/// per-worker count returned here is then frozen into every paired request.
pub fn calibrate_cell(
    upstream: &ChildProgram,
    request_template: &BenchmarkChildRequest,
    timeout: Duration,
) -> Result<CalibrationResult, String> {
    calibrate_cell_with(
        upstream,
        request_template,
        timeout,
        |child, request, timeout| run_child_sample(child, request, timeout),
    )
}

pub fn calibrate_cell_with<R>(
    upstream: &ChildProgram,
    request_template: &BenchmarkChildRequest,
    timeout: Duration,
    mut run_sample: R,
) -> Result<CalibrationResult, String>
where
    R: FnMut(&ChildProgram, &BenchmarkChildRequest, Duration) -> Result<RawSample, String>,
{
    if upstream.allocator.allocator_id != "upstream-mimalloc" {
        return Err("calibration must use the pinned upstream-mimalloc child".into());
    }
    if timeout < Duration::from_secs(2) {
        return Err("calibration timeout must permit the two-second upper bound".into());
    }
    let mut transactions = request_template.transactions_per_worker.max(1);
    let mut observations = Vec::new();
    for attempt in 0..20_u32 {
        let mut request = request_template.clone();
        request.block_id = u32::MAX;
        request.ordinal = 0;
        request.workload_seed = splitmix64(request.run_seed ^ 0xca11_ba7e ^ attempt as u64);
        request.transactions_per_worker = transactions;
        request.allocator = upstream.allocator.clone();
        request.toolchain = upstream.toolchain.clone();
        request.reproduction_command = reproduction_command(upstream, &request);
        let sample = run_sample(upstream, &request, timeout)?;
        observations.push(format!(
            "attempt={attempt},transactions={transactions},elapsed_ns={}",
            sample.elapsed_ns
        ));
        if (500_000_000..=2_000_000_000).contains(&sample.elapsed_ns) {
            return Ok(CalibrationResult {
                transactions_per_worker: transactions,
                operation_count: sample.operation_count,
                elapsed_ns: sample.elapsed_ns,
            });
        }
        let desired = (transactions as u128)
            .saturating_mul(1_000_000_000)
            .checked_div(sample.elapsed_ns.max(1) as u128)
            .unwrap_or(u128::from(u64::MAX))
            .clamp(1, u128::from(u64::MAX)) as u64;
        // Avoid one noisy observation producing a pathological jump while
        // still converging quickly from the one-transaction probe.
        let lower = (transactions / 16).max(1);
        let upper = transactions.saturating_mul(16).max(2);
        let next = desired.clamp(lower, upper);
        transactions = if next == transactions {
            if sample.elapsed_ns < 500_000_000 {
                transactions.saturating_add(1)
            } else {
                transactions.saturating_sub(1).max(1)
            }
        } else {
            next
        };
    }
    Err(format!(
        "upstream calibration did not reach the 0.5-2.0 second interval: {}",
        observations.join("; ")
    ))
}

/// Run one complete paired cell. A sink sees each successful sample
/// immediately (for append-only JSONL), but a `RawRun` is returned only after
/// every allocator in every block succeeds and validates.
pub fn run_balanced_cell<F>(plan: &CellRunPlan, mut append_sample: F) -> Result<RawRun, String>
where
    F: FnMut(&RawSample) -> Result<(), String>,
{
    run_balanced_cell_with(plan, &mut append_sample, |child, request, timeout| {
        run_child_sample(child, request, timeout)
    })
}

/// Injectable form used by contract tests and calibration harnesses. It keeps
/// the same no-retry/all-or-nothing state machine as the process-backed path.
pub fn run_balanced_cell_with<F, R>(
    plan: &CellRunPlan,
    mut append_sample: F,
    mut run_sample: R,
) -> Result<RawRun, String>
where
    F: FnMut(&RawSample) -> Result<(), String>,
    R: FnMut(&ChildProgram, &BenchmarkChildRequest, Duration) -> Result<RawSample, String>,
{
    match plan.request_template.run_kind.as_str() {
        "headline" if plan.blocks < 15 => {
            return Err("headline core cells require at least 15 complete blocks".into())
        }
        "reduced-smoke" if plan.blocks != 1 => {
            return Err("reduced smoke runs require exactly one non-headline block".into())
        }
        "headline" | "reduced-smoke" => {}
        _ => return Err("unknown benchmark run kind".into()),
    }
    if plan.timeout.is_zero() {
        return Err("child timeout must be nonzero".into());
    }
    if plan.children.len() != ALLOCATOR_IDS.len() {
        return Err("exactly five directly linked child programs are required".into());
    }
    for allocator in ALLOCATOR_IDS {
        let matches = plan
            .children
            .iter()
            .filter(|child| child.allocator.allocator_id == allocator)
            .count();
        if matches != 1 {
            return Err(format!("expected exactly one child for {allocator}"));
        }
    }
    let orders = balanced_block_orders(plan.blocks, plan.request_template.run_seed)?;
    validate_near_balanced(&orders)?;
    let mut samples = Vec::with_capacity(plan.blocks as usize * ALLOCATOR_IDS.len());
    for order in orders {
        for (ordinal, allocator_id) in order.allocator_ids.iter().enumerate() {
            let child = plan
                .children
                .iter()
                .find(|child| child.allocator.allocator_id == *allocator_id)
                .expect("child IDs validated above");
            let mut request = plan.request_template.clone();
            request.block_id = order.block_id;
            request.ordinal = ordinal as u8;
            request.workload_seed = order.workload_seed;
            request.allocator = child.allocator.clone();
            request.toolchain = child.toolchain.clone();
            let request_path = plan.request_directory.as_ref().map(|directory| {
                directory.join(format!(
                    "{}-{}-block-{:04}-ordinal-{}-{}.json",
                    request.scenario_id,
                    request.thread_point,
                    request.block_id,
                    request.ordinal,
                    request.allocator.allocator_id
                ))
            });
            request.reproduction_command = match &request_path {
                Some(path) => format!(
                    "MIMALLOC_PROF=0 MIMALLOC_MEMORY_EVENTS=0 {} < {}",
                    shell_quote(&child.program.display().to_string()),
                    shell_quote(&path.display().to_string())
                ),
                None => reproduction_command(child, &request),
            };
            if let Some(path) = request_path {
                let parent = path
                    .parent()
                    .ok_or_else(|| "request artifact has no parent directory".to_string())?;
                std::fs::create_dir_all(parent)
                    .map_err(|error| format!("create request artifact directory: {error}"))?;
                let mut file = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&path)
                    .map_err(|error| {
                        format!("create request artifact {}: {error}", path.display())
                    })?;
                serde_json::to_writer(&mut file, &request)
                    .map_err(|error| format!("write request artifact: {error}"))?;
                file.write_all(b"\n")
                    .map_err(|error| format!("finish request artifact: {error}"))?;
            }
            let sample = run_sample(child, &request, plan.timeout).map_err(|error| {
                format!(
                    "cell aborted at block {} ordinal {} allocator {}: {error}",
                    order.block_id, ordinal, allocator_id
                )
            })?;
            append_sample(&sample)?;
            samples.push(sample);
        }
    }
    let run = RawRun {
        schema_version: plan.request_template.schema_version.clone(),
        suite_version: plan.request_template.suite_version.clone(),
        run_kind: plan.request_template.run_kind.clone(),
        execution_mode: plan.request_template.execution_mode.clone(),
        run_seed: plan.request_template.run_seed,
        samples,
    };
    validate_complete_raw_run(&run, plan.blocks)?;
    Ok(run)
}

/// Spawn one fresh child with a strict watchdog and an empty ambient
/// environment. Success means exactly one matching JSON response and no
/// timeout/crash/nonzero exit; no failed outcome can become a raw sample.
pub fn run_child_sample(
    child: &ChildProgram,
    request: &BenchmarkChildRequest,
    timeout: Duration,
) -> Result<RawSample, String> {
    request.validate()?;
    if child.allocator != request.allocator {
        return Err("child program identity differs from request identity".into());
    }
    let encoded = serde_json::to_vec(request)
        .map_err(|error| format!("serialize benchmark child request: {error}"))?;
    let mut command = Command::new(&child.program);
    command
        .args(&child.arguments)
        .env_clear()
        .envs(child.environment.iter().map(|(key, value)| (key, value)))
        .env("MIMALLOC_PROF", "0")
        .env("MIMALLOC_MEMORY_EVENTS", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut process = command
        .spawn()
        .map_err(|error| format!("spawn benchmark child: {error}"))?;
    process
        .stdin
        .take()
        .ok_or_else(|| "benchmark child stdin was not piped".to_string())?
        .write_all(&encoded)
        .map_err(|error| format!("write benchmark child request: {error}"))?;

    let started = Instant::now();
    loop {
        match process
            .try_wait()
            .map_err(|error| format!("poll benchmark child: {error}"))?
        {
            Some(status) => {
                let output = process
                    .wait_with_output()
                    .map_err(|error| format!("collect benchmark child output: {error}"))?;
                if !status.success() {
                    return Err(format!(
                        "benchmark child exited unsuccessfully: status={status}, stderr={}",
                        String::from_utf8_lossy(&output.stderr)
                    ));
                }
                if output.stdout.len() > 1024 * 1024 {
                    return Err("benchmark child response exceeded 1 MiB".into());
                }
                if !output.stderr.is_empty() {
                    return Err(format!(
                        "successful benchmark child wrote stderr: {}",
                        String::from_utf8_lossy(&output.stderr)
                    ));
                }
                let response: BenchmarkChildResponse = serde_json::from_slice(&output.stdout)
                    .map_err(|error| {
                        format!("expected exactly one benchmark child response: {error}")
                    })?;
                response.validate_against(request)?;
                return Ok(response.sample);
            }
            None if started.elapsed() >= timeout => {
                let _ = process.kill();
                let _ = process.wait();
                return Err(format!(
                    "benchmark child timed out after {} ms",
                    timeout.as_millis()
                ));
            }
            None => std::thread::sleep(Duration::from_millis(5)),
        }
    }
}

fn reproduction_command(child: &ChildProgram, request: &BenchmarkChildRequest) -> String {
    format!(
        "MIMALLOC_PROF=0 MIMALLOC_MEMORY_EVENTS=0 {} # suite={} scenario={} threads={} transactions-per-worker={} seed={}",
        child.program.display(),
        request.suite_version,
        request.scenario_id,
        request.thread_point,
        request.transactions_per_worker,
        request.workload_seed
    )
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

/// A replayable, block-local order. Every allocator occurs once per block.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockOrder {
    pub block_id: u32,
    pub workload_seed: u64,
    pub allocator_ids: [String; 5],
}

/// Deterministically balances every ordinal position without selective retries.
pub fn balanced_block_orders(blocks: u32, run_seed: u64) -> Result<Vec<BlockOrder>, String> {
    if blocks == 0 {
        return Err("at least one complete block is required".into());
    }
    let mut base = ALLOCATOR_IDS.map(str::to_string);
    deterministic_shuffle(&mut base, run_seed);
    Ok((0..blocks)
        .map(|block_id| {
            let rotation = (block_id as usize) % ALLOCATOR_IDS.len();
            let allocator_ids = std::array::from_fn(|ordinal| {
                base[(ordinal + rotation) % ALLOCATOR_IDS.len()].clone()
            });
            BlockOrder {
                block_id,
                workload_seed: splitmix64(run_seed.wrapping_add(block_id as u64)),
                allocator_ids,
            }
        })
        .collect())
}

/// Check the `floor(blocks/5)`/`ceil(blocks/5)` ordinal requirement.
pub fn validate_near_balanced(orders: &[BlockOrder]) -> Result<(), String> {
    if orders.is_empty() {
        return Err("no blocks supplied".into());
    }
    let floor = orders.len() / ALLOCATOR_IDS.len();
    let ceiling = orders.len().div_ceil(ALLOCATOR_IDS.len());
    for ordinal in 0..ALLOCATOR_IDS.len() {
        for allocator in ALLOCATOR_IDS {
            let count = orders
                .iter()
                .filter(|order| order.allocator_ids[ordinal] == allocator)
                .count();
            if !(floor..=ceiling).contains(&count) {
                return Err(format!("{allocator} appears {count} times at ordinal {ordinal}, expected {floor}..={ceiling}"));
            }
        }
    }
    Ok(())
}

/// A calibrated count belongs to one scenario/thread cell and is immutable for
/// the complete five-allocator run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrozenCalibration {
    pub scenario_id: String,
    pub thread_count: u32,
    pub operation_count: u64,
}

impl FrozenCalibration {
    pub fn new(
        scenario_id: impl Into<String>,
        thread_count: u32,
        operation_count: u64,
    ) -> Result<Self, String> {
        let calibration = Self {
            scenario_id: scenario_id.into(),
            thread_count,
            operation_count,
        };
        if calibration.scenario_id.is_empty()
            || calibration.thread_count == 0
            || calibration.operation_count == 0
        {
            return Err(
                "calibration requires a scenario, non-zero thread count, and non-zero operations"
                    .into(),
            );
        }
        Ok(calibration)
    }

    pub fn apply_to(&self, allocator_id: &str) -> Result<u64, String> {
        if !ALLOCATOR_IDS.contains(&allocator_id) {
            return Err(format!("unknown allocator {allocator_id}"));
        }
        Ok(self.operation_count)
    }
}

/// Reject incomplete cells before any raw run can be handed to Phase 3.
pub fn validate_complete_raw_run(run: &RawRun, expected_blocks: u32) -> Result<(), String> {
    if run.run_kind == "headline" && expected_blocks < 15 {
        return Err("core runs require at least 15 blocks".into());
    }
    if run.run_kind == "reduced-smoke" && expected_blocks != 1 {
        return Err("reduced smoke raw runs require exactly one block".into());
    }
    if run.samples.len() != expected_blocks as usize * ALLOCATOR_IDS.len() {
        return Err("raw run does not contain exactly five samples per block".into());
    }
    for block in 0..expected_blocks {
        let samples: Vec<&RawSample> = run
            .samples
            .iter()
            .filter(|sample| sample.block_id == block)
            .collect();
        if samples.len() != ALLOCATOR_IDS.len() {
            return Err(format!("block {block} is incomplete"));
        }
        let mut seen_ordinals = [false; ALLOCATOR_IDS.len()];
        for sample in &samples {
            let ordinal = sample.ordinal as usize;
            if ordinal >= ALLOCATOR_IDS.len() || seen_ordinals[ordinal] {
                return Err(format!("block {block} has duplicate or invalid ordinals"));
            }
            seen_ordinals[ordinal] = true;
        }
        for allocator in ALLOCATOR_IDS {
            let sample = samples
                .iter()
                .find(|sample| sample.allocator_id == allocator)
                .ok_or_else(|| format!("block {block} is missing {allocator}"))?;
            sample.validate()?;
        }
        if samples
            .iter()
            .any(|sample| sample.workload_seed != samples[0].workload_seed)
        {
            return Err(format!("block {block} does not share one workload seed"));
        }
        if samples.iter().any(|sample| {
            sample.run_seed != run.run_seed
                || sample.schema_version != run.schema_version
                || sample.suite_version != run.suite_version
                || sample.run_kind != run.run_kind
                || sample.execution_mode != run.execution_mode
                || sample.scenario_id != samples[0].scenario_id
                || sample.scenario_version != samples[0].scenario_version
                || sample.thread_point != samples[0].thread_point
                || sample.thread_count != samples[0].thread_count
                || sample.operation_count != samples[0].operation_count
                || sample.requested_transactions != samples[0].requested_transactions
                || sample.checksum != samples[0].checksum
        }) {
            return Err(format!(
                "block {block} does not share one frozen workload contract"
            ));
        }
    }
    Ok(())
}

/// Contract check for the planted mutex-serialized control. It is kept out of
/// headline output and exists only to prove the complete five-child/block path
/// remains sensitive to an intentionally slower execution mode.
pub fn detect_planted_serialized_control(
    normal: &RawRun,
    serialized: &RawRun,
) -> Result<f64, String> {
    if normal.execution_mode != "normal" || serialized.execution_mode != "serialized-control" {
        return Err("control comparison requires normal and serialized-control runs".into());
    }
    if normal.samples.len() != serialized.samples.len() || normal.samples.is_empty() {
        return Err("control comparison requires equally complete raw runs".into());
    }
    let mut normal_ns = 0_u128;
    let mut serialized_ns = 0_u128;
    let mut paired = 0_u64;
    for sample in normal
        .samples
        .iter()
        .filter(|sample| sample.thread_count > 1)
    {
        let control = serialized.samples.iter().find(|candidate| {
            candidate.block_id == sample.block_id
                && candidate.ordinal == sample.ordinal
                && candidate.allocator_id == sample.allocator_id
                && candidate.scenario_id == sample.scenario_id
                && candidate.thread_point == sample.thread_point
                && candidate.workload_seed == sample.workload_seed
                && candidate.operation_count == sample.operation_count
                && candidate.checksum == sample.checksum
        });
        let control =
            control.ok_or_else(|| "serialized control is missing a paired sample".to_string())?;
        normal_ns += u128::from(sample.elapsed_ns);
        serialized_ns += u128::from(control.elapsed_ns);
        paired += 1;
    }
    if paired == 0 {
        return Err("serialized control needs at least one multi-thread paired sample".into());
    }
    let ratio = serialized_ns as f64 / normal_ns.max(1) as f64;
    if !ratio.is_finite() || ratio < 1.05 {
        return Err(format!(
            "planted serialized control was not detected (elapsed ratio {ratio:.3})"
        ));
    }
    Ok(ratio)
}

fn deterministic_shuffle(values: &mut [String; 5], mut state: u64) {
    for index in (1..values.len()).rev() {
        state = splitmix64(state);
        values.swap(index, (state as usize) % (index + 1));
    }
}

fn splitmix64(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    let mut z = value;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}
