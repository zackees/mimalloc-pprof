//! Reusable concurrent allocator stress/soak harness.
//!
//! Provides deterministic, native-thread-based concurrent workload execution
//! with:
//! - Barrier-synchronized worker start for genuinely overlapping operations
//! - Atomically tracked simultaneous worker count (the "concurrency oracle")
//! - Child-process isolation with hard watchdogs for hang-prone scenarios
//! - Machine-readable JSON output with full reproduction metadata
//!
//! # Concurrency oracle
//!
//! A single-worker run must report `max_simultaneous_workers == 1`; a
//! multi-worker run must report `max_simultaneous_workers >= 2`.  This is
//! the RED/GREEN gate that proves the harness is exercising real concurrency.

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Barrier, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------------------
// Configuration & result types
// ---------------------------------------------------------------------------

/// Configuration for a stress scenario run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StressConfig {
    /// Human-readable scenario name (e.g. "tiny-hot-path", "cross-thread-free").
    pub name: String,
    /// Deterministic seed for reproducible runs.
    pub seed: u64,
    /// Number of native OS threads to spawn.
    pub worker_count: usize,
    /// Total allocation operations across all workers.
    pub operation_count: usize,
    /// Minimum allocation size in bytes.
    #[serde(default = "default_alloc_min")]
    pub allocation_size_min: usize,
    /// Maximum allocation size in bytes.
    #[serde(default = "default_alloc_max")]
    pub allocation_size_max: usize,
}

fn default_alloc_min() -> usize {
    16
}
fn default_alloc_max() -> usize {
    4096
}

/// Invalid workload configuration, rejected before threads are created.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StressError {
    ZeroWorkers,
    ZeroOperations,
    InsufficientOperationsForWorkers,
    InvalidAllocationRange,
    UnsupportedProtocolVersion(u32),
}

impl std::fmt::Display for StressError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ZeroWorkers => f.write_str("worker_count must be greater than zero"),
            Self::ZeroOperations => f.write_str("operation_count must be greater than zero"),
            Self::InsufficientOperationsForWorkers => {
                f.write_str("operation_count must be at least worker_count")
            }
            Self::InvalidAllocationRange => {
                f.write_str("allocation_size_min must not exceed allocation_size_max")
            }
            Self::UnsupportedProtocolVersion(version) => {
                write!(f, "unsupported stress-child protocol version {version}")
            }
        }
    }
}
impl std::error::Error for StressError {}

impl StressConfig {
    pub fn validate(&self) -> Result<(), StressError> {
        if self.worker_count == 0 {
            return Err(StressError::ZeroWorkers);
        }
        if self.operation_count == 0 {
            return Err(StressError::ZeroOperations);
        }
        if self.operation_count < self.worker_count {
            return Err(StressError::InsufficientOperationsForWorkers);
        }
        if self.allocation_size_min > self.allocation_size_max {
            return Err(StressError::InvalidAllocationRange);
        }
        Ok(())
    }
}

/// The result of executing a [`StressConfig`] against a [`ScenarioType`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StressResult {
    /// The config that produced this result.
    pub config: StressConfig,
    /// Peak concurrent workers observed during the run.
    pub max_simultaneous_workers: usize,
    /// Total operations completed before exit / timeout / crash.
    pub ops_completed: u64,
    /// Deterministic checksum observed from touched workload memory.
    pub observed_checksum: u64,
    /// Deterministic checksum expected from the versioned workload sequence.
    pub expected_checksum: u64,
    /// True if the watchdog killed the run.
    pub timed_out: bool,
    /// True if the child process exited with a non-zero code or signal.
    pub crashed: bool,
    /// Wall-clock elapsed seconds.
    pub elapsed_secs: f64,
    /// Exact command to reproduce this run.
    pub reproduction_command: String,
}

