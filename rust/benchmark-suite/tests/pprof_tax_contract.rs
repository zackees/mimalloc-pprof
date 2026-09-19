//! RED->GREEN contract tests for the pprof-tax-v1 data contract (issue #187).
//!
//! Every fixture here is planted/synthetic; nothing asserts that any real
//! configuration is faster or slower than another on real hardware.

use std::collections::BTreeSet;

use benchmark_suite::model::LatestReport;
use benchmark_suite::pprof_tax::{
    attach_pprof_tax_report, block_orders, build_pprof_tax_report, classify_sample,
    latency_overhead, synthetic_pprof_tax_fixture, throughput_overhead, validate_block_orders,
    validate_manifest, validate_pprof_tax_report, validate_raw_run, validate_raw_sample,
    PprofTaxRawRun, PprofTaxRawSample, PPROF_TAX_COMPILED_CONFIGURATION_IDS,
    PPROF_TAX_CONFIGURATIONS, PPROF_TAX_CONFIGURATION_IDS, PPROF_TAX_MIN_BLOCKS,
};
use benchmark_suite::provenance::sha256_bytes;
use benchmark_suite::report::build_latest_report;
use benchmark_suite::stats::{summarize_paired, MetricDirection, MetricObservation};
use benchmark_suite::validate::{synthetic_full_fixture, validate_publication_raw};

const FIXTURE_SEED: u64 = 0x7070_726f_665f_7461;

fn fixture() -> PprofTaxRawRun {
    synthetic_pprof_tax_fixture(FIXTURE_SEED).expect("synthetic pprof-tax fixture builds")
}

fn report_from(raw: &PprofTaxRawRun) -> benchmark_suite::pprof_tax::PprofTaxMetricReport {
    let bytes = serde_json::to_vec(raw).unwrap();
    let sha = sha256_bytes(&bytes);
    build_pprof_tax_report(raw, &sha, "pprof-tax-raw-run.json").expect("fixture report builds")
}

/// A minimally complete, self-consistent active sample (`fork-pprof-aggressive`)
/// that `validate_raw_sample` accepts unmodified. Tests tamper with a clone.
fn base_active_sample() -> PprofTaxRawSample {
    PprofTaxRawSample {
        block_id: 0,
        position: 0,
        configuration_id: "fork-pprof-aggressive".into(),
        compiled_configuration_id: "fork-pprof-on".into(),
        configuration_manifest_sha256: "a".repeat(64),
        executable_sha256: "b".repeat(64),
        pprof_compiled: true,
        pprof_active: true,
        sampling_interval_bytes: Some(4096),
        frame_pointer_policy: "cmake-mi-pprof-implicit".into(),
        scenario_id: "tiny-fixed-64".into(),
        thread_point: "1".into(),
        thread_count: 1,
        operations_per_worker: 100,
        workload_seed: 1,
        throughput_operations_per_second: Some(1000.0),
        elapsed_ns: Some(1_000_000),
        operation_count: Some(1000),
        allocation_calls: Some(1000),
        checksum: Some(1),
        allocated_bytes_lower_bound: Some(1000),
        peak_rss_bytes: Some(1024),
        end_rss_delta_bytes: Some(0),
        profile_path: Some("/tmp/profile".into()),
        profile_sha256: Some("c".repeat(64)),
        profile_size_bytes: Some(100),
        sample_count: Some(1),
        sampled_bytes: Some(4096),
        dropped_records: Some(0),
        profiler_arena_bytes: Some(1024),
        interval_confirmed: Some(4096),
        timed_out: false,
        exit_code: Some(0),
        validity_status: "valid".into(),
        invalid_reason: None,
    }
}

/// Mark every sample for `configuration_id` in the run as an invalid, clean
/// timeout, so `validate_raw_run`'s "exactly one sample per slot" invariant
/// stays satisfied while it drops out of every paired comparison.
fn invalidate_configuration(raw: &mut PprofTaxRawRun, configuration_id: &str) {
    for sample in raw
        .samples
        .iter_mut()
        .filter(|sample| sample.configuration_id == configuration_id)
    {
        sample.validity_status = "invalid".into();
        sample.invalid_reason = Some("timeout".into());
        sample.timed_out = true;
        sample.throughput_operations_per_second = None;
        sample.elapsed_ns = None;
        sample.operation_count = None;
        sample.allocation_calls = None;
        sample.profile_path = None;
        sample.profile_sha256 = None;
        sample.profile_size_bytes = None;
        sample.sample_count = None;
        sample.sampled_bytes = None;
        sample.dropped_records = None;
        sample.profiler_arena_bytes = None;
        sample.interval_confirmed = None;
    }
}

