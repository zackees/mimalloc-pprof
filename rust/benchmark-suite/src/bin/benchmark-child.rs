fn main() {
    if let Err(error) = benchmark_suite::child::benchmark_child_main() {
        eprintln!("benchmark-child rejected request: {error}");
        std::process::exit(2);
    }
}