/// Built-in workload shapes that the harness knows how to materialise
/// (required so that both the in-process path and the child-process /
/// watchdog path can run the same workload without serialising closures).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ScenarioType {
    /// Allocate a random-sized block, immediately drop it.  Pure throughput.
    AllocFree,
    /// Accumulate blocks in a Vec, clear periodically (sawtooth RSS).
    Sawtooth,
    /// Sleep forever — a positive control for the watchdog path.
    Hang,
}

/// The workload implementation selected by a child request.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExecutionMode {
    Normal,
    SerializedControl,
}

/// Current wire format version for one isolated workload request.
pub const CHILD_PROTOCOL_VERSION: u32 = 1;

/// Versioned request accepted by the production stress-child entry point.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StressChildRequest {
    pub protocol_version: u32,
    pub config: StressConfig,
    pub scenario: ScenarioType,
    pub execution_mode: ExecutionMode,
}

#[derive(Default)]
struct StartGateState {
    parked_workers: usize,
    released: bool,
}

// ---------------------------------------------------------------------------
// Deterministic RNG (splitmix64 variant — no external dep)
// ---------------------------------------------------------------------------

/// A small, deterministic, seedable RNG used so every run with the same seed
/// produces the identical sequence of allocation sizes.
pub struct SeededRng(u64);

impl SeededRng {
    pub fn new(seed: u64) -> Self {
        Self(seed.wrapping_add(0x9e3779b97f4a7c15))
    }

    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^ (z >> 31)
    }

    /// Return a `usize` in `[0, max)` (or 0 when max == 0).
    pub fn next_usize(&mut self, max: usize) -> usize {
        if max == 0 {
            return 0;
        }
        (self.next() as usize) % max
    }
}

// ---------------------------------------------------------------------------
// In-process executor
// ---------------------------------------------------------------------------

/// Run in-process without a hard timeout. Use a child process when a workload
/// must be forcibly bounded.
/// Locate a cargo-built binary that sits next to the running test executable.
///
/// #294: `env!("CARGO_BIN_EXE_<name>")` is expanded by cargo AT COMPILE TIME into the
/// *builder's* absolute path, so a test using it carries a literal
/// `/home/runner/work/.../target/<profile>/stress-child` in its image and cannot run on
/// any other machine -- which is why windows-bundles.yml had to detect and drop those
/// binaries instead of shipping them. Resolving the path at run time keeps the test
/// relocatable: cargo puts integration-test executables in `<target>/<profile>/deps/`
/// and binaries one level up in `<target>/<profile>/`, and a test staged into a flat
/// bundle directory finds its companion right beside it.
///
/// `MIMALLOC_PPROF_BIN_DIR` overrides the search entirely, for a bundle that stages
/// binaries somewhere else again.
pub fn sibling_bin(name: &str) -> std::path::PathBuf {
    let file_name = format!("{name}{}", std::env::consts::EXE_SUFFIX);
    if let Some(dir) = std::env::var_os("MIMALLOC_PPROF_BIN_DIR") {
        return std::path::PathBuf::from(dir).join(file_name);
    }
    let exe = std::env::current_exe().expect("the running test executable has a path");
    let dir = exe
        .parent()
        .expect("the running test executable has a parent directory")
        .to_path_buf();
    // Prefer `<profile>/<name>` when we are in `<profile>/deps/`; fall back to a sibling,
    // which is where a flat test bundle puts both.
    if dir.file_name().is_some_and(|component| component == "deps") {
        if let Some(parent) = dir.parent() {
            let candidate = parent.join(&file_name);
            if candidate.exists() {
                return candidate;
            }
        }
    }
    dir.join(file_name)
}

pub fn run_scenario(
    config: StressConfig,
    scenario: ScenarioType,
) -> Result<StressResult, StressError> {
    run_with_mode(config, scenario, ExecutionMode::Normal)
}

