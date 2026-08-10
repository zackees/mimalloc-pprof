//! PR-sentinel benchmark dashboard — runs 5 scenario cards against the
//! mimalloc-pprof fork, emits JSON + standalone HTML.
//!
//! Part of #171 "perf: publish a bounded four-allocator benchmark dashboard".

use bench_harness::{run_benchmark, BenchConfig, BenchResult, ScenarioType};
use mimalloc_pprof::MiMalloc;
use serde::Serialize;
use std::time::Duration;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

// ---------------------------------------------------------------------------
// Scenario cards — PR sentinel tier (5 cards)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
struct ScenarioCard {
    id: &'static str,
    name: &'static str,
    description: &'static str,
    worker_count: usize,
    operation_count: usize,
    allocation_size_min: usize,
    allocation_size_max: usize,
    scenario: ScenarioType,
}

const PR_SENTINEL_CARDS: &[ScenarioCard] = &[
    ScenarioCard {
        id: "tiny-hot-path",
        name: "Tiny hot path (16–64 B)",
        description: "Single-thread, small fixed-size allocations — exercises the fast path.",
        worker_count: 1,
        operation_count: 2_000_000,
        allocation_size_min: 16,
        allocation_size_max: 64,
        scenario: ScenarioType::AllocFree,
    },
    ScenarioCard {
        id: "small-mixed",
        name: "Small mixed (8–1024 B)",
        description: "Multi-thread, log-distributed sizes — exercises size-class lookup.",
        worker_count: 4,
        operation_count: 2_000_000,
        allocation_size_min: 8,
        allocation_size_max: 1024,
        scenario: ScenarioType::AllocFree,
    },
    ScenarioCard {
        id: "medium-mixed",
        name: "Medium mixed (4–64 KiB)",
        description: "Multi-thread, larger allocations — exercises arena and page-map paths.",
        worker_count: 4,
        operation_count: 200_000,
        allocation_size_min: 4096,
        allocation_size_max: 65536,
        scenario: ScenarioType::AllocFree,
    },
    ScenarioCard {
        id: "batched-lifo",
        name: "Batched lifetime (sawtooth)",
        description: "Accumulate then release in batches — exercises free-list churn.",
        worker_count: 4,
        operation_count: 2_000_000,
        allocation_size_min: 16,
        allocation_size_max: 1024,
        scenario: ScenarioType::Sawtooth,
    },
    ScenarioCard {
        id: "cross-thread-free",
        name: "Cross-thread free",
        description: "Allocate in one thread, drop in another — exercises remote-free paths.",
        worker_count: 4,
        operation_count: 500_000,
        allocation_size_min: 16,
        allocation_size_max: 4096,
        scenario: ScenarioType::AllocFree,
    },
];

// ---------------------------------------------------------------------------
// Card result (one card × one configuration)
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct CardResult {
    card_id: String,
    card_name: String,
    mean_throughput_ops_per_sec: f64,
    mean_ns_per_op: f64,
    cv_percent: f64,
    ops_completed: u64,
    elapsed_secs: f64,
    rounds: usize,
}

// ---------------------------------------------------------------------------
// Dashboard output
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct DashboardOutput {
    commit: String,
    rustc: String,
    target: String,
    cpu_model: String,
    timestamp: String,
    cards: Vec<CardResult>,
}

fn run_card(card: &ScenarioCard) -> BenchResult {
    let config = BenchConfig {
        name: format!("sentinel-{}", card.id),
        seed: 0x171171,
        worker_count: card.worker_count,
        operation_count: card.operation_count,
        allocation_size_min: card.allocation_size_min,
        allocation_size_max: card.allocation_size_max,
        warmup_rounds: 2,
        measurement_rounds: 5,
        max_duration_secs: Some(60),
        scenario: card.scenario,
        serialized: false,
    };
    run_benchmark(config)
}

