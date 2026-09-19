use std::io::Write;
use std::path::{Path, PathBuf};

use benchmark_suite::model::LatestReport;
use benchmark_suite::pprof_tax::{
    attach_pprof_tax_report, build_pprof_tax_report, synthetic_pprof_tax_fixture,
    validate_manifest, validate_pprof_tax_report, validate_raw_run, PprofTaxManifest,
    PprofTaxRawRun,
};
use benchmark_suite::provenance::{sha256_bytes, sha256_file};

const FIXTURE_SEED: u64 = 0x7070_726f_665f_7461;

fn main() {
    if let Err(error) = run() {
        eprintln!("benchmark-pprof-tax-validate: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut arguments = std::env::args().skip(1);
    let mut input: Option<PathBuf> = None;
    let mut manifest_path: Option<PathBuf> = None;
    let mut raw_artifact_sha256: Option<String> = None;
    let mut raw_artifact_name: Option<String> = None;
    let mut base_latest: Option<PathBuf> = None;
    let mut report_out: Option<PathBuf> = None;
    let mut latest_out: Option<PathBuf> = None;
    let mut fixture_out: Option<PathBuf> = None;
    let mut fixture_latest_out: Option<PathBuf> = None;

    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--selftest" => return selftest(),
            "--help" | "-h" => {
                println!(
                    "usage: benchmark-pprof-tax-validate --selftest | --fixture-out <raw.json> | --fixture-latest-out <latest.json> | --input <raw.json> --manifest <manifest.json> --raw-artifact-sha256 <hex> --raw-artifact-name <name> --base-latest <latest.json> --report-out <path> --latest-out <path>"
                );
                return Ok(());
            }
            "--input" => input = Some(PathBuf::from(next_value(&mut arguments, "--input")?)),
            "--manifest" => {
                manifest_path = Some(PathBuf::from(next_value(&mut arguments, "--manifest")?))
            }
            "--raw-artifact-sha256" => {
                raw_artifact_sha256 = Some(next_value(&mut arguments, "--raw-artifact-sha256")?)
            }
            "--raw-artifact-name" => {
                raw_artifact_name = Some(next_value(&mut arguments, "--raw-artifact-name")?)
            }
            "--base-latest" => {
                base_latest = Some(PathBuf::from(next_value(&mut arguments, "--base-latest")?))
            }
            "--report-out" => {
                report_out = Some(PathBuf::from(next_value(&mut arguments, "--report-out")?))
            }
            "--latest-out" => {
                latest_out = Some(PathBuf::from(next_value(&mut arguments, "--latest-out")?))
            }
            "--fixture-out" => {
                fixture_out = Some(PathBuf::from(next_value(&mut arguments, "--fixture-out")?))
            }
            "--fixture-latest-out" => {
                fixture_latest_out = Some(PathBuf::from(next_value(
                    &mut arguments,
                    "--fixture-latest-out",
                )?))
            }
            _ => return Err(format!("unknown argument: {argument}")),
        }
    }

    if let Some(path) = fixture_out {
        let raw = synthetic_pprof_tax_fixture(FIXTURE_SEED)?;
        validate_raw_run(&raw)?;
        write_new_json(&path, &raw)?;
        println!(
            "PASS wrote a complete pprof-tax fixture with {} samples",
            raw.samples.len()
        );
        return Ok(());
    }

    if let Some(path) = fixture_latest_out {
        let (mut latest, report) = fixture_latest()?;
        attach_pprof_tax_report(&mut latest, report)?;
        write_new_json(&path, &latest)?;
        println!("PASS wrote a latest.json with an attached pprof-tax section");
        return Ok(());
    }

    let input = input.ok_or("--input is required")?;
    let manifest_path = manifest_path.ok_or("--manifest is required")?;
    let raw_artifact_sha256 = raw_artifact_sha256.ok_or("--raw-artifact-sha256 is required")?;
    let raw_artifact_name = raw_artifact_name.ok_or("--raw-artifact-name is required")?;
    let base_latest = base_latest.ok_or("--base-latest is required")?;
    let report_out = report_out.ok_or("--report-out is required")?;
    let latest_out = latest_out.ok_or("--latest-out is required")?;

    let observed_input_sha256 = sha256_file(&input)?;
    if observed_input_sha256 != raw_artifact_sha256 {
        return Err(format!(
            "{}: SHA-256 mismatch: expected {raw_artifact_sha256}, got {observed_input_sha256}",
            input.display()
        ));
    }
    let raw: PprofTaxRawRun = read_json(&input)?;
    let observed_manifest_sha256 = sha256_file(&manifest_path)?;
    if observed_manifest_sha256 != raw.configuration_manifest_sha256 {
        return Err(format!(
            "{}: manifest SHA-256 does not match the raw run's configuration_manifest_sha256",
            manifest_path.display()
        ));
    }
    let manifest: PprofTaxManifest = read_json(&manifest_path)?;
    if manifest != raw.manifest {
        return Err(
            "the --manifest file does not match the manifest embedded in the raw run".into(),
        );
    }

    let report = build_pprof_tax_report(&raw, &raw_artifact_sha256, &raw_artifact_name)?;
    validate_pprof_tax_report(&report)?;

    let mut latest: LatestReport = read_json(&base_latest)?;
    if latest.validation_report.status != "valid"
        || !latest.validation_report.headline_eligible
        || !latest.validation_report.errors.is_empty()
    {
        return Err("base latest report is not validated/headline-eligible".into());
    }
    attach_pprof_tax_report(&mut latest, report.clone())?;
    write_new_json(&report_out, &report)?;
    write_new_json(&latest_out, &latest)?;
    println!(
        "PASS validated pprof-tax report with {} blocks across {} cells; key={}",
        report.blocks,
        report.cells.len(),
        report.metric_comparison_key
    );
    Ok(())
}