/// Execute a versioned child request after validating its protocol version.
pub fn run_child_request(request: StressChildRequest) -> Result<StressResult, StressError> {
    if request.protocol_version != CHILD_PROTOCOL_VERSION {
        return Err(StressError::UnsupportedProtocolVersion(
            request.protocol_version,
        ));
    }
    run_with_mode(request.config, request.scenario, request.execution_mode)
}

fn run_with_mode(
    config: StressConfig,
    scenario: ScenarioType,
    execution_mode: ExecutionMode,
) -> Result<StressResult, StressError> {
    config.validate()?;
    let workers = config.worker_count;
    let simultaneous = Arc::new(AtomicUsize::new(0));
    let maximum = Arc::new(AtomicUsize::new(0));
    let completed = Arc::new(AtomicU64::new(0));
    let observed_checksum = Arc::new(AtomicU64::new(0));
    let expected_checksum = Arc::new(AtomicU64::new(0));
    let serial_lock = Arc::new(std::sync::Mutex::new(()));
    let ready = Arc::new(Barrier::new(workers + 1));
    let start_gate = Arc::new((Mutex::new(StartGateState::default()), Condvar::new()));
    let complete = Arc::new(Barrier::new(workers + 1));
    let mut handles = Vec::with_capacity(workers);
    for worker in 0..workers {
        let cfg = config.clone();
        let simultaneous = Arc::clone(&simultaneous);
        let maximum = Arc::clone(&maximum);
        let completed = Arc::clone(&completed);
        let observed_checksum = Arc::clone(&observed_checksum);
        let expected_checksum = Arc::clone(&expected_checksum);
        let serial_lock = Arc::clone(&serial_lock);
        let ready = Arc::clone(&ready);
        let start_gate = Arc::clone(&start_gate);
        let complete = Arc::clone(&complete);
        handles.push(thread::spawn(move || {
            let mut rng = SeededRng::new(cfg.seed.wrapping_add(worker as u64));
            let mut held = (scenario == ScenarioType::Sawtooth).then(|| Vec::with_capacity(1024));
            ready.wait();
            #[cfg(debug_assertions)]
            if worker == 0 {
                if let Ok(milliseconds) = std::env::var("STRESS_HARNESS_TEST_PRE_PARK_DELAY_MS") {
                    if let Ok(milliseconds) = milliseconds.parse::<u64>() {
                        thread::sleep(Duration::from_millis(milliseconds));
                    }
                }
            }
            let (gate_state, gate_ready) = &*start_gate;
            let mut gate = gate_state.lock().expect("start gate lock poisoned");
            gate.parked_workers += 1;
            // The coordinator and parked workers share this condition; wake
            // all so a worker cannot consume the readiness notification.
            gate_ready.notify_all();
            while !gate.released {
                gate = gate_ready.wait(gate).expect("start gate wait poisoned");
            }
            drop(gate);
            let current = simultaneous.fetch_add(1, Ordering::SeqCst) + 1;
            maximum.fetch_max(current, Ordering::SeqCst);
            let operation_count =
                cfg.operation_count / workers + usize::from(worker < cfg.operation_count % workers);
            match scenario {
                ScenarioType::AllocFree => {
                    for _ in 0..operation_count {
                        let serial_guard = (execution_mode == ExecutionMode::SerializedControl)
                            .then(|| {
                                serial_lock
                                    .lock()
                                    .expect("serialized control lock poisoned")
                            });
                        let size = cfg.allocation_size_min
                            + rng.next_usize(cfg.allocation_size_max - cfg.allocation_size_min + 1);
                        let expected = (size as u8).wrapping_add(worker as u8);
                        let mut allocation = vec![0u8; size];
                        allocation[0] = expected;
                        observed_checksum.fetch_add(allocation[0] as u64, Ordering::Relaxed);
                        expected_checksum.fetch_add(expected as u64, Ordering::Relaxed);
                        drop(allocation);
                        drop(serial_guard);
                        completed.fetch_add(1, Ordering::Relaxed);
                    }
                }
                ScenarioType::Sawtooth => {
                    let held = held.as_mut().expect("sawtooth control state preallocated");
                    for index in 0..operation_count {
                        let serial_guard = (execution_mode == ExecutionMode::SerializedControl)
                            .then(|| {
                                serial_lock
                                    .lock()
                                    .expect("serialized control lock poisoned")
                            });
                        let size = cfg.allocation_size_min
                            + rng.next_usize(cfg.allocation_size_max - cfg.allocation_size_min + 1);
                        let expected = (size as u8).wrapping_add(worker as u8);
                        let mut allocation = vec![0u8; size];
                        allocation[0] = expected;
                        observed_checksum.fetch_add(allocation[0] as u64, Ordering::Relaxed);
                        expected_checksum.fetch_add(expected as u64, Ordering::Relaxed);
                        held.push(allocation);
                        drop(serial_guard);
                        completed.fetch_add(1, Ordering::Relaxed);
                        if held.len() == 1024 || index + 1 == operation_count {
                            held.clear();
                        }
                    }
                }
                ScenarioType::Hang => loop {
                    thread::sleep(Duration::from_secs(1));
                },
            }
            simultaneous.fetch_sub(1, Ordering::SeqCst);
            complete.wait();
        }));
    }
    // Debug-test-only setup delay used to prove that pre-start coordination is
    // outside the reported interval. It is never present in release builds.
    #[cfg(debug_assertions)]
    if let Ok(milliseconds) = std::env::var("STRESS_HARNESS_TEST_SETUP_DELAY_MS") {
        if let Ok(milliseconds) = milliseconds.parse::<u64>() {
            thread::sleep(Duration::from_millis(milliseconds));
        }
    }
    ready.wait();
    let (gate_state, gate_ready) = &*start_gate;
    let mut gate = gate_state.lock().expect("start gate lock poisoned");
    while gate.parked_workers < workers {
        gate = gate_ready.wait(gate).expect("start gate wait poisoned");
    }
    let started = Instant::now();
    gate.released = true;
    gate_ready.notify_all();
    drop(gate);
    complete.wait();
    let elapsed_secs = started.elapsed().as_secs_f64();
    for handle in handles {
        let _ = handle.join();
    }
    Ok(StressResult {
        config,
        max_simultaneous_workers: maximum.load(Ordering::SeqCst),
        ops_completed: completed.load(Ordering::Relaxed),
        observed_checksum: observed_checksum.load(Ordering::Relaxed),
        expected_checksum: expected_checksum.load(Ordering::Relaxed),
        timed_out: false,
        crashed: false,
        elapsed_secs,
        reproduction_command: "cargo test -p stress-harness -- --nocapture --test-threads=1".into(),
    })
}