// (1) Exactly the seven stable IDs in order, plus the four compiled IDs.
#[test]
fn seven_configuration_ids_and_four_compiled_ids_are_stable_and_ordered() {
    assert_eq!(
        PPROF_TAX_CONFIGURATION_IDS,
        [
            "upstream-baseline",
            "fork-pprof-off",
            "fork-pprof-on-stopped",
            "fork-pprof-off-frame-pointers",
            "fork-pprof-sparse",
            "fork-pprof-aggressive",
            "fork-pprof-rate-1-stress",
        ]
    );
    assert_eq!(
        PPROF_TAX_COMPILED_CONFIGURATION_IDS,
        [
            "upstream-baseline",
            "fork-pprof-off",
            "fork-pprof-on",
            "fork-pprof-off-frame-pointers",
        ]
    );
    assert_eq!(PPROF_TAX_CONFIGURATIONS.len(), 7);
    for (id, spec) in PPROF_TAX_CONFIGURATION_IDS
        .iter()
        .zip(PPROF_TAX_CONFIGURATIONS.iter())
    {
        assert_eq!(*id, spec.configuration_id);
    }
}

// (2) Stale upstream commit rejected.
#[test]
fn stale_upstream_commit_is_rejected() {
    let raw = fixture();
    let mut manifest = raw.manifest.clone();
    manifest.upstream_source_sha = "bcee5a88bcee5a88bcee5a88bcee5a88bcee5a88".into();
    let error = validate_manifest(&manifest).expect_err("a stale upstream commit must be rejected");
    assert!(error.contains("upstream"), "{error}");
}

// (3) Non-allowlisted flag drift across fork configs rejected; the two
// allowlisted toggles (MI_PPROF, the exact frame-pointer flag) still pass.
#[test]
fn non_allowlisted_flag_drift_between_fork_configurations_is_rejected() {
    let raw = fixture();
    assert!(validate_manifest(&raw.manifest).is_ok());

    let mut arch_drift = raw.manifest.clone();
    for entry in &mut arch_drift.compiled_configurations {
        if entry.compiled_configuration_id == "fork-pprof-on" {
            entry
                .cmake_cache
                .insert("MI_OPT_ARCH".into(), Some("ON".into()));
        }
    }
    assert!(validate_manifest(&arch_drift).is_err());

    let mut release_drift = raw.manifest.clone();
    for entry in &mut release_drift.compiled_configurations {
        if entry.compiled_configuration_id == "fork-pprof-on" {
            entry
                .cmake_cache
                .insert("CMAKE_C_FLAGS_RELEASE".into(), Some("-O2".into()));
        }
    }
    assert!(validate_manifest(&release_drift).is_err());
}

// (4) Identity probe mismatch rejected (config id, exe sha, pprof_compiled).
#[test]
fn identity_probe_mismatch_is_rejected() {
    let raw = fixture();

    let mut bad_config_id = raw.manifest.clone();
    bad_config_id.compiled_configurations[0]
        .identity_probe
        .configuration_id = "fork-pprof-off".into();
    assert!(validate_manifest(&bad_config_id).is_err());

    let mut bad_exe = raw.manifest.clone();
    bad_exe.compiled_configurations[0]
        .identity_probe
        .executable_sha256 = "0".repeat(64);
    assert!(validate_manifest(&bad_exe).is_err());

    let mut bad_compiled = raw.manifest.clone();
    let flipped = !bad_compiled.compiled_configurations[0]
        .identity_probe
        .pprof_compiled;
    bad_compiled.compiled_configurations[0]
        .identity_probe
        .pprof_compiled = flipped;
    assert!(validate_manifest(&bad_compiled).is_err());
}

