//! Trustworthy Rust benchmark harness for allocator throughput/latency
//! measurement — Phase 0 of the #170 upstream-parity and profiler-optimization
//! program.
//!
//! # Design
//!
//! - Reuses the `stress-harness` crate for deterministic, barrier-synchronised
//!   concurrent workloads.
//! - Adds warmup / measurement round separation, per-round timing, and
//!   statistical aggregation (mean, std-dev, coefficient of variation).
//! - Records build + host metadata so every result is independently
//!   reproducible.
//! - Supports A/B comparison with a planted-slower control that the
//!   statistical comparison must reject.
//!
//! # Limitations (this phase)
//!
//! - Hardware counters (cycles, cache misses) require `perf` / PDB and are
//!   deferred to stable-reference-host runs.  RSS / commit measurement is
//!   best-effort (Linux `/proc/self/status`).
//! - GitHub-hosted runners are informational only; small timing wins need
//!   stable reference hardware per the #170 acceptance criteria.

use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};

// Re-export the stress harness types we build on.
pub use stress_harness::{ScenarioType, StressConfig};

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/// Extended benchmark configuration — extends [`StressConfig`] with timing
/// controls.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchConfig {
    /// Human-readable benchmark name.
    pub name: String,
    /// Deterministic seed (passed through to every round).
    pub seed: u64,
    /// Number of native OS threads.
    pub worker_count: usize,
    /// Total allocation operations per round.
    pub operation_count: usize,
    /// Allocation size range `[min, max]` bytes.
    #[serde(default = "default_alloc_min")]
    pub allocation_size_min: usize,
    #[serde(default = "default_alloc_max")]
    pub allocation_size_max: usize,
    /// Number of warmup rounds (results discarded).
    #[serde(default = "default_warmup")]
    pub warmup_rounds: usize,
    /// Number of measurement rounds.
    #[serde(default = "default_measure_rounds")]
    pub measurement_rounds: usize,
    /// Hard wall-clock limit per round.
    #[serde(default)]
    pub max_duration_secs: Option<u64>,
    /// Workload shape.
    pub scenario: ScenarioType,
    /// If true, serialise all worker allocations through a global [`std::sync::Mutex`]
    /// — the planted-slower control.
    #[serde(default)]
    pub serialized: bool,
}

fn default_alloc_min() -> usize {
    16
}
fn default_alloc_max() -> usize {
    256
}
fn default_warmup() -> usize {
    2
}
fn default_measure_rounds() -> usize {
    5
}

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------

/// Build + host metadata captured at benchmark time so results are
/// independently reproducible.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchMetadata {
    /// Git commit hash of the build.
    pub commit: String,
    /// `rustc --version` output.
    pub rustc: String,
    /// Cargo profile (`debug` / `release`).
    pub profile: String,
    /// Whether `MI_PPROF=ON` (best-effort from env).
    pub mi_pprof: Option<String>,
    /// `$CARGO_CFG_TARGET_ARCH`-`$CARGO_CFG_TARGET_OS`
    pub target: String,
    /// Host CPU model string (best-effort).
    pub cpu_model: String,
    /// OS name + version.
    pub os: String,
    /// ISO-8601 timestamp.
    pub timestamp: String,
}

impl BenchMetadata {
    /// Capture everything we can from the current environment.
    pub fn capture() -> Self {
        let commit = std::process::Command::new("git")
            .args(["rev-parse", "--short", "HEAD"])
            .output()
            .ok()
            .filter(|o| o.status.success())
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".into());

        let rustc = std::process::Command::new("rustc")
            .arg("--version")
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "unknown".into());

        let profile = if cfg!(debug_assertions) {
            "debug"
        } else {
            "release"
        }
        .into();

        let mi_pprof = std::env::var("MI_PPROF").ok();

        let target = format!("{}-{}", std::env::consts::ARCH, std::env::consts::OS);

        let cpu_model = sys_cpu_model();
        let os = sys_os_string();

        let timestamp = chrono_now();

        Self {
            commit,
            rustc,
            profile,
            mi_pprof,
            target,
            cpu_model,
            os,
            timestamp,
        }
    }
}

fn sys_cpu_model() -> String {
    #[cfg(target_os = "linux")]
    {
        std::fs::read_to_string("/proc/cpuinfo")
            .ok()
            .and_then(|s| {
                s.lines()
                    .find(|l| l.starts_with("model name"))
                    .map(|l| l.splitn(2, ':').nth(1).unwrap_or("").trim().to_string())
            })
            .unwrap_or_else(|| "unknown".into())
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("sysctl")
            .args(["-n", "machdep.cpu.brand_string"])
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "unknown".into())
    }
    #[cfg(target_os = "windows")]
    {
        std::env::var("PROCESSOR_IDENTIFIER").unwrap_or_else(|_| "unknown".into())
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        "unknown".into()
    }
}

