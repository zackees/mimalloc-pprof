//! Linux process-memory metric and external `/proc` sampling protocol.
//!
//! The measured child owns allocator work and the requested-live-byte oracle.
//! The parent owns every kernel read so sampler allocations never enter the
//! allocator under measurement.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::model::{
    AbsoluteCellSummary, AllocatorBuildIdentity, AllocatorIdentity, BenchmarkChildRequest,
    BenchmarkChildResponse, CellCalibration, LatestReport, PairedCellSummary, PublicationRunner,
    RawSample, RunIdentity, RunnerMetadata, ToolchainMetadata, CHILD_PROTOCOL_VERSION,
};
use crate::orchestration::ChildProgram;
use crate::provenance::sha256_bytes;
use crate::scenarios::{CardId, ThreadPoint, Topology};
use crate::stats::{summarize_absolute, summarize_paired, MetricDirection, MetricObservation};

pub const MEMORY_SCHEMA_VERSION: &str = "linux-process-memory-v1";
pub const MEMORY_CONTROL_VERSION: &str = "memory-control-v1";
pub const MEMORY_SAMPLE_TARGET_NS: u64 = 5_000_000;
pub const MEMORY_MAX_SAMPLE_GAP_NS: u64 = 100_000_000;
pub const MEMORY_MIN_BLOCKS: u32 = 15;
pub const CONTROL_RECORD_BYTES: usize = 32;
const CONTROL_MAGIC: &[u8; 4] = b"MPM1";