// (7) classify_sample: zero samples after crossing the interval is invalid;
// zero samples below the crossing threshold stays valid.
#[test]
fn classify_sample_flags_zero_after_crossing_but_not_below_threshold() {
    let mut crossed = base_active_sample();
    crossed.sample_count = Some(0);
    crossed.sampling_interval_bytes = Some(4096);
    crossed.allocated_bytes_lower_bound = Some(4096 * 20);
    classify_sample(&mut crossed, false);
    assert_eq!(crossed.validity_status, "invalid");
    assert_eq!(
        crossed.invalid_reason.as_deref(),
        Some("zero-samples-after-crossing-interval")
    );

    let mut below = base_active_sample();
    below.sample_count = Some(0);
    below.sampling_interval_bytes = Some(4096);
    below.allocated_bytes_lower_bound = Some(4096 * 19);
    classify_sample(&mut below, false);
    assert_eq!(below.validity_status, "valid");
    assert!(below.invalid_reason.is_none());
}

// (8) Impossible compiled/active/interval combinations fail validate_raw_sample;
// serde rejects unknown fields.
#[test]
fn impossible_states_fail_validation_and_serde_rejects_unknown_fields() {
    let mut active_not_compiled = base_active_sample();
    active_not_compiled.configuration_id = "fork-pprof-off".into();
    active_not_compiled.compiled_configuration_id = "fork-pprof-off".into();
    active_not_compiled.pprof_compiled = false;
    assert!(validate_raw_sample(&active_not_compiled).is_err());

    let mut interval_on_upstream = base_active_sample();
    interval_on_upstream.configuration_id = "upstream-baseline".into();
    interval_on_upstream.compiled_configuration_id = "upstream-baseline".into();
    interval_on_upstream.pprof_compiled = false;
    interval_on_upstream.pprof_active = false;
    interval_on_upstream.frame_pointer_policy = "omitted".into();
    // sampling_interval_bytes stays Some(4096): impossible for upstream-baseline.
    assert!(validate_raw_sample(&interval_on_upstream).is_err());

    let value = serde_json::to_value(base_active_sample()).unwrap();
    let mut object = value.as_object().unwrap().clone();
    object.insert("unexpected_field".into(), serde_json::json!(true));
    let text = serde_json::to_string(&object).unwrap();
    assert!(serde_json::from_str::<PprofTaxRawSample>(&text).is_err());
}

// (16) Active raw samples missing dropped_records or profiler_arena_bytes are
// rejected.
#[test]
fn active_samples_missing_dropped_records_or_arena_bytes_are_rejected() {
    let mut missing_dropped = base_active_sample();
    missing_dropped.dropped_records = None;
    assert!(validate_raw_sample(&missing_dropped).is_err());

    let mut missing_arena = base_active_sample();
    missing_arena.profiler_arena_bytes = None;
    assert!(validate_raw_sample(&missing_arena).is_err());
}

// (9) The report always contains frame-pointer-tax; its omission is rejected.
#[test]
fn report_always_carries_frame_pointer_tax_and_omission_is_rejected() {
    let raw = fixture();
    let report = report_from(&raw);
    let frame_pointer = report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "frame-pointer-tax")
        .expect("frame-pointer-tax must always be present");
    assert!(matches!(
        frame_pointer.support_status.as_str(),
        "supported" | "unsupported"
    ));
    validate_pprof_tax_report(&report).expect("a complete report must validate");

    let mut without = report;
    without
        .comparisons
        .retain(|comparison| comparison.comparison_id != "frame-pointer-tax");
    assert!(validate_pprof_tax_report(&without).is_err());
}

// (10) Directional fixture: the planted sparse candidate is slower than its
// reference, so overhead must be positive and the two overhead helpers must
// disagree in sign.
#[test]
fn planted_slower_candidate_yields_positive_overhead_with_correct_signs() {
    let raw = fixture();
    let report = report_from(&raw);
    let sparse = report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "sparse-sampling-tax")
        .unwrap();
    assert_eq!(sparse.support_status, "supported");
    let ratio = sparse.ratio.unwrap();
    assert!(ratio < 1.0, "planted sparse throughput must be slower");
    assert!(throughput_overhead(ratio) > 0.0);
    assert!((sparse.overhead.unwrap() - throughput_overhead(ratio)).abs() < 1e-12);
    assert!(latency_overhead(ratio) < 0.0);
}