fn sys_os_string() -> String {
    format!("{} {}", std::env::consts::OS, std::env::consts::ARCH)
}

fn chrono_now() -> String {
    // Avoid pulling in `chrono` — use a minimal ISO-8601 approximation.
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Convert to a rough UTC YYYY-MM-DDTHH:MM:SSZ
    let days = secs / 86400;
    let time_of_day = secs % 86400;
    let h = time_of_day / 3600;
    let m = (time_of_day % 3600) / 60;
    let s = time_of_day % 60;

    // Days since 1970-01-01 to Y/M/D using a simple civil-date formula.
    let (y, mo, d) = civil_from_days(days as i64);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{m:02}:{s:02}Z")
}

/// Convert days since 1970-01-01 to (year, month, day).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    // Howard Hinnant's algorithm
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (y + if m <= 2 { 1 } else { 0 }, m, d)
}

// ---------------------------------------------------------------------------
// Per-round sample
// ---------------------------------------------------------------------------

/// One timed measurement round.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchSample {
    pub round: usize,
    pub ops_completed: u64,
    pub elapsed_secs: f64,
    pub throughput_ops_per_sec: f64,
    pub throughput_ns_per_op: f64,
}

// ---------------------------------------------------------------------------
// Aggregate summary
// ---------------------------------------------------------------------------

/// Statistical summary across all measurement rounds.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchSummary {
    pub rounds: usize,
    pub total_ops: u64,
    pub mean_throughput_ops_per_sec: f64,
    pub std_throughput_ops_per_sec: f64,
    pub cv_throughput_percent: f64,
    pub min_throughput_ops_per_sec: f64,
    pub max_throughput_ops_per_sec: f64,
    pub mean_ns_per_op: f64,
}

// ---------------------------------------------------------------------------
// Full result
// ---------------------------------------------------------------------------

/// The top-level result of a benchmark run.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchResult {
    pub config: BenchConfig,
    pub metadata: BenchMetadata,
    pub samples: Vec<BenchSample>,
    pub summary: BenchSummary,
    pub reproduction_command: String,
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

/// Run a full benchmark (warmup + measurement) and return aggregated results.
pub fn run_benchmark(config: BenchConfig) -> BenchResult {
    let metadata = BenchMetadata::capture();
    let total_rounds = config.warmup_rounds + config.measurement_rounds;
    let mut samples = Vec::with_capacity(config.measurement_rounds);

    for round in 0..total_rounds {
        let is_warmup = round < config.warmup_rounds;
        let round_start = Instant::now();

        let stress_cfg = StressConfig {
            name: format!("{}-round-{}", config.name, round),
            seed: config.seed.wrapping_add(round as u64),
            worker_count: config.worker_count,
            operation_count: config.operation_count,
            allocation_size_min: config.allocation_size_min,
            allocation_size_max: config.allocation_size_max,
            max_duration_secs: config.max_duration_secs,
        };

        let stress_result = if config.serialized {
            // Planted-slower control: force all allocations through a Mutex.
            run_serialized(stress_cfg, config.scenario)
        } else {
            stress_harness::run_scenario(stress_cfg, config.scenario)
        };

        let elapsed = round_start.elapsed().as_secs_f64();
        let ops = stress_result.ops_completed;

        if !is_warmup {
            let tput = if elapsed > 0.0 {
                ops as f64 / elapsed
            } else {
                f64::INFINITY
            };
            samples.push(BenchSample {
                round: round - config.warmup_rounds,
                ops_completed: ops,
                elapsed_secs: elapsed,
                throughput_ops_per_sec: tput,
                throughput_ns_per_op: if ops > 0 {
                    (elapsed * 1e9) / ops as f64
                } else {
                    f64::INFINITY
                },
            });
        }
    }

    let summary = compute_summary(&samples);
    let repro = format!(
        "cargo test -p bench-harness --release -- --nocapture --test-threads=1"
    );

    BenchResult {
        config,
        metadata,
        samples,
        summary,
        reproduction_command: repro,
    }
}

// ---------------------------------------------------------------------------
// Planted-slower (serialized) path
// ---------------------------------------------------------------------------

