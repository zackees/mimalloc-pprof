use benchmark_suite::model::{FeatureState, PublicationRawRun, VALIDATOR_VERSION};
use benchmark_suite::validate::{
    parse_and_validate, synthetic_full_fixture, validate_publication_raw, EXPECTED_CELL_COUNT,
    HEADLINE_ALLOCATORS, MINIMUM_BLOCKS_PER_CELL, VALIDATION_CHECKS,
};
use serde_json::{json, Value};

fn valid() -> PublicationRawRun {
    synthetic_full_fixture().expect("construct full fixture")
}

fn rejection(input: &PublicationRawRun) -> String {
    validate_publication_raw(input)
        .expect_err("invalid fixture must fail closed")
        .to_string()
}

#[test]
fn checked_fixture_spec_builds_a_valid_complete_matrix() {
    let spec: Value =
        serde_json::from_str(include_str!("fixtures/valid-full.fixture.json")).unwrap();
    let input = valid();
    let report = validate_publication_raw(&input).unwrap();
    assert_eq!(spec["expected_schema_version"], input.schema_version);
    assert_eq!(spec["expected_suite_version"], input.suite_version);
    assert_eq!(spec["expected_cells"], EXPECTED_CELL_COUNT);
    assert_eq!(spec["expected_blocks_per_cell"], MINIMUM_BLOCKS_PER_CELL);
    assert_eq!(spec["expected_samples"], input.samples.len());
    assert_eq!(report.validator_version, VALIDATOR_VERSION);
    assert_eq!(report.status, "valid");
    assert!(report.headline_eligible);
    assert_eq!(report.allocator_ids, HEADLINE_ALLOCATORS);
    assert_eq!(report.checks, VALIDATION_CHECKS);
    assert!(report.errors.is_empty());
    let encoded = serde_json::to_string(&input).unwrap();
    let (_, reparsed_report) = parse_and_validate(&encoded).unwrap();
    assert_eq!(report, reparsed_report);
}

#[test]
fn incomplete_matrix_and_missing_pair_are_rejected() {
    let mut input = valid();
    input.samples.pop();
    assert!(rejection(&input).contains("exactly four allocators"));
}

#[test]
fn stale_upstream_pin_is_rejected() {
    let mut input = valid();
    let upstream = input
        .allocators
        .iter_mut()
        .find(|allocator| allocator.allocator_id == "upstream-mimalloc")
        .unwrap();
    upstream.source_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into();
    assert!(rejection(&input).contains("upstream-mimalloc"));
}

#[test]
fn mixed_runner_is_rejected() {
    let mut input = valid();
    input.samples[0].runner.os = "different-linux".into();
    assert!(rejection(&input).contains("mixed runner"));
}

#[test]
fn stale_runner_fingerprint_is_rejected() {
    let mut input = valid();
    input.runner.cpu_model.push_str("-changed");
    assert!(rejection(&input).contains("fingerprint"));
}

#[test]
fn duplicate_sample_and_calibration_keys_are_rejected() {
    let mut samples = valid();
    samples.samples.push(samples.samples[0].clone());
    assert!(rejection(&samples).contains("duplicate sample key"));

    let mut calibrations = valid();
    calibrations.calibrations[1] = calibrations.calibrations[0].clone();
    assert!(rejection(&calibrations).contains("duplicate or unknown calibration"));
}

#[test]
fn aggregate_only_and_malformed_json_are_rejected_structurally() {
    for input in [
        include_str!("fixtures/aggregate-only-sentinel.json"),
        include_str!("fixtures/malformed.json"),
    ] {
        assert!(parse_and_validate(input).is_err());
    }
}

