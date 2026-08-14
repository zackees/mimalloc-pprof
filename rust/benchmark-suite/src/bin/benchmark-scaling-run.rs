fn main() {
    if let Err(error) = benchmark_suite::scaling_runner::benchmark_scaling_run_main() {
        eprintln!("benchmark-scaling-run: {error}");
        std::process::exit(1);
    }
}
