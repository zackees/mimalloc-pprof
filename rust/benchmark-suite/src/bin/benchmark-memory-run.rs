fn main() {
    if let Err(error) = benchmark_suite::memory_runner::benchmark_memory_run_main() {
        eprintln!("benchmark-memory-run: {error}");
        std::process::exit(1);
    }
}