#[test]
fn missing_and_unknown_fields_are_rejected_at_every_core_boundary() {
    let mut missing = serde_json::to_value(valid()).unwrap();
    missing["samples"][0]
        .as_object_mut()
        .unwrap()
        .remove("elapsed_ns");
    assert!(parse_and_validate(&serde_json::to_string(&missing).unwrap()).is_err());

    let mut unknown_top = serde_json::to_value(valid()).unwrap();
    unknown_top
        .as_object_mut()
        .unwrap()
        .insert("aggregate".into(), json!({}));
    assert!(parse_and_validate(&serde_json::to_string(&unknown_top).unwrap()).is_err());

    let mut unknown_nested = serde_json::to_value(valid()).unwrap();
    unknown_nested["allocators"][0]
        .as_object_mut()
        .unwrap()
        .insert("floating_ref".into(), json!("main"));
    assert!(parse_and_validate(&serde_json::to_string(&unknown_nested).unwrap()).is_err());
}

#[test]
fn nonfinite_nonpositive_inconsistent_and_partial_samples_are_rejected() {
    let mut nonfinite = valid();
    nonfinite.samples[0].throughput_operations_per_second = f64::NAN;
    assert!(rejection(&nonfinite).contains("non-finite"));

    let mut nonpositive = valid();
    nonpositive.samples[0].elapsed_ns = 0;
    assert!(rejection(&nonpositive).contains("timing"));

    let mut inconsistent = valid();
    inconsistent.samples[0].throughput_operations_per_second *= 1.01;
    assert!(rejection(&inconsistent).contains("contradicts"));

    let mut partial = valid();
    partial.samples[0].completed_transactions -= 1;
    assert!(rejection(&partial).contains("transaction counts"));
}

#[test]
fn checksum_and_block_request_identity_mismatches_are_rejected() {
    let mut checksum = valid();
    checksum.samples[0].checksum ^= 1;
    assert!(rejection(&checksum).contains("contradicts"));

    let mut seed = valid();
    seed.samples[0].workload_seed ^= 1;
    assert!(rejection(&seed).contains("contradicts") || rejection(&seed).contains("mismatched"));
}

#[test]
fn allocator_order_must_be_near_balanced_even_when_every_block_is_a_permutation() {
    let mut input = valid();
    let scenario = input.samples[0].scenario_id.clone();
    let thread_point = input.samples[0].thread_point.clone();
    for sample in input
        .samples
        .iter_mut()
        .filter(|sample| sample.scenario_id == scenario && sample.thread_point == thread_point)
    {
        sample.ordinal = HEADLINE_ALLOCATORS
            .iter()
            .position(|id| *id == sample.allocator_id)
            .unwrap() as u8;
    }
    assert!(rejection(&input).contains("near-balanced"));
}

#[test]
fn failed_status_and_invalid_calibration_are_rejected() {
    let mut failed = valid();
    failed.samples[0].timed_out = true;
    assert!(rejection(&failed).contains("failed child"));

    let mut calibration = valid();
    calibration.calibrations[0].elapsed_ns = 499_999_999;
    assert!(rejection(&calibration).contains("0.5-2.0s"));
}

#[test]
fn exact_version_ids_and_strict_options_are_required() {
    let mut version = valid();
    version.schema_version = "benchmark-raw-v2".into();
    assert!(rejection(&version).contains("exact raw/suite versions"));

    let mut floating_allocator = valid();
    floating_allocator.allocators[0].allocator_version = "latest".into();
    assert!(rejection(&floating_allocator).contains("tcmalloc"));

    let mut options = valid();
    options
        .allocators
        .iter_mut()
        .find(|allocator| allocator.allocator_id == "mimalloc-pprof")
        .unwrap()
        .options
        .pprof_runtime = FeatureState::Enabled;
    assert!(rejection(&options).contains("options"));
}

#[test]
fn checked_in_schemas_are_valid_strict_json() {
    for schema in [
        include_str!("../schema/raw-run-v1.schema.json"),
        include_str!("../schema/latest-v1.schema.json"),
        include_str!("../schema/history-v1.schema.json"),
    ] {
        let value: Value = serde_json::from_str(schema).unwrap();
        assert_eq!(
            value["$schema"],
            "https://json-schema.org/draft/2020-12/schema"
        );
        assert_eq!(value["additionalProperties"], false);
    }
}