// ---------------------------------------------------------------------------
// Child-process isolation
// ---------------------------------------------------------------------------

/// Environment variable set on the child to trigger child-mode execution.
pub const CHILD_ENV_VAR: &str = "STRESS_HARNESS_CHILD_INPUT";

/// Spawn `config`+`scenario` in a child process with a hard `watchdog_secs`
/// wall-clock limit.  If the child hasn't exited by the deadline it is killed
/// and the result reports `timed_out: true`.
///
/// The child process is the **current** test binary (obtained via
/// `std::env::current_exe()`), invoked with the test filter
/// `child_process_isolation`.  This avoids `cargo test` lock conflicts and
/// guarantees the child links the same allocator as the parent.
pub fn run_in_child_process(
    config: StressConfig,
    scenario: ScenarioType,
    watchdog_secs: u64,
) -> StressResult {
    let input_json = serde_json::to_string(&(&config, scenario)).expect("serialise child input");

    // Build the reproduction command before we consume config.
    let repro = format!(
        "cargo test -p stress-harness -- child_process_isolation --nocapture --test-threads=1"
    );

    // Spawn the *same test binary* directly (not via `cargo test`) to avoid
    // lock-file collisions when a prior child was killed.
    let test_binary =
        std::env::current_exe().unwrap_or_else(|_| std::path::PathBuf::from("stress-harness-test"));

    let mut child = std::process::Command::new(&test_binary)
        .args(["--nocapture", "child_process_isolation"])
        .env(CHILD_ENV_VAR, &input_json)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap_or_else(|e| {
            panic!(
                "failed to spawn child test process {:?}: {}",
                test_binary, e
            )
        });

    let started = Instant::now();
    let deadline = started + Duration::from_secs(watchdog_secs);

    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let elapsed = started.elapsed().as_secs_f64();
                // Drain remaining output.
                let output = child
                    .wait_with_output()
                    .expect("child wait_with_output after try_wait");
                let stdout = String::from_utf8_lossy(&output.stdout);

                if status.success() {
                    // Try to parse the result from the last JSON line.
                    if let Some(line) = stdout
                        .lines()
                        .rev()
                        .find(|l| l.trim_start().starts_with('{'))
                    {
                        if let Ok(result) = serde_json::from_str::<StressResult>(line) {
                            return result;
                        }
                    }
                }

                return StressResult {
                    config,
                    max_simultaneous_workers: 0,
                    ops_completed: 0,
                    observed_checksum: 0,
                    expected_checksum: 0,
                    timed_out: false,
                    crashed: !status.success(),
                    elapsed_secs: elapsed,
                    reproduction_command: repro,
                };
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return StressResult {
                        config,
                        max_simultaneous_workers: 0,
                        ops_completed: 0,
                        observed_checksum: 0,
                        expected_checksum: 0,
                        timed_out: true,
                        crashed: false,
                        elapsed_secs: started.elapsed().as_secs_f64(),
                        reproduction_command: repro,
                    };
                }
                thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                return StressResult {
                    config,
                    max_simultaneous_workers: 0,
                    ops_completed: 0,
                    observed_checksum: 0,
                    expected_checksum: 0,
                    timed_out: false,
                    crashed: true,
                    elapsed_secs: started.elapsed().as_secs_f64(),
                    reproduction_command: repro,
                };
            }
        }
    }
}