// (11a) Whole-block pairing treats the block as the indivisible unit:
// duplicating within-block operation volume does not change block_count.
#[test]
fn whole_block_pairing_is_insensitive_to_within_block_operation_volume() {
    let observations = vec![
        MetricObservation {
            block_id: 0,
            allocator_id: "candidate".into(),
            value: 100.0,
        },
        MetricObservation {
            block_id: 0,
            allocator_id: "reference".into(),
            value: 90.0,
        },
    ];
    let summary = summarize_paired(
        1,
        "cell",
        "candidate",
        "reference",
        MetricDirection::HigherIsBetter,
        &observations,
    )
    .unwrap();
    assert_eq!(summary.block_count, 1);
}

// (11b) Below the minimum paired-block floor, numeric fields are null and the
// comparison is never `supported`.
#[test]
fn below_minimum_paired_blocks_yields_null_numbers_and_a_non_supported_status() {
    let mut raw = fixture();
    let index = raw
        .samples
        .iter()
        .position(|sample| {
            sample.configuration_id == "fork-pprof-aggressive" && sample.block_id == 0
        })
        .unwrap();
    raw.samples[index].validity_status = "invalid".into();
    raw.samples[index].invalid_reason = Some("timeout".into());
    raw.samples[index].timed_out = true;
    raw.samples[index].throughput_operations_per_second = None;
    raw.samples[index].elapsed_ns = None;
    raw.samples[index].operation_count = None;
    raw.samples[index].allocation_calls = None;
    raw.samples[index].profile_path = None;
    raw.samples[index].profile_sha256 = None;
    raw.samples[index].profile_size_bytes = None;
    raw.samples[index].sample_count = None;
    raw.samples[index].sampled_bytes = None;
    raw.samples[index].dropped_records = None;
    raw.samples[index].profiler_arena_bytes = None;
    raw.samples[index].interval_confirmed = None;

    validate_raw_run(&raw).expect("one invalid sample keeps the run structurally valid");
    let report = report_from(&raw);
    let aggressive = report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "aggressive-sampling-tax")
        .unwrap();
    assert_eq!(aggressive.valid_block_count, PPROF_TAX_MIN_BLOCKS - 1);
    assert_ne!(aggressive.support_status, "supported");
    assert!(aggressive.ratio.is_none());
    assert!(aggressive.overhead.is_none());
    assert!(!aggressive.headline_eligible);
}

// (12) rate-1-stress carries the stress-only badge, is never headline
// eligible, and `build_pprof_tax_report` refuses a headline that is not
// the aggregate sparse-sampling-tax.
#[test]
fn rate_1_stress_is_never_headline_and_build_refuses_a_non_sparse_headline() {
    let raw = fixture();
    let report = report_from(&raw);
    let rate1 = report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "rate-1-stress")
        .unwrap();
    assert_eq!(rate1.badge, "stress-only");
    assert!(!rate1.headline_eligible);

    let mut relabelled = report.clone();
    let rate1_index = relabelled
        .comparisons
        .iter()
        .position(|comparison| comparison.comparison_id == "rate-1-stress")
        .unwrap();
    relabelled.headline = relabelled.comparisons[rate1_index].clone();
    assert!(validate_pprof_tax_report(&relabelled).is_err());

    let mut broken_headline = raw.clone();
    invalidate_configuration(&mut broken_headline, "fork-pprof-sparse");
    validate_raw_run(&broken_headline).expect("every declared slot is still filled");
    let bytes = serde_json::to_vec(&broken_headline).unwrap();
    let sha = sha256_bytes(&bytes);
    assert!(build_pprof_tax_report(&broken_headline, &sha, "raw.json").is_err());
}

// (13) Marking every rate-1 sample invalid leaves the sparse comparison's
// numbers bit-identical.
#[test]
fn invalidating_every_rate_1_sample_does_not_change_the_sparse_comparison() {
    let raw = fixture();
    let baseline_report = report_from(&raw);
    let baseline_sparse = baseline_report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "sparse-sampling-tax")
        .unwrap()
        .clone();

    let mut tampered = raw;
    invalidate_configuration(&mut tampered, "fork-pprof-rate-1-stress");
    validate_raw_run(&tampered).expect("every declared slot is still filled");
    let after_report = report_from(&tampered);
    let after_sparse = after_report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "sparse-sampling-tax")
        .unwrap();
    assert_eq!(&baseline_sparse, after_sparse);
}