fn main() {
    eprintln!("=== mimalloc-pprof PR sentinel benchmark ===");
    let start = std::time::Instant::now();

    let mut card_results: Vec<CardResult> = Vec::new();
    let mut commit = String::from("unknown");
    let mut rustc = String::from("unknown");
    let mut target = String::from("unknown");
    let mut cpu_model = String::from("unknown");
    let mut timestamp = String::from("unknown");

    for card in PR_SENTINEL_CARDS {
        eprintln!("\n--- {} ({}) ---", card.name, card.id);
        let result = run_card(card);

        // Capture metadata from the first card.
        if commit == "unknown" {
            commit = result.metadata.commit.clone();
            rustc = result.metadata.rustc.clone();
            target = result.metadata.target.clone();
            cpu_model = result.metadata.cpu_model.clone();
            timestamp = result.metadata.timestamp.clone();
        }

        let cr = CardResult {
            card_id: card.id.to_string(),
            card_name: card.name.to_string(),
            mean_throughput_ops_per_sec: result.summary.mean_throughput_ops_per_sec,
            mean_ns_per_op: result.summary.mean_ns_per_op,
            cv_percent: result.summary.cv_throughput_percent,
            ops_completed: result.summary.total_ops,
            elapsed_secs: result.samples.iter().map(|s| s.elapsed_secs).sum(),
            rounds: result.summary.rounds,
        };

        eprintln!(
            "  {:.0} ops/s ({:.1} ns/op), CV={:.1}%",
            cr.mean_throughput_ops_per_sec,
            cr.mean_ns_per_op,
            cr.cv_percent,
        );

        card_results.push(cr);
    }

    let elapsed = start.elapsed();
    eprintln!("\nTotal: {:.1}s", elapsed.as_secs_f64());

    let dashboard = DashboardOutput {
        commit,
        rustc,
        target,
        cpu_model,
        timestamp,
        cards: card_results,
    };

    // Emit JSON.
    let json = serde_json::to_string_pretty(&dashboard).unwrap();
    println!("{}", json);

    // Emit HTML.
    let html = generate_html(&dashboard, elapsed);
    let html_path = "benchmark-report.html";
    std::fs::write(html_path, &html).expect("write HTML");
    eprintln!("\nHTML report written to {html_path}");
}

// ---------------------------------------------------------------------------
// HTML generation
// ---------------------------------------------------------------------------

fn generate_html(d: &DashboardOutput, elapsed: Duration) -> String {
    let mut rows = String::new();
    for c in &d.cards {
        rows.push_str(&format!(
            r#"<tr>
    <td>{}</td>
    <td>{}</td>
    <td class="num">{:.0}</td>
    <td class="num">{:.1}</td>
    <td class="num">{:.1}</td>
</tr>
"#,
            c.card_id,
            c.card_name,
            c.mean_throughput_ops_per_sec,
            c.mean_ns_per_op,
            c.cv_percent,
        ));
    }

    format!(
        r#"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>mimalloc-pprof benchmark — PR sentinel</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #ddd; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .meta {{ font-size: 0.85em; color: #666; margin-bottom: 1.5em; }}
  .meta dt {{ font-weight: 600; display: inline; }}
  .meta dd {{ display: inline; margin: 0 1em 0 0.3em; }}
</style>
</head>
<body>
<h1>mimalloc-pprof — PR sentinel benchmark</h1>
<dl class="meta">
  <dt>Commit</dt><dd>{commit}</dd>
  <dt>Rustc</dt><dd>{rustc}</dd>
  <dt>Target</dt><dd>{target}</dd>
  <dt>CPU</dt><dd>{cpu_model}</dd>
  <dt>Timestamp</dt><dd>{timestamp}</dd>
  <dt>Wall time</dt><dd>{elapsed:.1}s</dd>
</dl>
<table>
<thead>
<tr>
  <th>Card</th>
  <th>Description</th>
  <th>Throughput (ops/s)</th>
  <th>Latency (ns/op)</th>
  <th>CV (%)</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
<p class="meta">Runner class: {runner_class}</p>
</body>
</html>"#,
        commit = d.commit,
        rustc = d.rustc,
        target = d.target,
        cpu_model = d.cpu_model,
        timestamp = d.timestamp,
        elapsed = elapsed.as_secs_f64(),
        rows = rows,
        runner_class = if std::env::var("CI").is_ok() {
            "GitHub-hosted (informational — not a stable reference host)"
        } else {
            "local"
        },
    )
}