/// Run the same workload shape but force every allocation through a global
/// [`std::sync::Mutex`], effectively serialising all workers.  This is the
/// planted-slower control that any statistical comparison must reject.
fn run_serialized(
    config: StressConfig,
    scenario: ScenarioType,
) -> stress_harness::StressResult {
    use std::sync::{Arc, Barrier, Mutex};
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
    use std::thread;

    let worker_count = config.worker_count.max(1);
    let ops_completed = Arc::new(AtomicU64::new(0));
    let simultaneous = Arc::new(AtomicUsize::new(0));
    let max_simultaneous = Arc::new(AtomicUsize::new(0));
    let serial_lock = Arc::new(Mutex::new(()));

    let ready = Arc::new(Barrier::new(worker_count));
    let running = Arc::new(Barrier::new(worker_count + 1));

    let mut handles = Vec::with_capacity(worker_count);
    for wid in 0..worker_count {
        let cfg = config.clone();
        let ops = Arc::clone(&ops_completed);
        let sim = Arc::clone(&simultaneous);
        let max_sim = Arc::clone(&max_simultaneous);
        let rb = Arc::clone(&ready);
        let run_b = Arc::clone(&running);
        let lock = Arc::clone(&serial_lock);

        handles.push(thread::spawn(move || {
            rb.wait();
            let cur = sim.fetch_add(1, Ordering::SeqCst) + 1;
            max_sim.fetch_max(cur, Ordering::SeqCst);
            run_b.wait();

            let per_worker = (cfg.operation_count / worker_count).max(1);
            let mut rng = stress_harness::SeededRng::new(
                cfg.seed.wrapping_add(wid as u64),
            );

            match scenario {
                ScenarioType::AllocFree => {
                    for _ in 0..per_worker {
                        let _guard = lock.lock().unwrap();
                        let sz = cfg.allocation_size_min
                            + rng.next_usize(
                                cfg.allocation_size_max
                                    - cfg.allocation_size_min
                                    + 1,
                            );
                        let v = vec![0u8; sz];
                        drop(v); // alloc + free inside the lock
                        drop(_guard);
                        ops.fetch_add(1, Ordering::Relaxed);
                    }
                }
                ScenarioType::Sawtooth => {
                    const BATCH: usize = 1024;
                    let mut held: Vec<Vec<u8>> = Vec::with_capacity(BATCH);
                    for i in 0..per_worker {
                        let _guard = lock.lock().unwrap();
                        let sz = cfg.allocation_size_min
                            + rng.next_usize(
                                cfg.allocation_size_max
                                    - cfg.allocation_size_min
                                    + 1,
                            );
                        held.push(vec![0u8; sz]);
                        drop(_guard);
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

    // Main thread waits on the same barrier so all workers proceed.
    running.wait();
    std::thread::sleep(Duration::from_millis(5));

    for h in handles {
        let _ = h.join();
    }

    stress_harness::StressResult {
        config,
        max_simultaneous_workers: max_simultaneous.load(Ordering::SeqCst),
        ops_completed: ops_completed.load(Ordering::Relaxed),
        timed_out: false,
        crashed: false,
        elapsed_secs: 0.0, // measured externally
        reproduction_command: String::new(),
    }
}

// ---------------------------------------------------------------------------
// Statistics
// ---------------------------------------------------------------------------

fn compute_summary(samples: &[BenchSample]) -> BenchSummary {
    let n = samples.len();
    if n == 0 {
        return BenchSummary {
            rounds: 0,
            total_ops: 0,
            mean_throughput_ops_per_sec: 0.0,
            std_throughput_ops_per_sec: 0.0,
            cv_throughput_percent: 0.0,
            min_throughput_ops_per_sec: 0.0,
            max_throughput_ops_per_sec: 0.0,
            mean_ns_per_op: 0.0,
        };
    }

    let total_ops: u64 = samples.iter().map(|s| s.ops_completed).sum();
    let tputs: Vec<f64> = samples.iter().map(|s| s.throughput_ops_per_sec).collect();
    let mean = tputs.iter().sum::<f64>() / n as f64;
    let variance = tputs.iter().map(|t| (t - mean).powi(2)).sum::<f64>() / n as f64;
    let std_dev = variance.sqrt();
    let cv = if mean > 0.0 {
        (std_dev / mean) * 100.0
    } else {
        0.0
    };
    let min_tput = tputs.iter().cloned().fold(f64::INFINITY, f64::min);
    let max_tput = tputs.iter().cloned().fold(0.0_f64, f64::max);
    let mean_ns = if mean > 0.0 { 1e9 / mean } else { f64::INFINITY };

    BenchSummary {
        rounds: n,
        total_ops,
        mean_throughput_ops_per_sec: mean,
        std_throughput_ops_per_sec: std_dev,
        cv_throughput_percent: cv,
        min_throughput_ops_per_sec: min_tput,
        max_throughput_ops_per_sec: max_tput,
        mean_ns_per_op: mean_ns,
    }
}

/// Returns `true` when the A/B comparison confidently identifies `slower` as
/// meaningfully slower than `baseline`.  Uses Welch's t-test at the given
/// significance level (default 0.01).
pub fn is_significantly_slower(
    baseline: &BenchResult,
    slower: &BenchResult,
    alpha: f64,
) -> bool {
    let a: Vec<f64> = baseline
        .samples
        .iter()
        .map(|s| s.throughput_ops_per_sec)
        .collect();
    let b: Vec<f64> = slower
        .samples
        .iter()
        .map(|s| s.throughput_ops_per_sec)
        .collect();

    if a.len() < 2 || b.len() < 2 {
        // Not enough samples for a t-test; fall back to ratio comparison.
        let mean_a = a.iter().sum::<f64>() / a.len() as f64;
        let mean_b = b.iter().sum::<f64>() / b.len() as f64;
        return mean_b < mean_a * 0.9;
    }

    let (mean_a, var_a) = mean_var(&a);
    let (mean_b, var_b) = mean_var(&b);

    // Welch's t-statistic
    let se = (var_a / a.len() as f64 + var_b / b.len() as f64).sqrt();
    if se == 0.0 {
        return false;
    }
    let t = (mean_a - mean_b) / se;

    // Welch-Satterthwaite degrees of freedom
    let df_num = (var_a / a.len() as f64 + var_b / b.len() as f64).powi(2);
    let df_den = (var_a / a.len() as f64).powi(2) / (a.len() - 1) as f64
        + (var_b / b.len() as f64).powi(2) / (b.len() - 1) as f64;
    let df = if df_den > 0.0 { df_num / df_den } else { 1.0 };

    // Critical value for one-tailed test at alpha.
    // Use a simple approximation for the t-distribution critical value.
    let crit = t_critical_value(df, alpha);

    t > crit && mean_b < mean_a
}

fn mean_var(xs: &[f64]) -> (f64, f64) {
    let n = xs.len() as f64;
    let mean = xs.iter().sum::<f64>() / n;
    let var = xs.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0);
    (mean, var)
}

/// Approximate the critical value of Student's t-distribution
/// for a one-tailed test at the given significance level.
/// Uses a simple rational approximation valid for df ≥ 1.
fn t_critical_value(df: f64, alpha: f64) -> f64 {
    if df <= 0.0 {
        return 2.326; // z-score for alpha=0.01
    }
    // Use the inverse CDF approximation from Abramowitz & Stegun 26.7.5
    let zp = z_score(alpha);
    let zp2 = zp * zp;
    let zp3 = zp2 * zp;
    let zp5 = zp3 * zp2;
    let t = zp
        + (zp3 + zp) / (4.0 * df)
        + (5.0 * zp5 + 16.0 * zp3 + 3.0 * zp) / (96.0 * df * df)
        + (3.0 * zp5 * zp2 + 19.0 * zp5 + 17.0 * zp3 - 15.0 * zp) / (384.0 * df * df * df);
    t
}

/// Inverse CDF of the standard normal distribution (probit approximation).
fn z_score(p: f64) -> f64 {
    // Peter J. Acklam's algorithm for the inverse normal CDF.
    if p <= 0.0 {
        return -8.0;
    }
    if p >= 1.0 {
        return 8.0;
    }
    // For one-tailed test with alpha, we want z for (1 - alpha).
    let p = 1.0 - p;

    let a1 = -39.6968302866538;
    let a2 = 220.9460984245205;
    let a3 = -275.9285104469687;
    let a4 = 138.3577518672690;
    let a5 = -30.66479806614716;
    let a6 = 2.506628277459239;

    let b1 = -54.47609879822406;
    let b2 = 161.5858368580409;
    let b3 = -155.6989798598866;
    let b4 = 66.80131188771972;
    let b5 = -13.28068155288572;

    let c1 = -7.784894002430293e-3;
    let c2 = -0.3223964580411365;
    let c3 = -2.400758277161838;
    let c4 = -2.549732539343734;
    let c5 = 4.374664141464968;
    let c6 = 2.938163982698783;

    let d1 = 7.784695709041462e-3;
    let d2 = 0.3224671290700398;
    let d3 = 2.445134137142996;
    let d4 = 3.754408661907416;

    let p_low = 0.02425;
    let p_high = 1.0 - p_low;

    if p < p_low {
        let q = (-2.0 * p.ln()).sqrt();
        (((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6)
            / ((((d1 * q + d2) * q + d3) * q + d4) * q + 1.0)
    } else if p <= p_high {
        let q = p - 0.5;
        let r = q * q;
        (((((a1 * r + a2) * r + a3) * r + a4) * r + a5) * r + a6) * q
            / (((((b1 * r + b2) * r + b3) * r + b4) * r + b5) * r + 1.0)
    } else {
        let q = (-2.0 * (1.0 - p).ln()).sqrt();
        -(((((c1 * q + c2) * q + c3) * q + c4) * q + c5) * q + c6)
            / ((((d1 * q + d2) * q + d3) * q + d4) * q + 1.0)
    }
}
