use std::collections::BTreeMap;

use benchmark_suite::comparison_key::{
    comparison_key, AffinityCompatibility, AllocatorCompatibility, CardCompatibility,
    ComparisonKeyInput, MetricCompatibility, PatchCompatibility, PowerCompatibility,
    RealizedOperationCount, RunnerCompatibility, SchemaCompatibility, SuiteCompatibility,
    ToolchainCompatibility,
};
use benchmark_suite::stats::{
    summarize_absolute, summarize_paired, type7_quantile, MetricDirection, MetricObservation,
    BOOTSTRAP_METHOD, BOOTSTRAP_PRNG, BOOTSTRAP_RESAMPLES, STATISTICS_VERSION,
};

const REFERENCE: &str = "upstream-mimalloc";
const CANDIDATE: &str = "mimalloc-pprof";

fn observations(candidate: &[f64], reference: &[f64]) -> Vec<MetricObservation> {
    assert_eq!(candidate.len(), reference.len());
    let mut values = Vec::new();
    for (block, (&candidate_value, &reference_value)) in candidate.iter().zip(reference).enumerate()
    {
        // Adversarial allocator order is intentional: pairing must use IDs.
        values.push(MetricObservation {
            block_id: block as u32,
            allocator_id: REFERENCE.to_owned(),
            value: reference_value,
        });
        values.push(MetricObservation {
            block_id: block as u32,
            allocator_id: "jemalloc".to_owned(),
            value: 7.0 + block as f64,
        });
        values.push(MetricObservation {
            block_id: block as u32,
            allocator_id: CANDIDATE.to_owned(),
            value: candidate_value,
        });
        values.push(MetricObservation {
            block_id: block as u32,
            allocator_id: "tcmalloc".to_owned(),
            value: 8.0 + block as f64,
        });
    }
    values
}

fn close(actual: f64, expected: f64) {
    let tolerance = expected.abs().max(1.0) * 1e-12;
    assert!(
        (actual - expected).abs() <= tolerance,
        "{actual} != {expected}"
    );
}

#[test]
fn type7_and_absolute_summary_match_golden_values() {
    let values = [1.0, 2.0, 3.0, 4.0];
    close(type7_quantile(&values, 0.25).unwrap(), 1.75);
    close(type7_quantile(&values, 0.50).unwrap(), 2.5);
    close(type7_quantile(&values, 0.75).unwrap(), 3.25);

    let summary = summarize_absolute(&values).unwrap();
    assert_eq!(summary.count, 4);
    close(summary.min, 1.0);
    close(summary.max, 4.0);
    close(summary.median, 2.5);
    close(summary.q1, 1.75);
    close(summary.q3, 3.25);
    close(summary.iqr, 1.5);
    close(summary.relative_iqr, 0.6);
    assert!(summary.noisy);
}

#[test]
fn identical_pairs_have_exact_unit_effect_and_interval() {
    let values: Vec<f64> = (1..=15).map(|value| value as f64).collect();
    let result = summarize_paired(
        17,
        "tiny-fixed-64/1",
        CANDIDATE,
        REFERENCE,
        MetricDirection::HigherIsBetter,
        &observations(&values, &values),
    )
    .unwrap();
    assert_eq!(result.effect, 1.0);
    assert_eq!(result.confidence_interval.lower, 1.0);
    assert_eq!(result.confidence_interval.upper, 1.0);
    assert_eq!(result.block_count, 15);
    assert_eq!(result.bootstrap.resample_count, BOOTSTRAP_RESAMPLES);
    assert_eq!(result.bootstrap.method, BOOTSTRAP_METHOD);
    assert_eq!(result.bootstrap.prng, BOOTSTRAP_PRNG);
    assert!(result.informational);
}

