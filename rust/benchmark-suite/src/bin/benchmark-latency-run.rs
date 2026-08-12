fn main() {
    if let Err(error) = benchmark_suite::latency_runner::benchmark_latency_run_main() {
        eprintln!("benchmark-latency-run: {error}");
        std::process::exit(1);
    }
}
