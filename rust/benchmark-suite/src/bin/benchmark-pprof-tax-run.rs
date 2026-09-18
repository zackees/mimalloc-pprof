fn main() {
    if let Err(error) = benchmark_suite::pprof_tax_runner::benchmark_pprof_tax_run_main() {
        eprintln!("benchmark-pprof-tax-run: {error}");
        std::process::exit(1);
    }
}