#[test]
fn exact_ten_percent_improvement_has_exact_golden_ratio() {
    let reference = vec![10.0; 15];
    let candidate = vec![11.0; 15];
    let result = summarize_paired(
        23,
        "tiny-fixed-64/1",
        CANDIDATE,
        REFERENCE,
        MetricDirection::HigherIsBetter,
        &observations(&candidate, &reference),
    )
    .unwrap();
    close(result.effect, 1.1);
    close(result.confidence_interval.lower, 1.1);
    close(result.confidence_interval.upper, 1.1);
}

#[test]
fn common_drift_and_execution_order_do_not_change_paired_effect() {
    let reference: Vec<f64> = (1..=15).map(|value| value as f64 * 100.0).collect();
    let candidate: Vec<f64> = reference.iter().map(|value| value * 1.1).collect();
    let ordered = observations(&candidate, &reference);
    let mut reversed = ordered.clone();
    reversed.reverse();
    let first = summarize_paired(
        29,
        "drift/1",
        CANDIDATE,
        REFERENCE,
        MetricDirection::HigherIsBetter,
        &ordered,
    )
    .unwrap();
    let second = summarize_paired(
        29,
        "drift/1",
        CANDIDATE,
        REFERENCE,
        MetricDirection::HigherIsBetter,
        &reversed,
    )
    .unwrap();
    close(first.effect, 1.1);
    assert_eq!(first, second);
}

#[test]
fn lower_is_better_is_normalized_above_one() {
    let candidate = vec![90.0; 15];
    let reference = vec![100.0; 15];
    let result = summarize_paired(
        31,
        "latency/1",
        CANDIDATE,
        REFERENCE,
        MetricDirection::LowerIsBetter,
        &observations(&candidate, &reference),
    )
    .unwrap();
    close(result.effect, 100.0 / 90.0);
    assert!(result.effect > 1.0);
}

#[test]
fn an_extreme_observation_is_retained_and_reported_as_noisy() {
    let summary = summarize_absolute(&[1.0, 1.0, 1.0, 100.0]).unwrap();
    assert_eq!(summary.count, 4);
    assert_eq!(summary.max, 100.0);
    assert!(summary.noisy);
    assert!(summary.relative_iqr > 0.10);
}

#[test]
fn missing_duplicate_nonpositive_and_nonfinite_inputs_are_rejected() {
    assert!(summarize_absolute(&[]).is_err());
    assert!(summarize_absolute(&[0.0]).is_err());
    assert!(summarize_absolute(&[f64::NAN]).is_err());
    assert!(summarize_absolute(&[f64::INFINITY]).is_err());

    let mut missing = observations(&[2.0, 2.0], &[1.0, 1.0]);
    missing.retain(|item| !(item.block_id == 1 && item.allocator_id == CANDIDATE));
    assert!(summarize_paired(
        1,
        "cell",
        CANDIDATE,
        REFERENCE,
        MetricDirection::HigherIsBetter,
        &missing,
    )
    .is_err());

    let mut duplicate = observations(&[2.0], &[1.0]);
    duplicate.push(duplicate[0].clone());
    assert!(summarize_paired(
        1,
        "cell",
        CANDIDATE,
        REFERENCE,
        MetricDirection::HigherIsBetter,
        &duplicate,
    )
    .is_err());

    for bad in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let mut invalid = observations(&[2.0], &[1.0]);
        invalid[0].value = bad;
        assert!(summarize_paired(
            1,
            "cell",
            CANDIDATE,
            REFERENCE,
            MetricDirection::HigherIsBetter,
            &invalid,
        )
        .is_err());
    }
}

#[test]
fn deterministic_summary_serialization_is_byte_identical() {
    let reference: Vec<f64> = (10..25).map(|value| value as f64).collect();
    let candidate: Vec<f64> = reference
        .iter()
        .enumerate()
        .map(|(index, value)| value * (1.0 + index as f64 / 100.0))
        .collect();
    let first_input = observations(&candidate, &reference);
    let mut second_input = first_input.clone();
    second_input.reverse();
    let summarize = |input: &[MetricObservation]| {
        summarize_paired(
            0x0123_4567_89ab_cdef,
            "deterministic/4",
            CANDIDATE,
            REFERENCE,
            MetricDirection::HigherIsBetter,
            input,
        )
        .unwrap()
    };
    let first = serde_json::to_vec(&summarize(&first_input)).unwrap();
    let second = serde_json::to_vec(&summarize(&second_input)).unwrap();
    assert_eq!(first, second);
}

