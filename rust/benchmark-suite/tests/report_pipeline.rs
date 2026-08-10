use benchmark_suite::model::{HISTORY_SCHEMA_VERSION, LATEST_SCHEMA_VERSION};
use benchmark_suite::report::build_latest_report;
use benchmark_suite::stats::STATISTICS_VERSION;
use benchmark_suite::validate::{synthetic_full_fixture, validate_publication_raw};

#[test]
fn validated_raw_builds_complete_deterministic_latest_and_compact_history() {
    let raw = synthetic_full_fixture().unwrap();
    let validation = validate_publication_raw(&raw).unwrap();
    let (first, history) = build_latest_report(&raw, validation.clone()).unwrap();
    let (second, _) = build_latest_report(&raw, validation).unwrap();

    assert_eq!(first.latest_schema_version, LATEST_SCHEMA_VERSION);
    assert_eq!(first.statistics_version, STATISTICS_VERSION);
    assert_eq!(first.raw_samples.len(), 30 * 15 * 4);
    assert_eq!(first.absolute_summaries.len(), 30 * 4);
    assert_eq!(first.paired_summaries.len(), 30 * 3);
    assert!(first
        .paired_summaries
        .iter()
        .all(|summary| summary.summary.informational));
    assert_eq!(first.pending_metrics.len(), 4);
    assert_eq!(first.comparison_key.len(), 64);
    assert_eq!(history.history_schema_version, HISTORY_SCHEMA_VERSION);
    assert_eq!(history.absolute_summaries, first.absolute_summaries);
    assert_eq!(history.paired_summaries, first.paired_summaries);

    let first_bytes = serde_json::to_vec(&first).unwrap();
    let second_bytes = serde_json::to_vec(&second).unwrap();
    assert_eq!(first_bytes, second_bytes);
    let history_json = serde_json::to_string(&history).unwrap();
    assert!(!history_json.contains("raw_samples"));
    assert!(!history_json.contains("reproduction_command"));
}

#[test]
fn invalid_validation_report_cannot_be_laundered_into_latest() {
    let raw = synthetic_full_fixture().unwrap();
    let mut validation = validate_publication_raw(&raw).unwrap();
    validation.status = "invalid".into();
    validation.headline_eligible = false;
    validation.errors.push("planted invalidity".into());
    assert!(build_latest_report(&raw, validation).is_err());
}