const REFERENCE_ALLOCATOR: &str = "upstream-mimalloc";
const ALLOCATOR_IDS: [&str; 4] = [
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "mimalloc-pprof",
];
const METRICS: [(&str, &str); 5] = [
    ("sampled-peak-rss-bytes", "bytes"),
    ("post-drain-rss-100ms-bytes", "bytes"),
    ("post-drain-rss-1s-bytes", "bytes"),
    ("post-drain-rss-5s-bytes", "bytes"),
    ("fragmentation-proxy", "ratio"),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum ControlKind {
    BaselineReady = 1,
    Begin = 2,
    WorkloadActive = 3,
    WorkloadDrained = 4,
    ExitResult = 5,
}

impl ControlKind {
    pub const ALL: [Self; 5] = [
        Self::BaselineReady,
        Self::Begin,
        Self::WorkloadActive,
        Self::WorkloadDrained,
        Self::ExitResult,
    ];

    pub fn from_byte(value: u8) -> Result<Self, String> {
        Self::ALL
            .into_iter()
            .find(|candidate| *candidate as u8 == value)
            .ok_or_else(|| format!("unknown memory control message {value}"))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ControlRecord {
    pub kind: ControlKind,
    pub current_live_requested_bytes: u64,
    pub peak_live_requested_bytes: u64,
    pub checksum: u64,
}

impl ControlRecord {
    pub const fn empty(kind: ControlKind) -> Self {
        Self {
            kind,
            current_live_requested_bytes: 0,
            peak_live_requested_bytes: 0,
            checksum: 0,
        }
    }

    pub fn encode(self) -> [u8; CONTROL_RECORD_BYTES] {
        let mut output = [0_u8; CONTROL_RECORD_BYTES];
        output[..4].copy_from_slice(CONTROL_MAGIC);
        output[4] = self.kind as u8;
        output[8..16].copy_from_slice(&self.current_live_requested_bytes.to_le_bytes());
        output[16..24].copy_from_slice(&self.peak_live_requested_bytes.to_le_bytes());
        output[24..32].copy_from_slice(&self.checksum.to_le_bytes());
        output
    }

    pub fn decode(input: [u8; CONTROL_RECORD_BYTES]) -> Result<Self, String> {
        if &input[..4] != CONTROL_MAGIC || input[5..8] != [0, 0, 0] {
            return Err("invalid memory control record header".into());
        }
        let read_u64 = |range: std::ops::Range<usize>| {
            let mut bytes = [0_u8; 8];
            bytes.copy_from_slice(&input[range]);
            u64::from_le_bytes(bytes)
        };
        Ok(Self {
            kind: ControlKind::from_byte(input[4])?,
            current_live_requested_bytes: read_u64(8..16),
            peak_live_requested_bytes: read_u64(16..24),
            checksum: read_u64(24..32),
        })
    }
}

pub fn write_control_record(writer: &mut impl Write, record: ControlRecord) -> Result<(), String> {
    writer
        .write_all(&record.encode())
        .and_then(|()| writer.flush())
        .map_err(|error| format!("write memory control record: {error}"))
}

pub fn read_control_record(reader: &mut impl Read) -> Result<ControlRecord, String> {
    let mut bytes = [0_u8; CONTROL_RECORD_BYTES];
    reader
        .read_exact(&mut bytes)
        .map_err(|error| format!("read memory control record: {error}"))?;
    ControlRecord::decode(bytes)
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct LiveRequestedOracle {
    current: u64,
    peak: u64,
}

impl LiveRequestedOracle {
    pub fn allocate(&mut self, bytes: u64) -> Result<(), String> {
        if bytes == 0 {
            return Err("requested-live oracle cannot add zero bytes".into());
        }
        self.current = self
            .current
            .checked_add(bytes)
            .ok_or_else(|| "requested-live oracle overflowed".to_string())?;
        self.peak = self.peak.max(self.current);
        Ok(())
    }

    pub fn free(&mut self, bytes: u64) -> Result<(), String> {
        if bytes == 0 || bytes > self.current {
            return Err("requested-live oracle underflowed".into());
        }
        self.current -= bytes;
        Ok(())
    }

    pub const fn current_bytes(self) -> u64 {
        self.current
    }

    pub const fn peak_bytes(self) -> u64 {
        self.peak
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MemoryTimelinePoint {
    /// Nanoseconds from the parent-side process spawn timestamp.
    pub elapsed_ns: u64,
    pub rss_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SamplingIntervalDistribution {
    pub target_interval_ns: u64,
    pub sample_count: u64,
    pub minimum_interval_ns: u64,
    pub median_interval_ns: u64,
    pub p95_interval_ns: u64,
    pub maximum_interval_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MemoryEnvironment {
    pub page_size_bytes: u64,
    pub kernel: String,
    pub transparent_hugepage: String,
    /// Exact cgroup v2 `memory.max` text (`max` means unlimited).
    pub cgroup_memory_max: String,
    /// Exact cgroup v2 `memory.high` text (`max` means unlimited).
    pub cgroup_memory_high: String,
    pub hosted_runner: bool,
    pub purge_policy: String,
    pub allocator_runtime_options: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MemoryCompatibility {
    pub metric_schema_version: String,
    pub page_size_bytes: u64,
    pub kernel: String,
    pub sampling_target_interval_ns: u64,
    pub purge_policy: String,
    pub transparent_hugepage: String,
    pub cgroup_memory_max: String,
    pub cgroup_memory_high: String,
    pub allocator_runtime_options: BTreeMap<String, String>,
}

impl MemoryEnvironment {
    pub fn compatibility(&self, target_interval_ns: u64) -> MemoryCompatibility {
        MemoryCompatibility {
            metric_schema_version: MEMORY_SCHEMA_VERSION.into(),
            page_size_bytes: self.page_size_bytes,
            kernel: self.kernel.clone(),
            sampling_target_interval_ns: target_interval_ns,
            purge_policy: self.purge_policy.clone(),
            transparent_hugepage: self.transparent_hugepage.clone(),
            cgroup_memory_max: self.cgroup_memory_max.clone(),
            cgroup_memory_high: self.cgroup_memory_high.clone(),
            allocator_runtime_options: self.allocator_runtime_options.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MemoryRawSample {
    pub metric_schema_version: String,
    pub block_id: u32,
    pub ordinal: u8,
    pub workload_seed: u64,
    pub allocator_id: String,
    pub allocator_source_sha: String,
    pub child_binary_sha256: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub baseline_ready_ns: u64,
    pub workload_active_ns: u64,
    pub workload_drained_ns: u64,
    pub post_drain_sample_100ms_ns: u64,
    pub post_drain_sample_1s_ns: u64,
    pub post_drain_sample_5s_ns: u64,
    pub sampler_pid: u32,
    pub sampled_pid: u32,
    pub baseline_rss_bytes: u64,
    pub baseline_hwm_bytes: u64,
    pub sampled_peak_rss_bytes: u64,
    pub kernel_peak_hwm_bytes: u64,
    pub peak_live_requested_bytes: u64,
    pub post_drain_rss_100ms_bytes: u64,
    pub post_drain_rss_1s_bytes: u64,
    pub post_drain_rss_5s_bytes: u64,
    pub sampled_peak_rss_delta_bytes: i64,
    pub post_drain_rss_delta_100ms_bytes: i64,
    pub post_drain_rss_delta_1s_bytes: i64,
    pub post_drain_rss_delta_5s_bytes: i64,
    pub fragmentation_proxy: f64,
    pub hwm_discrepancy: bool,
    pub hwm_tolerance_bytes: u64,
    pub sampling: SamplingIntervalDistribution,
    pub timeline: Vec<MemoryTimelinePoint>,
    pub environment: MemoryEnvironment,
    /// The child-owned workload/count/checksum oracle result.
    pub child_sample: RawSample,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MemoryRawRun {
    pub metric_schema_version: String,
    pub status: String,
    pub run_seed: u64,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub allocator_lock_sha256: String,
    pub allocators: Vec<AllocatorBuildIdentity>,
    pub calibrations: Vec<CellCalibration>,
    pub samples: Vec<MemoryRawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MemoryMetricReport {
    pub metric_schema_version: String,
    pub status: String,
    pub invalid_reason: Option<String>,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub sampling_target_interval_ns: u64,
    pub purge_policy: String,
    pub units: BTreeMap<String, String>,
    pub direction: MetricDirection,
    pub informational: bool,
    pub methodology: MemoryMethodology,
    pub absolute_summaries: Vec<AbsoluteCellSummary>,
    pub paired_summaries: Vec<PairedCellSummary>,
    pub raw_samples: Vec<MemoryRawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MemoryMethodology {
    pub rss_source: String,
    pub hwm_source: String,
    pub baseline_definition: String,
    pub sampled_peak_definition: String,
    pub post_drain_definition: String,
    pub fragmentation_formula: String,
    pub hwm_discrepancy_tolerance: String,
    pub page_touch_contract: String,
    pub purge_policy: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MemoryHistoryReport {
    pub metric_schema_version: String,
    pub status: String,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner_fingerprint_sha256: String,
    pub sampling_target_interval_ns: u64,
    pub purge_policy: String,
    pub units: BTreeMap<String, String>,
    pub direction: MetricDirection,
    pub informational: bool,
    pub methodology: MemoryMethodology,
    pub absolute_summaries: Vec<AbsoluteCellSummary>,
    pub paired_summaries: Vec<PairedCellSummary>,
}

impl MemoryMetricReport {
    pub fn history_projection(&self) -> MemoryHistoryReport {
        MemoryHistoryReport {
            metric_schema_version: self.metric_schema_version.clone(),
            status: self.status.clone(),
            metric_comparison_key: self.metric_comparison_key.clone(),
            run: self.run.clone(),
            runner_fingerprint_sha256: self.runner.fingerprint_sha256.clone(),
            sampling_target_interval_ns: self.sampling_target_interval_ns,
            purge_policy: self.purge_policy.clone(),
            units: self.units.clone(),
            direction: self.direction,
            informational: self.informational,
            methodology: self.methodology.clone(),
            absolute_summaries: self.absolute_summaries.clone(),
            paired_summaries: self.paired_summaries.clone(),
        }
    }
}

pub fn parse_smaps_rollup(input: &str) -> Result<u64, String> {
    parse_unique_proc_kb(input, "Rss")
}

pub fn parse_status_hwm(input: &str) -> Result<u64, String> {
    parse_unique_proc_kb(input, "VmHWM")
}

fn parse_unique_proc_kb(input: &str, field: &str) -> Result<u64, String> {
    let prefix = format!("{field}:");
    let matches = input
        .lines()
        .filter_map(|line| line.strip_prefix(&prefix))
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        return Err(format!("proc field {field} must occur exactly once"));
    }
    let parts = matches[0].split_ascii_whitespace().collect::<Vec<_>>();
    if parts.len() != 2 || parts[1] != "kB" {
        return Err(format!("proc field {field} must use an integer kB value"));
    }
    let kibibytes = parts[0]
        .parse::<u64>()
        .map_err(|_| format!("proc field {field} is not an unsigned integer"))?;
    kibibytes
        .checked_mul(1024)
        .ok_or_else(|| format!("proc field {field} overflows bytes"))
}

pub fn signed_delta(value: u64, baseline: u64) -> Result<i64, String> {
    let delta = i128::from(value) - i128::from(baseline);
    i64::try_from(delta).map_err(|_| "memory delta exceeds signed 64-bit range".into())
}

pub fn fragmentation_proxy(delta_bytes: i64, peak_live_requested: u64) -> Result<f64, String> {
    if delta_bytes <= 0 || peak_live_requested == 0 {
        return Err(
            "fragmentation proxy needs positive RSS delta and live-byte denominator".into(),
        );
    }
    let ratio = delta_bytes as f64 / peak_live_requested as f64;
    if !ratio.is_finite() || ratio <= 0.0 {
        return Err("fragmentation proxy is non-finite or non-positive".into());
    }
    Ok(ratio)
}

pub fn validate_sampler_ownership(parent_pid: u32, sampled_pid: u32) -> Result<(), String> {
    if parent_pid == 0 || sampled_pid == 0 || parent_pid == sampled_pid {
        return Err("memory sampler must run in the parent and target a distinct child PID".into());
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProcSnapshot {
    pub rss_bytes: u64,
    pub hwm_bytes: u64,
}

pub fn read_proc_snapshot(pid: u32) -> Result<ProcSnapshot, String> {
    if pid == 0 {
        return Err("cannot sample PID zero".into());
    }
    let root = PathBuf::from("/proc").join(pid.to_string());
    let smaps_path = root.join("smaps_rollup");
    let status_path = root.join("status");
    let smaps = std::fs::read_to_string(&smaps_path)
        .map_err(|error| format!("{}: {error}", smaps_path.display()))?;
    let status = std::fs::read_to_string(&status_path)
        .map_err(|error| format!("{}: {error}", status_path.display()))?;
    Ok(ProcSnapshot {
        rss_bytes: parse_smaps_rollup(&smaps)?,
        hwm_bytes: parse_status_hwm(&status)?,
    })
}

pub fn collect_memory_environment(kernel: &str) -> Result<MemoryEnvironment, String> {
    if kernel.is_empty() {
        return Err("memory environment needs a kernel identity".into());
    }
    let output = Command::new("getconf")
        .arg("PAGESIZE")
        .output()
        .map_err(|error| format!("run getconf PAGESIZE: {error}"))?;
    if !output.status.success() || !output.stderr.is_empty() {
        return Err("getconf PAGESIZE did not complete cleanly".into());
    }
    let page_size_bytes = std::str::from_utf8(&output.stdout)
        .map_err(|error| format!("getconf PAGESIZE was not UTF-8: {error}"))?
        .trim()
        .parse::<u64>()
        .map_err(|_| "getconf PAGESIZE was not an unsigned integer".to_string())?;
    if page_size_bytes == 0 || !page_size_bytes.is_power_of_two() {
        return Err("getconf PAGESIZE returned an invalid page size".into());
    }
    let transparent_hugepage =
        read_trimmed_or_not_observable(Path::new("/sys/kernel/mm/transparent_hugepage/enabled"));
    let cgroup_root = resolve_cgroup_v2_root()?;
    let cgroup_memory_max = read_trimmed_or_not_observable(&cgroup_root.join("memory.max"));
    let cgroup_memory_high = read_trimmed_or_not_observable(&cgroup_root.join("memory.high"));
    Ok(MemoryEnvironment {
        page_size_bytes,
        kernel: kernel.into(),
        transparent_hugepage,
        cgroup_memory_max,
        cgroup_memory_high,
        hosted_runner: std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true"),
        purge_policy: "natural-only".into(),
        allocator_runtime_options: BTreeMap::from([
            ("MIMALLOC_MEMORY_EVENTS".into(), "0".into()),
            ("MIMALLOC_PROF".into(), "0".into()),
        ]),
    })
}

fn read_trimmed_or_not_observable(path: &Path) -> String {
    std::fs::read_to_string(path)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "not-observable".into())
}

fn resolve_cgroup_v2_root() -> Result<PathBuf, String> {
    let cgroup = std::fs::read_to_string("/proc/self/cgroup")
        .map_err(|error| format!("read /proc/self/cgroup: {error}"))?;
    let paths = cgroup
        .lines()
        .filter_map(|line| line.strip_prefix("0::"))
        .collect::<Vec<_>>();
    if paths.len() != 1 {
        return Ok(PathBuf::from("/sys/fs/cgroup"));
    }
    let relative = paths[0].trim_start_matches('/');
    if relative.split('/').any(|component| component == "..") {
        return Err("cgroup v2 path contains traversal".into());
    }
    Ok(PathBuf::from("/sys/fs/cgroup").join(relative))
}

enum ReaderEvent {
    Control(ControlRecord, u64),
    Result(Box<BenchmarkChildResponse>),
    Error(String),
}

fn elapsed_ns(started: Instant) -> u64 {
    started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64
}

fn remaining(deadline: Instant) -> Duration {
    deadline.saturating_duration_since(Instant::now())
}

fn receive_control(
    receiver: &mpsc::Receiver<ReaderEvent>,
    expected: ControlKind,
    timeout: Duration,
) -> Result<(ControlRecord, u64), String> {
    match receiver.recv_timeout(timeout) {
        Ok(ReaderEvent::Control(record, observed_ns)) if record.kind == expected => {
            Ok((record, observed_ns))
        }
        Ok(ReaderEvent::Control(record, _)) => Err(format!(
            "expected memory control {:?}, received {:?}",
            expected, record.kind
        )),
        Ok(ReaderEvent::Result(_)) => Err("memory child returned a result before exit".into()),
        Ok(ReaderEvent::Error(error)) => Err(error),
        Err(error) => Err(format!("wait for memory control {:?}: {error}", expected)),
    }
}

fn read_memory_child_events(
    mut stdout: impl Read,
    sender: mpsc::Sender<ReaderEvent>,
    started: Instant,
) {
    let result = (|| -> Result<(), String> {
        for expected in [
            ControlKind::BaselineReady,
            ControlKind::WorkloadActive,
            ControlKind::WorkloadDrained,
            ControlKind::ExitResult,
        ] {
            let record = read_control_record(&mut stdout)?;
            if record.kind != expected {
                return Err(format!(
                    "memory child control order violation: expected {:?}, got {:?}",
                    expected, record.kind
                ));
            }
            sender
                .send(ReaderEvent::Control(record, elapsed_ns(started)))
                .map_err(|_| "memory controller stopped receiving events".to_string())?;
            if expected == ControlKind::ExitResult {
                let mut length = [0_u8; 4];
                stdout
                    .read_exact(&mut length)
                    .map_err(|error| format!("read memory child result length: {error}"))?;
                let length = u32::from_le_bytes(length) as usize;
                if length == 0 || length > 1024 * 1024 {
                    return Err("memory child result length is invalid".into());
                }
                let mut bytes = vec![0_u8; length];
                stdout
                    .read_exact(&mut bytes)
                    .map_err(|error| format!("read memory child result: {error}"))?;
                let response = serde_json::from_slice(&bytes)
                    .map_err(|error| format!("decode memory child result: {error}"))?;
                sender
                    .send(ReaderEvent::Result(Box::new(response)))
                    .map_err(|_| "memory controller stopped receiving result".to_string())?;
            }
        }
        Ok(())
    })();
    if let Err(error) = result {
        let _ = sender.send(ReaderEvent::Error(error));
    }
}

fn wait_until(
    target: Instant,
    deadline: Instant,
    process: &mut std::process::Child,
) -> Result<(), String> {
    loop {
        if Instant::now() >= deadline {
            return Err("memory child exceeded its watchdog deadline".into());
        }
        if let Some(status) = process
            .try_wait()
            .map_err(|error| format!("poll memory child: {error}"))?
        {
            return Err(format!("memory child exited before result: {status}"));
        }
        let now = Instant::now();
        if now >= target {
            return Ok(());
        }
        std::thread::sleep((target - now).min(Duration::from_millis(10)));
    }
}

/// Run one measured child while sampling its `/proc` files externally. This
/// function is intentionally sequential: callers must not execute allocator
/// children in parallel.
pub fn run_memory_child_sample(
    child: &ChildProgram,
    request: &BenchmarkChildRequest,
    environment: &MemoryEnvironment,
    timeout: Duration,
) -> Result<MemoryRawSample, String> {
    if cfg!(not(target_os = "linux")) {
        return Err("linux-process-memory-v1 is Linux-only".into());
    }
    if child.allocator != request.allocator || timeout <= Duration::from_secs(5) {
        return Err("memory child identity mismatch or timeout too short".into());
    }
    let encoded = serde_json::to_vec(request)
        .map_err(|error| format!("serialize memory child request: {error}"))?;
    if encoded.len() >= 1024 * 1024 {
        return Err("memory child request exceeded 1 MiB".into());
    }
    let mut command = Command::new(&child.program);
    command
        .args(&child.arguments)
        .arg("--memory")
        .env_clear()
        .envs(child.environment.iter().map(|(key, value)| (key, value)))
        .env("MIMALLOC_PROF", "0")
        .env("MIMALLOC_MEMORY_EVENTS", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let started = Instant::now();
    let deadline = started + timeout;
    let mut process = command
        .spawn()
        .map_err(|error| format!("spawn memory child: {error}"))?;
    let sampled_pid = process.id();
    let sampler_pid = std::process::id();
    if let Err(error) = validate_sampler_ownership(sampler_pid, sampled_pid) {
        let _ = process.kill();
        let _ = process.wait();
        return Err(error);
    }
    let Some(mut stdin) = process.stdin.take() else {
        let _ = process.kill();
        let _ = process.wait();
        return Err("memory child stdin was not piped".into());
    };
    if let Err(error) = stdin
        .write_all(&encoded)
        .and_then(|()| stdin.write_all(b"\n"))
        .and_then(|()| stdin.flush())
    {
        let _ = process.kill();
        let _ = process.wait();
        return Err(format!("write memory child request: {error}"));
    }
    let Some(stdout) = process.stdout.take() else {
        let _ = process.kill();
        let _ = process.wait();
        return Err("memory child stdout was not piped".into());
    };
    let (sender, receiver) = mpsc::channel();
    let reader = std::thread::spawn(move || read_memory_child_events(stdout, sender, started));

    let result = (|| -> Result<MemoryRawSample, String> {
        let (_baseline_record, baseline_ready_ns) =
            receive_control(&receiver, ControlKind::BaselineReady, remaining(deadline))?;
        let baseline = read_proc_snapshot(sampled_pid)?;
        write_control_record(&mut stdin, ControlRecord::empty(ControlKind::Begin))?;
        let (_active_record, workload_active_ns) =
            receive_control(&receiver, ControlKind::WorkloadActive, remaining(deadline))?;

        let mut timeline = Vec::with_capacity(512);
        let mut next_sample = Instant::now();
        let (drained_record, workload_drained_ns) = loop {
            if Instant::now() >= deadline {
                return Err("memory child timed out during workload".into());
            }
            match receiver.try_recv() {
                Ok(ReaderEvent::Control(record, observed_ns))
                    if record.kind == ControlKind::WorkloadDrained =>
                {
                    break (record, observed_ns);
                }
                Ok(ReaderEvent::Control(record, _)) => {
                    return Err(format!("unexpected memory control {:?}", record.kind));
                }
                Ok(ReaderEvent::Result(_)) => {
                    return Err("memory child returned before the post-drain window".into());
                }
                Ok(ReaderEvent::Error(error)) => return Err(error),
                Err(mpsc::TryRecvError::Disconnected) => {
                    return Err("memory child control channel disconnected".into());
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
            if Instant::now() >= next_sample {
                let snapshot = read_proc_snapshot(sampled_pid)?;
                timeline.push(MemoryTimelinePoint {
                    elapsed_ns: elapsed_ns(started),
                    rss_bytes: snapshot.rss_bytes,
                });
                next_sample += Duration::from_nanos(MEMORY_SAMPLE_TARGET_NS);
            } else {
                std::thread::sleep(
                    next_sample
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(1)),
                );
            }
        };
        let kernel_peak_hwm_bytes = read_proc_snapshot(sampled_pid)?.hwm_bytes;
        let drain_observed = started + Duration::from_nanos(workload_drained_ns);

        wait_until(
            drain_observed + Duration::from_millis(100),
            deadline,
            &mut process,
        )?;
        let post_drain_sample_100ms_ns = elapsed_ns(started);
        let post_drain_rss_100ms_bytes = read_proc_snapshot(sampled_pid)?.rss_bytes;
        wait_until(
            drain_observed + Duration::from_secs(1),
            deadline,
            &mut process,
        )?;
        let post_drain_sample_1s_ns = elapsed_ns(started);
        let post_drain_rss_1s_bytes = read_proc_snapshot(sampled_pid)?.rss_bytes;
        wait_until(
            drain_observed + Duration::from_secs(5),
            deadline,
            &mut process,
        )?;
        let post_drain_sample_5s_ns = elapsed_ns(started);
        let post_drain_rss_5s_bytes = read_proc_snapshot(sampled_pid)?.rss_bytes;
        write_control_record(&mut stdin, ControlRecord::empty(ControlKind::ExitResult))?;

        let (_exit_record, _) =
            receive_control(&receiver, ControlKind::ExitResult, remaining(deadline))?;
        let response = match receiver.recv_timeout(remaining(deadline)) {
            Ok(ReaderEvent::Result(response)) => *response,
            Ok(ReaderEvent::Error(error)) => return Err(error),
            Ok(ReaderEvent::Control(record, _)) => {
                return Err(format!("unexpected control after exit: {:?}", record.kind));
            }
            Err(error) => return Err(format!("wait for memory child result: {error}")),
        };
        response.validate_against(request)?;
        if drained_record.current_live_requested_bytes != 0
            || drained_record.peak_live_requested_bytes != response.sample.peak_live_requested_bytes
            || drained_record.checksum != response.sample.checksum
        {
            return Err("memory control oracle contradicts the child result".into());
        }
        let status = loop {
            if let Some(status) = process
                .try_wait()
                .map_err(|error| format!("poll memory child exit: {error}"))?
            {
                break status;
            }
            if Instant::now() >= deadline {
                return Err("memory child did not exit before its watchdog deadline".into());
            }
            std::thread::sleep(Duration::from_millis(1));
        };
        if !status.success() {
            return Err(format!("memory child exited unsuccessfully: {status}"));
        }
        let mut stderr_bytes = Vec::new();
        if let Some(mut stderr) = process.stderr.take() {
            stderr
                .read_to_end(&mut stderr_bytes)
                .map_err(|error| format!("read memory child stderr: {error}"))?;
        }
        if !stderr_bytes.is_empty() {
            return Err(format!(
                "successful memory child wrote stderr: {}",
                String::from_utf8_lossy(&stderr_bytes)
            ));
        }
        if timeline.len() < 2 {
            return Err("memory workload ended before two external RSS samples".into());
        }
        // The reader timestamps the drain on receipt. A final sample can race
        // that receipt by microseconds, so retain only the observed workload
        // window and fail if this leaves insufficient evidence.
        timeline.retain(|point| {
            point.elapsed_ns >= workload_active_ns && point.elapsed_ns <= workload_drained_ns
        });
        let sampling = validate_timeline(
            &timeline,
            workload_active_ns,
            workload_drained_ns,
            MEMORY_SAMPLE_TARGET_NS,
        )?;
        let sampled_peak_rss_bytes = timeline
            .iter()
            .map(|point| point.rss_bytes)
            .max()
            .ok_or_else(|| "memory workload timeline is empty".to_string())?;
        let sampled_peak_rss_delta_bytes =
            signed_delta(sampled_peak_rss_bytes, baseline.rss_bytes)?;
        let fragmentation_proxy = fragmentation_proxy(
            sampled_peak_rss_delta_bytes,
            response.sample.peak_live_requested_bytes,
        )?;
        let hwm_delta = signed_delta(kernel_peak_hwm_bytes, baseline.hwm_bytes)?;
        let magnitude = sampled_peak_rss_delta_bytes.max(hwm_delta).max(0) as u64;
        let hwm_tolerance_bytes = (8 * 1024 * 1024).max(magnitude / 5);
        let hwm_discrepancy = (i128::from(sampled_peak_rss_delta_bytes) - i128::from(hwm_delta))
            .unsigned_abs()
            > u128::from(hwm_tolerance_bytes);

        Ok(MemoryRawSample {
            metric_schema_version: MEMORY_SCHEMA_VERSION.into(),
            block_id: request.block_id,
            ordinal: request.ordinal,
            workload_seed: request.workload_seed,
            allocator_id: request.allocator.allocator_id.clone(),
            allocator_source_sha: request.allocator.source_sha.clone(),
            child_binary_sha256: request.allocator.child_binary_sha256.clone(),
            scenario_id: request.scenario_id.clone(),
            thread_point: request.thread_point.clone(),
            thread_count: response.sample.thread_count,
            baseline_ready_ns,
            workload_active_ns,
            workload_drained_ns,
            post_drain_sample_100ms_ns,
            post_drain_sample_1s_ns,
            post_drain_sample_5s_ns,
            sampler_pid,
            sampled_pid,
            baseline_rss_bytes: baseline.rss_bytes,
            baseline_hwm_bytes: baseline.hwm_bytes,
            sampled_peak_rss_bytes,
            kernel_peak_hwm_bytes,
            peak_live_requested_bytes: response.sample.peak_live_requested_bytes,
            post_drain_rss_100ms_bytes,
            post_drain_rss_1s_bytes,
            post_drain_rss_5s_bytes,
            sampled_peak_rss_delta_bytes,
            post_drain_rss_delta_100ms_bytes: signed_delta(
                post_drain_rss_100ms_bytes,
                baseline.rss_bytes,
            )?,
            post_drain_rss_delta_1s_bytes: signed_delta(
                post_drain_rss_1s_bytes,
                baseline.rss_bytes,
            )?,
            post_drain_rss_delta_5s_bytes: signed_delta(
                post_drain_rss_5s_bytes,
                baseline.rss_bytes,
            )?,
            fragmentation_proxy,
            hwm_discrepancy,
            hwm_tolerance_bytes,
            sampling,
            timeline,
            environment: environment.clone(),
            child_sample: response.sample,
        })
    })();

    if result.is_err() {
        let _ = process.kill();
        let _ = process.wait();
    }
    drop(stdin);
    if reader.join().is_err() && result.is_ok() {
        return Err("memory child event reader panicked".into());
    }
    result
}

pub fn validate_timeline(
    points: &[MemoryTimelinePoint],
    workload_active_ns: u64,
    workload_drained_ns: u64,
    target_interval_ns: u64,
) -> Result<SamplingIntervalDistribution, String> {
    if points.len() < 2
        || target_interval_ns == 0
        || workload_active_ns >= workload_drained_ns
        || points.first().map(|point| point.elapsed_ns) < Some(workload_active_ns)
        || points.last().map(|point| point.elapsed_ns) > Some(workload_drained_ns)
    {
        return Err("memory timeline does not cover a valid workload window".into());
    }
    let leading_gap = points[0].elapsed_ns - workload_active_ns;
    let trailing_gap = workload_drained_ns - points[points.len() - 1].elapsed_ns;
    if leading_gap > MEMORY_MAX_SAMPLE_GAP_NS || trailing_gap > MEMORY_MAX_SAMPLE_GAP_NS {
        return Err("memory timeline leaves an unobserved workload edge".into());
    }
    let mut intervals = Vec::with_capacity(points.len() - 1);
    for pair in points.windows(2) {
        let interval = pair[1]
            .elapsed_ns
            .checked_sub(pair[0].elapsed_ns)
            .filter(|value| *value > 0)
            .ok_or_else(|| "memory timeline timestamps must strictly increase".to_string())?;
        if interval > MEMORY_MAX_SAMPLE_GAP_NS {
            return Err(format!(
                "memory sampler gap {interval}ns exceeds {MEMORY_MAX_SAMPLE_GAP_NS}ns bound"
            ));
        }
        intervals.push(interval);
    }
    intervals.sort_unstable();
    let quantile = |numerator: usize, denominator: usize| -> u64 {
        let index = (intervals.len() - 1) * numerator / denominator;
        intervals[index]
    };
    Ok(SamplingIntervalDistribution {
        target_interval_ns,
        sample_count: points.len() as u64,
        minimum_interval_ns: intervals[0],
        median_interval_ns: quantile(1, 2),
        p95_interval_ns: quantile(95, 100),
        maximum_interval_ns: *intervals.last().expect("timeline has an interval"),
    })
}

pub fn memory_scenario_cells(
    topology: Topology,
) -> Result<Vec<(CardId, ThreadPoint, usize)>, String> {
    topology.validate().map_err(|error| error.to_string())?;
    let declared = [
        (CardId::LargeObjects, ThreadPoint::One),
        (CardId::LargeObjects, ThreadPoint::Two),
        (CardId::SawtoothRetainDrain, ThreadPoint::One),
        (CardId::SawtoothRetainDrain, ThreadPoint::PhysicalCores),
        (CardId::SmallLogMixed, ThreadPoint::PhysicalCores),
        (
            CardId::CrossThreadProducerConsumer,
            ThreadPoint::PhysicalCores,
        ),
        (CardId::ThreadChurn, ThreadPoint::PhysicalCores),
    ];
    declared
        .into_iter()
        .map(|(card, point)| {
            let threads = topology.resolve(point).map_err(|error| error.to_string())?;
            if card == CardId::CrossThreadProducerConsumer && threads < 2 {
                return Err("memory cross-thread cell requires at least two physical cores".into());
            }
            Ok((card, point, threads))
        })
        .collect()
}

pub fn memory_comparison_key(input: &MemoryCompatibility) -> Result<String, String> {
    if input.metric_schema_version.is_empty()
        || input.page_size_bytes == 0
        || input.kernel.is_empty()
        || input.sampling_target_interval_ns == 0
        || input.purge_policy != "natural-only"
        || input.transparent_hugepage.is_empty()
        || input.cgroup_memory_max.is_empty()
        || input.cgroup_memory_high.is_empty()
        || input.allocator_runtime_options.is_empty()
    {
        return Err("memory comparison compatibility is incomplete".into());
    }
    let bytes = serde_json::to_vec(input)
        .map_err(|error| format!("serialize memory comparison compatibility: {error}"))?;
    Ok(sha256_bytes(&bytes))
}

fn sample_value(sample: &MemoryRawSample, metric: &str) -> f64 {
    match metric {
        "sampled-peak-rss-bytes" => sample.sampled_peak_rss_bytes as f64,
        "post-drain-rss-100ms-bytes" => sample.post_drain_rss_100ms_bytes as f64,
        "post-drain-rss-1s-bytes" => sample.post_drain_rss_1s_bytes as f64,
        "post-drain-rss-5s-bytes" => sample.post_drain_rss_5s_bytes as f64,
        "fragmentation-proxy" => sample.fragmentation_proxy,
        _ => unreachable!("metric list is fixed"),
    }
}

pub fn validate_memory_raw_run(raw: &MemoryRawRun) -> Result<(), String> {
    if raw.metric_schema_version != MEMORY_SCHEMA_VERSION
        || raw.status != "complete"
        || raw.run_seed == 0
    {
        return Err("memory raw run must be a complete linux-process-memory-v1 value".into());
    }
    if raw.allocators.len() != 4 || raw.calibrations.len() != 7 || raw.samples.is_empty() {
        return Err("memory raw run has an incomplete allocator/cell matrix".into());
    }
    let allocator_ids = raw
        .allocators
        .iter()
        .map(|allocator| allocator.allocator_id.as_str())
        .collect::<Vec<_>>();
    if allocator_ids != ALLOCATOR_IDS {
        return Err("memory raw run allocator order or identity is invalid".into());
    }
    let topology = Topology {
        physical_cores: raw.runner.physical_cores as usize,
        logical_cores: raw.runner.logical_cores as usize,
    };
    let required = memory_scenario_cells(topology)?;
    let required_keys = required
        .iter()
        .map(|(card, point, _)| (card.as_str(), point.name()))
        .collect::<BTreeSet<_>>();
    let required_threads = required
        .iter()
        .map(|(card, point, threads)| ((card.as_str(), point.name()), *threads as u32))
        .collect::<BTreeMap<_, _>>();
    let actual_calibrations = raw
        .calibrations
        .iter()
        .map(|value| (value.scenario_id.as_str(), value.thread_point.as_str()))
        .collect::<BTreeSet<_>>();
    if actual_calibrations != required_keys {
        return Err("memory calibrations do not match the declared matrix".into());
    }
    let calibrations = raw
        .calibrations
        .iter()
        .map(|value| {
            (
                (value.scenario_id.as_str(), value.thread_point.as_str()),
                value,
            )
        })
        .collect::<BTreeMap<_, _>>();
    if calibrations.len() != raw.calibrations.len()
        || raw.calibrations.iter().any(|value| {
            value.transactions_per_worker == 0
                || value.warmup_transactions_per_worker == 0
                || value.thread_count == 0
                || required_threads.get(&(value.scenario_id.as_str(), value.thread_point.as_str()))
                    != Some(&value.thread_count)
                || value.operation_count == 0
                || !(500_000_000..=2_000_000_000).contains(&value.elapsed_ns)
        })
    {
        return Err("memory calibrations are duplicate or outside the declared contract".into());
    }
    let allocators = raw
        .allocators
        .iter()
        .map(|value| (value.allocator_id.as_str(), value))
        .collect::<BTreeMap<_, _>>();
    if allocators.len() != raw.allocators.len() {
        return Err("memory allocator provenance contains duplicate identities".into());
    }
    let child_runner = RunnerMetadata {
        os: raw.runner.os.clone(),
        architecture: raw.runner.architecture.clone(),
        physical_cores: raw.runner.physical_cores,
        logical_cores: raw.runner.logical_cores,
    };
    let first = raw
        .samples
        .first()
        .ok_or_else(|| "memory raw run has no samples".to_string())?;
    let compatibility = first
        .environment
        .compatibility(first.sampling.target_interval_ns);
    memory_comparison_key(&compatibility)?;

    let mut blocks_by_cell: BTreeMap<(&str, &str), BTreeMap<u32, Vec<&MemoryRawSample>>> =
        BTreeMap::new();
    for sample in &raw.samples {
        let calibration = calibrations
            .get(&(sample.scenario_id.as_str(), sample.thread_point.as_str()))
            .ok_or_else(|| "memory sample has no matching calibration".to_string())?;
        let allocator = allocators
            .get(sample.allocator_id.as_str())
            .ok_or_else(|| "memory sample has no matching allocator provenance".to_string())?;
        sample.child_sample.validate()?;
        let expected_toolchain = ToolchainMetadata {
            rustc: raw.runner.rustc.clone(),
            target: raw.runner.target.clone(),
            compiler: allocator.compiler.clone(),
            linker: allocator.linker.clone(),
        };
        if sample.metric_schema_version != MEMORY_SCHEMA_VERSION
            || sample.ordinal >= 4
            || sample.child_sample.run_kind != "headline"
            || sample.child_sample.execution_mode != "normal"
            || sample.child_sample.run_seed != raw.run_seed
            || sample.child_sample.ordinal != sample.ordinal
            || sample.allocator_id != sample.child_sample.allocator_id
            || sample.child_sample.allocator_version != allocator.allocator_version
            || sample.allocator_source_sha != sample.child_sample.allocator_source_sha
            || sample.child_sample.allocator_source_sha != allocator.source_sha
            || sample.child_sample.allocator_library_sha256 != allocator.static_library_sha256
            || sample.child_binary_sha256 != sample.child_sample.child_binary_sha256
            || sample.child_sample.child_binary_sha256 != allocator.child_binary_sha256
            || sample.scenario_id != sample.child_sample.scenario_id
            || sample.thread_point != sample.child_sample.thread_point
            || sample.thread_count != sample.child_sample.thread_count
            || sample.thread_count != calibration.thread_count
            || sample.block_id != sample.child_sample.block_id
            || sample.workload_seed != sample.child_sample.workload_seed
            || sample.peak_live_requested_bytes != sample.child_sample.peak_live_requested_bytes
            || sample.child_sample.operation_count != calibration.operation_count
            || sample.child_sample.warmup_ns == 0
            || sample.child_sample.runner != child_runner
            || sample.child_sample.toolchain != expected_toolchain
            || sample.baseline_ready_ns >= sample.workload_active_ns
            || sample.workload_active_ns >= sample.workload_drained_ns
            || sample.sampling.target_interval_ns != MEMORY_SAMPLE_TARGET_NS
            || sample.environment.purge_policy != "natural-only"
            || sample.environment.hosted_runner != (raw.runner.runner_class == "github-hosted")
            || validate_sampler_ownership(sample.sampler_pid, sample.sampled_pid).is_err()
        {
            return Err("memory sample contradicts its child workload result".into());
        }
        for (observed, target) in [
            (sample.post_drain_sample_100ms_ns, 100_000_000_u64),
            (sample.post_drain_sample_1s_ns, 1_000_000_000_u64),
            (sample.post_drain_sample_5s_ns, 5_000_000_000_u64),
        ] {
            let since_drain = observed
                .checked_sub(sample.workload_drained_ns)
                .ok_or_else(|| "post-drain sample precedes workload drain".to_string())?;
            if since_drain < target || since_drain > target.saturating_add(100_000_000) {
                return Err("post-drain sample missed its declared observation window".into());
            }
        }
        let maximum = sample
            .timeline
            .iter()
            .map(|point| point.rss_bytes)
            .max()
            .ok_or_else(|| "memory sample timeline is empty".to_string())?;
        if maximum != sample.sampled_peak_rss_bytes
            || signed_delta(sample.sampled_peak_rss_bytes, sample.baseline_rss_bytes)?
                != sample.sampled_peak_rss_delta_bytes
            || signed_delta(sample.post_drain_rss_100ms_bytes, sample.baseline_rss_bytes)?
                != sample.post_drain_rss_delta_100ms_bytes
            || signed_delta(sample.post_drain_rss_1s_bytes, sample.baseline_rss_bytes)?
                != sample.post_drain_rss_delta_1s_bytes
            || signed_delta(sample.post_drain_rss_5s_bytes, sample.baseline_rss_bytes)?
                != sample.post_drain_rss_delta_5s_bytes
            || fragmentation_proxy(
                sample.sampled_peak_rss_delta_bytes,
                sample.peak_live_requested_bytes,
            )? != sample.fragmentation_proxy
            || sample
                .environment
                .compatibility(sample.sampling.target_interval_ns)
                != compatibility
        {
            return Err("memory sample has inconsistent derived values or compatibility".into());
        }
        let hwm_delta = signed_delta(sample.kernel_peak_hwm_bytes, sample.baseline_hwm_bytes)?;
        let magnitude = sample.sampled_peak_rss_delta_bytes.max(hwm_delta).max(0) as u64;
        let expected_tolerance = (8 * 1024 * 1024).max(magnitude / 5);
        let expected_discrepancy = (i128::from(sample.sampled_peak_rss_delta_bytes)
            - i128::from(hwm_delta))
        .unsigned_abs()
            > u128::from(expected_tolerance);
        if sample.hwm_tolerance_bytes != expected_tolerance
            || sample.hwm_discrepancy != expected_discrepancy
        {
            return Err("memory VmHWM discrepancy flag or tolerance is inconsistent".into());
        }
        let derived_sampling = validate_timeline(
            &sample.timeline,
            sample.workload_active_ns,
            sample.workload_drained_ns,
            sample.sampling.target_interval_ns,
        )?;
        if derived_sampling != sample.sampling {
            return Err("memory sampling distribution does not match timestamps".into());
        }
        blocks_by_cell
            .entry((&sample.scenario_id, &sample.thread_point))
            .or_default()
            .entry(sample.block_id)
            .or_default()
            .push(sample);
    }
    if blocks_by_cell.len() != required_keys.len() {
        return Err("memory samples omit a required scenario/thread cell".into());
    }
    for (cell, blocks) in blocks_by_cell {
        if blocks.len() < MEMORY_MIN_BLOCKS as usize {
            return Err(format!(
                "memory cell {cell:?} has fewer than 15 complete blocks"
            ));
        }
        for (block, samples) in blocks {
            let ids = samples
                .iter()
                .map(|sample| sample.allocator_id.as_str())
                .collect::<BTreeSet<_>>();
            let ordinals = samples
                .iter()
                .map(|sample| sample.ordinal)
                .collect::<BTreeSet<_>>();
            let seeds = samples
                .iter()
                .map(|sample| sample.workload_seed)
                .collect::<BTreeSet<_>>();
            if samples.len() != 4
                || ids != ALLOCATOR_IDS.into_iter().collect()
                || ordinals != [0, 1, 2, 3].into_iter().collect()
                || seeds.len() != 1
            {
                return Err(format!(
                    "memory cell {cell:?} block {block} is not a complete pair"
                ));
            }
            let first_child = &samples[0].child_sample;
            let same_workload = samples.iter().all(|sample| {
                let child = &sample.child_sample;
                child.run_seed == first_child.run_seed
                    && child.workload_seed == first_child.workload_seed
                    && child.thread_count == first_child.thread_count
                    && child.operation_unit == first_child.operation_unit
                    && child.operation_count == first_child.operation_count
                    && child.requested_transactions == first_child.requested_transactions
                    && child.completed_transactions == first_child.completed_transactions
                    && child.allocation_calls == first_child.allocation_calls
                    && child.calloc_calls == first_child.calloc_calls
                    && child.aligned_allocation_calls == first_child.aligned_allocation_calls
                    && child.free_calls == first_child.free_calls
                    && child.realloc_calls == first_child.realloc_calls
                    && child.checksum == first_child.checksum
            });
            if !same_workload {
                return Err(format!(
                    "memory cell {cell:?} block {block} is not a complete pair"
                ));
            }
            // The deterministic scenario/checksum derivation is allocator-independent,
            // so validate it once per complete paired block after all four children
            // proved identical workload identity above.
            let first = samples[0];
            let calibration = calibrations
                .get(&(first.scenario_id.as_str(), first.thread_point.as_str()))
                .ok_or_else(|| "memory block lost its validated calibration".to_string())?;
            let allocator = allocators
                .get(first.allocator_id.as_str())
                .ok_or_else(|| "memory block lost its validated allocator".to_string())?;
            let request = BenchmarkChildRequest {
                protocol_version: CHILD_PROTOCOL_VERSION.into(),
                schema_version: crate::RAW_SCHEMA_VERSION.into(),
                suite_version: crate::CORE_SUITE_VERSION.into(),
                run_kind: "headline".into(),
                execution_mode: "normal".into(),
                run_seed: raw.run_seed,
                block_id: first.block_id,
                ordinal: first.ordinal,
                workload_seed: first.workload_seed,
                allocator: AllocatorIdentity {
                    allocator_id: allocator.allocator_id.clone(),
                    allocator_version: allocator.allocator_version.clone(),
                    source_sha: allocator.source_sha.clone(),
                    library_sha256: allocator.static_library_sha256.clone(),
                    child_binary_sha256: allocator.child_binary_sha256.clone(),
                },
                scenario_id: first.scenario_id.clone(),
                scenario_version: crate::CORE_SUITE_VERSION.into(),
                thread_point: first.thread_point.clone(),
                physical_cores: raw.runner.physical_cores,
                logical_cores: raw.runner.logical_cores,
                transactions_per_worker: calibration.transactions_per_worker,
                warmup_transactions_per_worker: calibration.warmup_transactions_per_worker,
                reproduction_command: first_child.reproduction_command.clone(),
                runner: child_runner.clone(),
                toolchain: first_child.toolchain.clone(),
            };
            BenchmarkChildResponse {
                protocol_version: CHILD_PROTOCOL_VERSION.into(),
                sample: first_child.clone(),
            }
            .validate_against(&request)?;
        }
    }
    Ok(())
}

#[derive(Serialize)]
struct RunKey<'a> {
    protocol: MemoryCompatibility,
    runner_fingerprint: &'a str,
    allocator_lock_sha256: &'a str,
    allocator_sources: Vec<(&'a str, &'a str, &'a str)>,
    calibrations: Vec<(&'a str, &'a str, u32, u64)>,
}

fn run_comparison_key(raw: &MemoryRawRun) -> Result<String, String> {
    let first = raw
        .samples
        .first()
        .ok_or_else(|| "memory raw run has no samples".to_string())?;
    let mut allocator_sources = raw
        .allocators
        .iter()
        .map(|value| {
            (
                value.allocator_id.as_str(),
                value.source_sha.as_str(),
                value.child_binary_sha256.as_str(),
            )
        })
        .collect::<Vec<_>>();
    allocator_sources.sort_unstable();
    let mut calibrations = raw
        .calibrations
        .iter()
        .map(|value| {
            (
                value.scenario_id.as_str(),
                value.thread_point.as_str(),
                value.thread_count,
                value.operation_count,
            )
        })
        .collect::<Vec<_>>();
    calibrations.sort_unstable();
    let key = RunKey {
        protocol: first
            .environment
            .compatibility(first.sampling.target_interval_ns),
        runner_fingerprint: &raw.runner.fingerprint_sha256,
        allocator_lock_sha256: &raw.allocator_lock_sha256,
        allocator_sources,
        calibrations,
    };
    let bytes =
        serde_json::to_vec(&key).map_err(|error| format!("serialize memory run key: {error}"))?;
    Ok(sha256_bytes(&bytes))
}

pub fn build_memory_report(raw: &MemoryRawRun) -> Result<MemoryMetricReport, String> {
    validate_memory_raw_run(raw)?;
    let mut absolute_summaries = Vec::new();
    let mut paired_summaries = Vec::new();
    let topology = Topology {
        physical_cores: raw.runner.physical_cores as usize,
        logical_cores: raw.runner.logical_cores as usize,
    };
    for (card, point, _) in memory_scenario_cells(topology)? {
        let cell_samples = raw.samples.iter().filter(|sample| {
            sample.scenario_id == card.as_str() && sample.thread_point == point.name()
        });
        for (metric, _unit) in METRICS {
            let observations = cell_samples
                .clone()
                .map(|sample| MetricObservation {
                    block_id: sample.block_id,
                    allocator_id: sample.allocator_id.clone(),
                    value: sample_value(sample, metric),
                })
                .collect::<Vec<_>>();
            let cell_id = format!("{}/{}/{metric}", card.as_str(), point.name());
            for allocator in ALLOCATOR_IDS {
                let values = cell_samples
                    .clone()
                    .filter(|sample| sample.allocator_id == allocator)
                    .map(|sample| sample_value(sample, metric))
                    .collect::<Vec<_>>();
                absolute_summaries.push(AbsoluteCellSummary {
                    scenario_id: card.as_str().into(),
                    thread_point: point.name().into(),
                    metric_id: metric.into(),
                    direction: MetricDirection::LowerIsBetter,
                    allocator_id: allocator.into(),
                    summary: summarize_absolute(&values).map_err(|error| error.to_string())?,
                });
                if allocator != REFERENCE_ALLOCATOR {
                    paired_summaries.push(PairedCellSummary {
                        scenario_id: card.as_str().into(),
                        thread_point: point.name().into(),
                        metric_id: metric.into(),
                        summary: summarize_paired(
                            raw.run_seed,
                            &cell_id,
                            allocator,
                            REFERENCE_ALLOCATOR,
                            MetricDirection::LowerIsBetter,
                            &observations,
                        )
                        .map_err(|error| error.to_string())?,
                    });
                }
            }
        }
    }
    Ok(MemoryMetricReport {
        metric_schema_version: MEMORY_SCHEMA_VERSION.into(),
        status: "complete".into(),
        invalid_reason: None,
        metric_comparison_key: run_comparison_key(raw)?,
        run: raw.run.clone(),
        runner: raw.runner.clone(),
        sampling_target_interval_ns: MEMORY_SAMPLE_TARGET_NS,
        purge_policy: "natural-only".into(),
        units: METRICS
            .into_iter()
            .map(|(metric, unit)| (metric.into(), unit.into()))
            .collect(),
        direction: MetricDirection::LowerIsBetter,
        informational: true,
        methodology: MemoryMethodology {
            rss_source: "/proc/<pid>/smaps_rollup Rss, parsed as integer kB * 1024".into(),
            hwm_source: "/proc/<pid>/status VmHWM, parsed as integer kB * 1024; cross-check only".into(),
            baseline_definition: "external RSS/HWM after child warmup and baseline-ready, before begin".into(),
            sampled_peak_definition: "maximum external smaps_rollup RSS timestamped inside workload-active..workload-drained".into(),
            post_drain_definition: "external smaps_rollup RSS at >=100ms, >=1s, and >=5s after workload-drained".into(),
            fragmentation_formula: "(sampled_peak_rss_bytes - baseline_rss_bytes) / peak_live_requested_bytes; both operands must be positive".into(),
            hwm_discrepancy_tolerance: "flag when abs(sampled RSS delta - VmHWM delta) > max(8 MiB, 20% of the larger positive delta)".into(),
            page_touch_contract: "every allocation touches deterministic boundary bytes and at least one byte per OS page".into(),
            purge_policy: "natural allocator behavior only; no allocator-specific purge call".into(),
        },
        absolute_summaries,
        paired_summaries,
        raw_samples: raw.samples.clone(),
    })
}

pub fn attach_memory_report(
    latest: &mut LatestReport,
    report: MemoryMetricReport,
) -> Result<(), String> {
    if report.metric_schema_version != MEMORY_SCHEMA_VERSION
        || report.status != "complete"
        || report.invalid_reason.is_some()
        || report.raw_samples.is_empty()
        || report.absolute_summaries.is_empty()
        || report.paired_summaries.is_empty()
    {
        return Err("only a complete validated memory report can replace pending state".into());
    }
    let pending_ids = latest
        .pending_metrics
        .iter()
        .map(|value| value.metric_id.as_str())
        .collect::<BTreeSet<_>>();
    if !pending_ids.contains("memory") && latest.memory.is_none() {
        return Err("latest report has neither memory pending state nor prior memory data".into());
    }
    latest
        .pending_metrics
        .retain(|value| value.metric_id != "memory");
    latest.memory = Some(report);
    Ok(())
}