fn map(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
    entries
        .iter()
        .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
        .collect()
}

fn allocator(id: &str, marker: char) -> AllocatorCompatibility {
    AllocatorCompatibility {
        allocator_id: id.to_owned(),
        allocator_version: format!("1.0-{marker}"),
        fork_kind: "upstream".to_owned(),
        source_repository: format!("https://example.invalid/{id}"),
        source_sha: marker.to_string().repeat(40),
        source_archive_sha256: Some(marker.to_string().repeat(64)),
        source_tree_sha256: marker.to_ascii_uppercase().to_string().repeat(64),
        patches: vec![PatchCompatibility {
            patch_id: "build-fix".to_owned(),
            sha256: "e".repeat(64),
        }],
        lockfile_sha256: "f".repeat(64),
        build_system: "cmake".to_owned(),
        build_commands: vec![vec!["cmake".to_owned(), "--build".to_owned()]],
        compiler: "clang 20".to_owned(),
        linker: "lld 20".to_owned(),
        build_flags: vec!["-O3".to_owned()],
        options: map(&[("shared", "off")]),
        static_library_sha256: marker.to_string().repeat(64),
        child_binary_sha256: marker.to_ascii_uppercase().to_string().repeat(64),
        profiler_enabled: false,
        memory_events_enabled: false,
        frame_pointers_enabled: true,
        architecture: "x86_64".to_owned(),
        simd: "avx2".to_owned(),
    }
}

fn key_input() -> ComparisonKeyInput {
    ComparisonKeyInput {
        schemas: SchemaCompatibility {
            raw_schema_version: "benchmark-raw-v1".to_owned(),
            latest_schema_version: "benchmark-latest-v1".to_owned(),
            history_schema_version: "benchmark-history-v1".to_owned(),
            statistics_version: STATISTICS_VERSION.to_owned(),
        },
        suite: SuiteCompatibility {
            suite_version: "core-throughput-v1".to_owned(),
            cards: vec![CardCompatibility {
                card_id: "tiny-fixed-64".to_owned(),
                card_version: "core-throughput-v1".to_owned(),
                definition: map(&[("size", "64"), ("pattern", "fixed")]),
            }],
            metrics: vec![MetricCompatibility {
                metric_id: "throughput".to_owned(),
                unit: "operations/s".to_owned(),
                direction: "higher-is-better".to_owned(),
                definition: map(&[("clock", "monotonic"), ("scope", "timed")]),
            }],
        },
        realized_operation_counts: vec![RealizedOperationCount {
            scenario_id: "tiny-fixed-64".to_owned(),
            thread_point: "1".to_owned(),
            operation_count: 1_000_000,
        }],
        allocators: vec![
            allocator("jemalloc", 'a'),
            allocator("mimalloc-pprof", 'b'),
            allocator("tcmalloc", 'c'),
            allocator("upstream-mimalloc", 'd'),
        ],
        runner: RunnerCompatibility {
            runner_class: "github-hosted".to_owned(),
            fingerprint: "linux-x64-8core".to_owned(),
            target: "x86_64-unknown-linux-gnu".to_owned(),
        },
        toolchain: ToolchainCompatibility {
            compiler: "rustc 1.90".to_owned(),
            linker: "cc 14".to_owned(),
            target: "x86_64-unknown-linux-gnu".to_owned(),
            build_flags: vec!["-Ctarget-cpu=x86-64-v3".to_owned()],
        },
        affinity: AffinityCompatibility {
            policy: "compact".to_owned(),
            logical_cpus: vec![0, 1, 2, 3],
        },
        power: PowerCompatibility {
            policy: "observable".to_owned(),
            observable_values: map(&[("governor", "performance")]),
        },
        timestamp: "2026-08-10T00:00:00Z".to_owned(),
        run_url: "https://github.invalid/actions/runs/1".to_owned(),
    }
}

