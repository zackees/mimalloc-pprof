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
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------------------
// Configuration & result types
// ---------------------------------------------------------------------------

/// Configuration for a stress scenario run.
#[derive(Debug, Clone, Serialize, Deserialize)]
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
    /// Hard wall-clock limit for the run; exceeded → timed-out kill.
    #[serde(default)]
    pub max_duration_secs: Option<u64>,
}

fn default_alloc_min() -> usize {
    16
}
fn default_alloc_max() -> usize {
    4096
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

/// Run `scenario` in-process with the given config, returning a machine-readable
/// [`StressResult`].  All workers are barrier-synchronised so their allocation
/// work genuinely overlaps.
pub fn run_scenario(config: StressConfig, scenario: ScenarioType) -> StressResult {
    let started = Instant::now();
    let worker_count = config.worker_count.max(1);

    let simultaneous = Arc::new(AtomicUsize::new(0));
    let max_simultaneous = Arc::new(AtomicUsize::new(0));
    let ops_completed = Arc::new(AtomicU64::new(0));

    let ready_barrier = Arc::new(Barrier::new(worker_count));
    let running_barrier = Arc::new(Barrier::new(worker_count + 1)); // +1 for main

    let mut handles = Vec::with_capacity(worker_count);

    for wid in 0..worker_count {
        let cfg = config.clone();
        let sim = Arc::clone(&simultaneous);
        let max_sim = Arc::clone(&max_simultaneous);
        let ops = Arc::clone(&ops_completed);
        let rb = Arc::clone(&ready_barrier);
        let run_b = Arc::clone(&running_barrier);

        handles.push(thread::spawn(move || {
            // 1. All workers rendezvous — nobody starts before everyone is here.
            rb.wait();

            // 2. Atomically bump the "in flight" counter.
            let cur = sim.fetch_add(1, Ordering::SeqCst) + 1;
            max_sim.fetch_max(cur, Ordering::SeqCst);

            // 3. Signal main that we're past the counter increment.
            run_b.wait();

            // 4. Do the actual work.
            let per_worker = (cfg.operation_count / worker_count).max(1);
            let mut rng = SeededRng::new(cfg.seed.wrapping_add(wid as u64));

            match scenario {
                ScenarioType::AllocFree => {
                    for _ in 0..per_worker {
                        let sz = cfg.allocation_size_min
                            + rng.next_usize(
                                cfg.allocation_size_max - cfg.allocation_size_min + 1,
                            );
                        let _v = vec![0u8; sz];
                        ops.fetch_add(1, Ordering::Relaxed);
                    }
                }
                ScenarioType::Sawtooth => {
                    const BATCH: usize = 1024;
                    let mut held: Vec<Vec<u8>> = Vec::with_capacity(BATCH);
                    for i in 0..per_worker {
                        let sz = cfg.allocation_size_min
                            + rng.next_usize(
                                cfg.allocation_size_max - cfg.allocation_size_min + 1,
                            );
                        held.push(vec![0u8; sz]);
                        ops.fetch_add(1, Ordering::Relaxed);
                        if held.len() >= BATCH || i == per_worker - 1 {
                            held.clear();
                        }
                    }
                }
                ScenarioType::Hang => loop {
                    thread::sleep(Duration::from_secs(1));
                },
            }

            sim.fetch_sub(1, Ordering::SeqCst);
        }));
    }

    // 5. Wait for all workers to reach the post-increment barrier.
    running_barrier.wait();
    // Tiny grace period so the last straggler has time to register.
    thread::sleep(Duration::from_millis(5));

    // 6. Join all threads (or time out).
    let timed_out = if let Some(max_secs) = config.max_duration_secs {
        let deadline = started + Duration::from_secs(max_secs);
        let mut all_done = false;
        while !all_done && Instant::now() < deadline {
            all_done = handles.iter().all(|h| h.is_finished());
            if !all_done {
                thread::sleep(Duration::from_millis(10));
            }
        }
        !all_done
    } else {
        for h in handles {
            let _ = h.join();
        }
        false
    };

    let elapsed = started.elapsed().as_secs_f64();

    StressResult {
        config: config.clone(),
        max_simultaneous_workers: max_simultaneous.load(Ordering::SeqCst),
        ops_completed: ops_completed.load(Ordering::Relaxed),
        timed_out,
        crashed: false,
        elapsed_secs: elapsed,
        reproduction_command: format!(
            "cargo test -p stress-harness -- --nocapture --test-threads=1"
        ),
    }
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
    let input_json = serde_json::to_string(&(&config, scenario))
        .expect("serialise child input");

    // Build the reproduction command before we consume config.
    let repro = format!(
        "cargo test -p stress-harness -- child_process_isolation --nocapture --test-threads=1"
    );

    // Spawn the *same test binary* directly (not via `cargo test`) to avoid
    // lock-file collisions when a prior child was killed.
    let test_binary = std::env::current_exe()
        .unwrap_or_else(|_| std::path::PathBuf::from("stress-harness-test"));

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
                        if let Ok(result) =
                            serde_json::from_str::<StressResult>(line)
                        {
                            return result;
                        }
                    }
                }

                return StressResult {
                    config,
                    max_simultaneous_workers: 0,
                    ops_completed: 0,
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
                    timed_out: false,
                    crashed: true,
                    elapsed_secs: started.elapsed().as_secs_f64(),
                    reproduction_command: repro,
                };
            }
        }
    }
}

/// Entry-point for child-process mode.  Call this at the top of a test
/// function that checks `CHILD_ENV_VAR`.  Deserialises the scenario config
/// from the env var, runs it, prints the JSON result to stdout, and exits
/// the process.
pub fn run_child_mode() -> ! {
    let input_json =
        std::env::var(CHILD_ENV_VAR).expect("CHILD_ENV_VAR not set in child mode");
    let (config, scenario): (StressConfig, ScenarioType) =
        serde_json::from_str(&input_json).expect("deserialise child input");

    eprintln!(
        "[stress-harness child] scenario={} workers={} seed={}",
        config.name, config.worker_count, config.seed
    );

    let result = run_scenario(config, scenario);
    let json = serde_json::to_string(&result).expect("serialise result");
    println!("{}", json);
    std::process::exit(if result.crashed { 1 } else { 0 });
}