// (15) Attach/validate fail for invalid sparse data, mismatched upstream
// provenance, and a missing manifest digest.
#[test]
fn attach_and_validate_reject_invalid_sparse_mismatched_provenance_and_missing_digests() {
    let mut invalid_sparse = fixture();
    invalidate_configuration(&mut invalid_sparse, "fork-pprof-sparse");
    validate_raw_run(&invalid_sparse).expect("every declared slot is still filled");
    let bytes = serde_json::to_vec(&invalid_sparse).unwrap();
    let sha = sha256_bytes(&bytes);
    assert!(build_pprof_tax_report(&invalid_sparse, &sha, "raw.json").is_err());

    let raw = fixture();
    let report = report_from(&raw);

    let core = synthetic_full_fixture().unwrap();
    let validation = validate_publication_raw(&core).unwrap();
    let (latest, _history) = build_latest_report(&core, validation).unwrap();

    let mut mismatched_latest = latest.clone();
    for allocator in &mut mismatched_latest.allocators {
        if allocator.allocator_id == "upstream-mimalloc" {
            allocator.source_sha = "f".repeat(40);
        }
    }
    assert!(attach_pprof_tax_report(&mut mismatched_latest, report.clone()).is_err());

    let mut empty_digest_report = report.clone();
    empty_digest_report.configuration_manifest_sha256 = String::new();
    let mut latest_for_empty = latest.clone();
    assert!(attach_pprof_tax_report(&mut latest_for_empty, empty_digest_report).is_err());

    let mut latest_ok = latest;
    attach_pprof_tax_report(&mut latest_ok, report).expect("matching provenance attaches cleanly");
    assert!(latest_ok.pprof_tax.is_some());
    assert!(!latest_ok
        .pending_metrics
        .iter()
        .any(|value| value.metric_id == "pprof-tax"));
}

// block_orders is deterministic in run_seed and near-balanced for 15 blocks.
#[test]
fn block_orders_are_deterministic_and_near_balanced() {
    let a = block_orders(PPROF_TAX_MIN_BLOCKS, 42).unwrap();
    let b = block_orders(PPROF_TAX_MIN_BLOCKS, 42).unwrap();
    assert_eq!(a, b);
    let c = block_orders(PPROF_TAX_MIN_BLOCKS, 43).unwrap();
    assert_ne!(a, c);

    let owned: Vec<Vec<String>> = a
        .iter()
        .map(|order| order.iter().map(|id| id.to_string()).collect())
        .collect();
    validate_block_orders(&owned).expect("a seeded permutation set is near-balanced");
}

// history_projection drops exactly runner/configurations/cells/cell_comparisons/
// active_telemetry/rss_summaries, and keeps everything else.
#[test]
fn history_projection_drops_exactly_the_declared_keys() {
    let raw = fixture();
    let report = report_from(&raw);
    let history = report.history_projection();

    let report_value = serde_json::to_value(&report).unwrap();
    let history_value = serde_json::to_value(&history).unwrap();
    let report_keys: BTreeSet<String> = report_value.as_object().unwrap().keys().cloned().collect();
    let history_keys: BTreeSet<String> =
        history_value.as_object().unwrap().keys().cloned().collect();

    let expected_dropped: BTreeSet<String> = [
        "runner",
        "configurations",
        "cells",
        "cell_comparisons",
        "active_telemetry",
        "rss_summaries",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    let actually_dropped: BTreeSet<String> =
        report_keys.difference(&history_keys).cloned().collect();
    assert_eq!(actually_dropped, expected_dropped);
    assert!(history_keys.contains("runner_fingerprint_sha256"));
}

// A LatestReport without a pprof_tax section still round-trips and never
// serializes the key.
#[test]
fn latest_report_without_pprof_tax_round_trips_without_the_key() {
    let core = synthetic_full_fixture().unwrap();
    let validation = validate_publication_raw(&core).unwrap();
    let (latest, _history) = build_latest_report(&core, validation).unwrap();
    assert!(latest.pprof_tax.is_none());

    let value = serde_json::to_value(&latest).unwrap();
    assert!(!value.as_object().unwrap().contains_key("pprof_tax"));
    let reparsed: LatestReport = serde_json::from_value(value).unwrap();
    assert_eq!(reparsed, latest);
}