/// Serve one strict production child request on stdin and write exactly one
/// JSON [`StressResult`] to stdout. Diagnostics are returned to the caller so
/// the binary can write them to stderr without corrupting the protocol.
pub fn run_stdio_child() -> Result<(), String> {
    let mut input = String::new();
    std::io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| format!("read stress-child request: {error}"))?;
    let request: StressChildRequest = serde_json::from_str(&input)
        .map_err(|error| format!("invalid stress-child request: {error}"))?;
    let result = run_child_request(request).map_err(|error| error.to_string())?;
    let response = serde_json::to_string(&result)
        .map_err(|error| format!("serialize stress-child response: {error}"))?;
    std::io::stdout()
        .write_all(response.as_bytes())
        .map_err(|error| format!("write stress-child response: {error}"))?;
    Ok(())
}

/// Entry-point for child-process mode.  Call this at the top of a test
/// function that checks `CHILD_ENV_VAR`.  Deserialises the scenario config
/// from the env var, runs it, prints the JSON result to stdout, and exits
/// the process.
pub fn run_child_mode() -> ! {
    let input_json = std::env::var(CHILD_ENV_VAR).expect("CHILD_ENV_VAR not set in child mode");
    let (config, scenario): (StressConfig, ScenarioType) =
        serde_json::from_str(&input_json).expect("deserialise child input");

    eprintln!(
        "[stress-harness child] scenario={} workers={} seed={}",
        config.name, config.worker_count, config.seed
    );

    match run_scenario(config, scenario) {
        Ok(result) => {
            println!(
                "{}",
                serde_json::to_string(&result).expect("serialise result")
            );
            std::process::exit(if result.crashed { 1 } else { 0 });
        }
        Err(error) => {
            eprintln!("[stress-harness child] rejected configuration: {error}");
            std::process::exit(1);
        }
    }
}