fn digest(input: &ComparisonKeyInput) -> String {
    comparison_key(input).unwrap().sha256
}

#[test]
fn every_compatibility_field_mutation_changes_the_key() {
    let original = key_input();
    let expected = digest(&original);
    let assert_changed = |name: &str, changed: ComparisonKeyInput| {
        assert_ne!(
            digest(&changed),
            expected,
            "field mutation did not hash: {name}"
        );
    };

    macro_rules! changed {
        ($name:literal, $body:expr) => {{
            let mut value = original.clone();
            $body(&mut value);
            assert_changed($name, value);
        }};
    }

    changed!("raw_schema_version", |v: &mut ComparisonKeyInput| v
        .schemas
        .raw_schema_version
        .push('x'));
    changed!("latest_schema_version", |v: &mut ComparisonKeyInput| v
        .schemas
        .latest_schema_version
        .push('x'));
    changed!("history_schema_version", |v: &mut ComparisonKeyInput| v
        .schemas
        .history_schema_version
        .push('x'));
    changed!("statistics_version", |v: &mut ComparisonKeyInput| v
        .schemas
        .statistics_version
        .push('x'));
    changed!("suite_version", |v: &mut ComparisonKeyInput| v
        .suite
        .suite_version
        .push('x'));
    changed!("card_id", |v: &mut ComparisonKeyInput| v.suite.cards[0]
        .card_id
        .push('x'));
    changed!("card_version", |v: &mut ComparisonKeyInput| v.suite.cards
        [0]
    .card_version
    .push('x'));
    changed!("card_definition", |v: &mut ComparisonKeyInput| {
        v.suite.cards[0]
            .definition
            .insert("size".to_owned(), "65".to_owned());
    });
    changed!("metric_id", |v: &mut ComparisonKeyInput| v.suite.metrics[0]
        .metric_id
        .push('x'));
    changed!("metric_unit", |v: &mut ComparisonKeyInput| v.suite.metrics
        [0]
    .unit
    .push('x'));
    changed!("metric_direction", |v: &mut ComparisonKeyInput| v
        .suite
        .metrics[0]
        .direction
        .push('x'));
    changed!("metric_definition", |v: &mut ComparisonKeyInput| {
        v.suite.metrics[0]
            .definition
            .insert("scope".to_owned(), "all".to_owned());
    });
    changed!("operation_scenario", |v: &mut ComparisonKeyInput| v
        .realized_operation_counts[0]
        .scenario_id
        .push('x'));
    changed!("operation_thread_point", |v: &mut ComparisonKeyInput| v
        .realized_operation_counts[0]
        .thread_point
        .push('x'));
    changed!("operation_count", |v: &mut ComparisonKeyInput| v
        .realized_operation_counts[0]
        .operation_count +=
        1);

    changed!("allocator_id", |v: &mut ComparisonKeyInput| v.allocators[0]
        .allocator_id
        .push('x'));
    changed!("allocator_version", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .allocator_version
        .push('x'));
    changed!("fork_kind", |v: &mut ComparisonKeyInput| v.allocators[0]
        .fork_kind
        .push('x'));
    changed!("source_repository", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .source_repository
        .push('x'));
    changed!("source_sha", |v: &mut ComparisonKeyInput| v.allocators[0]
        .source_sha
        .push('x'));
    changed!("source_archive_sha256", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .source_archive_sha256 =
        None);
    changed!("source_tree_sha256", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .source_tree_sha256
        .push('x'));
    changed!("patch_id", |v: &mut ComparisonKeyInput| v.allocators[0]
        .patches[0]
        .patch_id
        .push('x'));
    changed!("patch_sha256", |v: &mut ComparisonKeyInput| v.allocators[0]
        .patches[0]
        .sha256
        .push('x'));
    changed!("lockfile_sha256", |v: &mut ComparisonKeyInput| v.allocators
        [0]
    .lockfile_sha256
    .push('x'));
    changed!("allocator_build_system", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .build_system
        .push('x'));
    changed!("allocator_build_commands", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .build_commands[0]
        .push("--verbose".to_owned()));
    changed!("allocator_compiler", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .compiler
        .push('x'));
    changed!("allocator_linker", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .linker
        .push('x'));
    changed!("allocator_build_flags", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .build_flags
        .push("-g".to_owned()));
    changed!("allocator_options", |v: &mut ComparisonKeyInput| {
        v.allocators[0]
            .options
            .insert("shared".to_owned(), "on".to_owned());
    });
    changed!("static_library_sha256", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .static_library_sha256
        .push('x'));
    changed!("child_binary_sha256", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .child_binary_sha256
        .push('x'));
    changed!("profiler_enabled", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .profiler_enabled =
        true);
    changed!("memory_events_enabled", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .memory_events_enabled =
        true);
    changed!("frame_pointers_enabled", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .frame_pointers_enabled =
        false);
    changed!("allocator_architecture", |v: &mut ComparisonKeyInput| v
        .allocators[0]
        .architecture
        .push('x'));
    changed!("allocator_simd", |v: &mut ComparisonKeyInput| v.allocators
        [0]
    .simd
    .push('x'));

    changed!("runner_class", |v: &mut ComparisonKeyInput| v
        .runner
        .runner_class
        .push('x'));
    changed!("runner_fingerprint", |v: &mut ComparisonKeyInput| v
        .runner
        .fingerprint
        .push('x'));
    changed!("runner_target", |v: &mut ComparisonKeyInput| v
        .runner
        .target
        .push('x'));
    changed!("toolchain_compiler", |v: &mut ComparisonKeyInput| v
        .toolchain
        .compiler
        .push('x'));
    changed!("toolchain_linker", |v: &mut ComparisonKeyInput| v
        .toolchain
        .linker
        .push('x'));
    changed!("toolchain_target", |v: &mut ComparisonKeyInput| v
        .toolchain
        .target
        .push('x'));
    changed!("toolchain_build_flags", |v: &mut ComparisonKeyInput| v
        .toolchain
        .build_flags
        .push("-g".to_owned()));
    changed!("affinity_policy", |v: &mut ComparisonKeyInput| v
        .affinity
        .policy
        .push('x'));
    changed!("affinity_cpus", |v: &mut ComparisonKeyInput| v
        .affinity
        .logical_cpus
        .push(7));
    changed!("power_policy", |v: &mut ComparisonKeyInput| v
        .power
        .policy
        .push('x'));
    changed!("power_observable_values", |v: &mut ComparisonKeyInput| {
        v.power
            .observable_values
            .insert("governor".to_owned(), "powersave".to_owned());
    });
}

#[test]
fn volatile_fields_are_excluded_and_semantic_collection_order_is_canonical() {
    let original = key_input();
    let expected = comparison_key(&original).unwrap();

    let mut volatile = original.clone();
    volatile.timestamp = "2099-01-01T00:00:00Z".to_owned();
    volatile.run_url = "https://github.invalid/actions/runs/999".to_owned();
    assert_eq!(comparison_key(&volatile).unwrap(), expected);

    let mut reordered = original;
    reordered.allocators.reverse();
    reordered.suite.cards.reverse();
    reordered.suite.metrics.reverse();
    reordered.realized_operation_counts.reverse();
    reordered.affinity.logical_cpus.reverse();
    assert_eq!(comparison_key(&reordered).unwrap(), expected);
    assert!(expected.canonical_json.starts_with("{\"affinity\":"));
}