/// Build the synthetic core `latest.json` plus a matching pprof-tax report,
/// ready to be attached. Shared by `--fixture-latest-out` and `selftest`.
fn fixture_latest() -> Result<
    (
        LatestReport,
        benchmark_suite::pprof_tax::PprofTaxMetricReport,
    ),
    String,
> {
    let core =
        benchmark_suite::validate::synthetic_full_fixture().map_err(|error| error.to_string())?;
    let validation = benchmark_suite::validate::validate_publication_raw(&core)
        .map_err(|error| error.to_string())?;
    let (latest, _history) = benchmark_suite::report::build_latest_report(&core, validation)?;
    let raw = synthetic_pprof_tax_fixture(FIXTURE_SEED)?;
    let raw_bytes = serde_json::to_vec(&raw).map_err(|error| error.to_string())?;
    let raw_artifact_sha256 = sha256_bytes(&raw_bytes);
    let report = build_pprof_tax_report(&raw, &raw_artifact_sha256, "pprof-tax-raw-run.json")?;
    Ok((latest, report))
}

/// Negative controls: a complete fixture must validate and publish, and each
/// declared impossible/invalid state must be rejected rather than silently
/// accepted.
fn selftest() -> Result<(), String> {
    let raw = synthetic_pprof_tax_fixture(FIXTURE_SEED)?;
    validate_raw_run(&raw)?;
    let raw_bytes = serde_json::to_vec(&raw).map_err(|error| error.to_string())?;
    let raw_artifact_sha256 = sha256_bytes(&raw_bytes);
    let report = build_pprof_tax_report(&raw, &raw_artifact_sha256, "pprof-tax-raw-run.json")?;
    validate_pprof_tax_report(&report)?;

    let (mut latest, attach_report) = fixture_latest()?;
    attach_pprof_tax_report(&mut latest, attach_report)
        .expect("a valid pprof-tax report must attach to a matching core latest report");

    // 1. Stale upstream commit (the issue's own bcee5a88, no longer the pin).
    let mut stale = raw.manifest.clone();
    stale.upstream_source_sha = "bcee5a88bcee5a88bcee5a88bcee5a88bcee5a88".into();
    if validate_manifest(&stale).is_ok() {
        return Err("selftest: a stale upstream commit was accepted".into());
    }

    // 2. MI_OPT_ARCH drift between fork-pprof-on and fork-pprof-off.
    let mut drifted = raw.manifest.clone();
    for entry in &mut drifted.compiled_configurations {
        if entry.compiled_configuration_id == "fork-pprof-on" {
            entry
                .cmake_cache
                .insert("MI_OPT_ARCH".into(), Some("ON".into()));
        }
    }
    if validate_manifest(&drifted).is_ok() {
        return Err("selftest: MI_OPT_ARCH drift between fork configurations was accepted".into());
    }

    // 3. Identity probe mismatch (executable digest disagrees with its entry).
    let mut bad_probe = raw.manifest.clone();
    bad_probe.compiled_configurations[0]
        .identity_probe
        .executable_sha256 = "0".repeat(64);
    if validate_manifest(&bad_probe).is_ok() {
        return Err("selftest: an identity probe mismatch was accepted".into());
    }

    // 4. A sample claiming pprof_active while pprof_compiled is false.
    let mut impossible = raw.clone();
    let index = impossible
        .samples
        .iter()
        .position(|sample| sample.configuration_id == "fork-pprof-off")
        .expect("fixture contains fork-pprof-off samples");
    impossible.samples[index].pprof_active = true;
    if benchmark_suite::pprof_tax::validate_raw_sample(&impossible.samples[index]).is_ok() {
        return Err("selftest: an active-but-not-compiled sample was accepted".into());
    }

    // 5. upstream-baseline claiming a sampling interval.
    let mut interval_on_baseline = raw.clone();
    let index = interval_on_baseline
        .samples
        .iter()
        .position(|sample| sample.configuration_id == "upstream-baseline")
        .expect("fixture contains upstream-baseline samples");
    interval_on_baseline.samples[index].sampling_interval_bytes = Some(4096);
    if benchmark_suite::pprof_tax::validate_raw_sample(&interval_on_baseline.samples[index]).is_ok()
    {
        return Err("selftest: upstream-baseline with a sampling interval was accepted".into());
    }

    // 6. A valid active sample missing its profile digest.
    let mut missing_digest = raw.clone();
    let index = missing_digest
        .samples
        .iter()
        .position(|sample| sample.configuration_id == "fork-pprof-sparse")
        .expect("fixture contains fork-pprof-sparse samples");
    missing_digest.samples[index].profile_sha256 = None;
    if benchmark_suite::pprof_tax::validate_raw_sample(&missing_digest.samples[index]).is_ok() {
        return Err(
            "selftest: a valid active sample missing its profile digest was accepted".into(),
        );
    }

    // 7. A stopped/inactive sample claiming profiler samples.
    let mut stopped_claims = raw.clone();
    let index = stopped_claims
        .samples
        .iter()
        .position(|sample| sample.configuration_id == "fork-pprof-off")
        .expect("fixture contains fork-pprof-off samples");
    stopped_claims.samples[index].sample_count = Some(5);
    if benchmark_suite::pprof_tax::validate_raw_sample(&stopped_claims.samples[index]).is_ok() {
        return Err("selftest: a stopped sample claiming profiler samples was accepted".into());
    }

    // 8. A report missing the frame-pointer-tax comparison.
    let mut no_frame_pointer = report.clone();
    no_frame_pointer
        .comparisons
        .retain(|comparison| comparison.comparison_id != "frame-pointer-tax");
    if validate_pprof_tax_report(&no_frame_pointer).is_ok() {
        return Err(
            "selftest: a report missing the frame-pointer-tax comparison was accepted".into(),
        );
    }

    // 9. Relabelling rate-1-stress as the headline.
    let mut relabelled = report.clone();
    let rate1_index = relabelled
        .comparisons
        .iter()
        .position(|comparison| comparison.comparison_id == "rate-1-stress")
        .expect("report always carries rate-1-stress");
    relabelled.comparisons[rate1_index].headline_eligible = true;
    relabelled.headline = relabelled.comparisons[rate1_index].clone();
    if validate_pprof_tax_report(&relabelled).is_ok() {
        return Err("selftest: relabelling rate-1-stress as the headline was accepted".into());
    }

    // 10. A 14-block "full" run (below the minimum paired-block floor).
    let mut short = raw.clone();
    short.blocks = 14;
    short.samples.retain(|sample| sample.block_id < 14);
    short.block_orders.truncate(14);
    if validate_raw_run(&short).is_ok() {
        return Err("selftest: a 14-block full-mode run was accepted".into());
    }

    // 11. Attaching a report whose upstream provenance does not match the
    // core latest report's own upstream-mimalloc pin.
    let (mut mismatched_latest, _report) = fixture_latest()?;
    for allocator in &mut mismatched_latest.allocators {
        if allocator.allocator_id == "upstream-mimalloc" {
            allocator.source_sha = "f".repeat(40);
        }
    }
    if attach_pprof_tax_report(&mut mismatched_latest, report.clone()).is_ok() {
        return Err("selftest: attaching with mismatched upstream provenance was accepted".into());
    }

    // 12. Attaching a report with an empty manifest digest.
    let mut empty_digest = report.clone();
    empty_digest.configuration_manifest_sha256 = String::new();
    let (mut latest_for_empty, _report) = fixture_latest()?;
    if attach_pprof_tax_report(&mut latest_for_empty, empty_digest).is_ok() {
        return Err(
            "selftest: attaching a report with an empty manifest digest was accepted".into(),
        );
    }

    println!("PASS benchmark-pprof-tax-validate selftest");
    Ok(())
}

fn next_value(arguments: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
    arguments
        .next()
        .ok_or_else(|| format!("{flag} requires a value"))
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, String> {
    let bytes = std::fs::read(path).map_err(|error| format!("{}: {error}", path.display()))?;
    serde_json::from_slice(&bytes).map_err(|error| format!("{}: {error}", path.display()))
}

fn write_new_json<T: serde::Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    serde_json::to_writer_pretty(&mut file, value)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    file.write_all(b"\n")
        .map_err(|error| format!("{}: {error}", path.display()))
}
