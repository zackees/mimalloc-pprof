fn main() {
    if let Err(error) = benchmark_suite::runner::benchmark_run_main() {
        eprintln!("benchmark-run failed: {error}");
        std::process::exit(2);
    }
}
