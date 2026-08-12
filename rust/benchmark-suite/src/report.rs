//! Build the public typed report only after the raw publication envelope has
//! passed semantic validation. Python renders this value but never computes
//! benchmark statistics.

use std::collections::{BTreeMap, BTreeSet};

use crate::comparison_key::{
    comparison_key, AffinityCompatibility, AllocatorCompatibility, CardCompatibility,
    ComparisonKeyInput, MetricCompatibility, PatchCompatibility, PowerCompatibility,
    RealizedOperationCount, RunnerCompatibility, SchemaCompatibility, SuiteCompatibility,
    ToolchainCompatibility,
};
use crate::model::{
    AbsoluteCellSummary, AllocatorIdentity, CanonicalUrls, FeatureState, HistoryRow, LatestReport,
    MethodologyContract, PairedCellSummary, PendingMetric, PublicationRawRun, ValidationReport,
    HISTORY_SCHEMA_VERSION, LATEST_SCHEMA_VERSION,
};
use crate::scenarios::cards;
use crate::stats::{
    summarize_absolute, summarize_paired, MetricDirection, MetricObservation, STATISTICS_VERSION,
};
use crate::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

const REFERENCE_ALLOCATOR: &str = "upstream-mimalloc";
const METRIC_ID: &str = "throughput-operations-per-second";

/// Produce the only JSON value accepted by the Python renderer and the compact
/// typed history row derived from it.
pub fn build_latest_report(
    raw: &PublicationRawRun,
    validation: ValidationReport,
) -> Result<(LatestReport, HistoryRow), String> {
    if validation.status != "valid"
        || !validation.headline_eligible
        || !validation.errors.is_empty()
    {
        return Err("latest report requires a clean, headline-eligible validation report".into());
    }

    let mut absolute_summaries = Vec::new();
    let mut paired_summaries = Vec::new();
    for definition in cards() {
        for point in definition.thread_points {
            let cell_samples = raw.samples.iter().filter(|sample| {
                sample.scenario_id == definition.id.as_str() && sample.thread_point == point.name()
            });
            let observations = cell_samples
                .clone()
                .map(|sample| MetricObservation {
                    block_id: sample.block_id,
                    allocator_id: sample.allocator_id.clone(),
                    value: sample.throughput_operations_per_second,
                })
                .collect::<Vec<_>>();
            let cell_id = format!("{}/{}/{}", definition.id.as_str(), point.name(), METRIC_ID);
            for allocator in crate::validate::HEADLINE_ALLOCATORS {
                let values = cell_samples
                    .clone()
                    .filter(|sample| sample.allocator_id == allocator)
                    .map(|sample| sample.throughput_operations_per_second)
                    .collect::<Vec<_>>();
                absolute_summaries.push(AbsoluteCellSummary {
                    scenario_id: definition.id.as_str().into(),
                    thread_point: point.name().into(),
                    metric_id: METRIC_ID.into(),
                    direction: MetricDirection::HigherIsBetter,
                    allocator_id: allocator.into(),
                    summary: summarize_absolute(&values).map_err(|error| error.to_string())?,
                });
                if allocator != REFERENCE_ALLOCATOR {
                    paired_summaries.push(PairedCellSummary {
                        scenario_id: definition.id.as_str().into(),
                        thread_point: point.name().into(),
                        metric_id: METRIC_ID.into(),
                        summary: summarize_paired(
                            raw.run_seed,
                            &cell_id,
                            allocator,
                            REFERENCE_ALLOCATOR,
                            MetricDirection::HigherIsBetter,
                            &observations,
                        )
                        .map_err(|error| error.to_string())?,
                    });
                }
            }
        }
    }

    let key = build_comparison_key(raw)?;
    let actions_run_url = if raw.run.run_origin == "github-actions" {
        format!(
            "https://github.com/zackees/mimalloc-pprof/actions/runs/{}",
            raw.run.run_id
        )
    } else {
        "https://github.com/zackees/mimalloc-pprof/actions".into()
    };
    let latest = LatestReport {
        latest_schema_version: LATEST_SCHEMA_VERSION.into(),
        raw_schema_version: RAW_SCHEMA_VERSION.into(),
        statistics_version: STATISTICS_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        validation_report: validation,
        run: raw.run.clone(),
        runner: raw.runner.clone(),
        allocators: raw.allocators.clone(),
        calibrations: raw.calibrations.clone(),
        raw_samples: raw.samples.clone(),
        absolute_summaries,
        paired_summaries,
        comparison_key: key,
        methodology: MethodologyContract {
            absolute_summary: "median, Type-7 quartiles, IQR, min/max; no outlier removal".into(),
            paired_effect: "exp(median(per-block log(candidate/reference)))".into(),
            confidence_interval:
                "deterministic 95% percentile whole-block bootstrap, 10000 resamples".into(),
            quantile_method: "R/NumPy Type 7 linear interpolation".into(),
            noise_threshold_relative_iqr: 0.10,
            informational: true,
        },
        pending_metrics: vec![
            pending("memory", 184),
            pending("latency", 185),
            pending("scaling", 186),
            pending("pprof-tax", 187),
        ],
        memory: None,
        latency: None,
        canonical_urls: CanonicalUrls {
            pages: "https://zackees.github.io/mimalloc-pprof/".into(),
            stats_branch: "https://github.com/zackees/mimalloc-pprof/tree/benchmark-stats".into(),
            latest_json: "https://zackees.github.io/mimalloc-pprof/latest.json".into(),
            methodology: "https://zackees.github.io/mimalloc-pprof/#methodology".into(),
        },
        reproduction_command: raw
            .samples
            .first()
            .map(|sample| sample.reproduction_command.clone())
            .ok_or_else(|| "validated raw run unexpectedly has no samples".to_string())?,
        actions_run_url,
    };
    let history = HistoryRow {
        history_schema_version: HISTORY_SCHEMA_VERSION.into(),
        statistics_version: STATISTICS_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run: latest.run.clone(),
        comparison_key: latest.comparison_key.clone(),
        runner: latest.runner.clone(),
        allocator_identities: latest
            .allocators
            .iter()
            .map(|allocator| AllocatorIdentity {
                allocator_id: allocator.allocator_id.clone(),
                allocator_version: allocator.allocator_version.clone(),
                source_sha: allocator.source_sha.clone(),
                library_sha256: allocator.static_library_sha256.clone(),
                child_binary_sha256: allocator.child_binary_sha256.clone(),
            })
            .collect(),
        absolute_summaries: latest.absolute_summaries.clone(),
        paired_summaries: latest.paired_summaries.clone(),
        memory: latest
            .memory
            .as_ref()
            .map(|value| value.history_projection()),
        latency: latest
            .latency
            .as_ref()
            .map(|value| value.history_projection()),
    };
    Ok((latest, history))
}

fn pending(metric_id: &str, issue: u32) -> PendingMetric {
    PendingMetric {
        status: "pending".into(),
        metric_id: metric_id.into(),
        reason: "pending - metric protocol not implemented".into(),
        phase_issue_url: format!("https://github.com/zackees/mimalloc-pprof/issues/{issue}"),
    }
}

fn build_comparison_key(raw: &PublicationRawRun) -> Result<String, String> {
    let cards = cards()
        .iter()
        .map(|card| {
            let mut definition = BTreeMap::new();
            definition.insert("description".into(), card.description.into());
            definition.insert("operation_unit".into(), card.operation_unit.name().into());
            definition.insert(
                "size_distribution".into(),
                format!("{:?}", card.size_distribution()),
            );
            definition.insert("lifetime".into(), format!("{:?}", card.lifetime()));
            definition.insert("touch_rule".into(), format!("{:?}", card.touch_rule()));
            definition.insert("invariant".into(), format!("{:?}", card.invariant()));
            definition.insert(
                "thread_points".into(),
                card.thread_points
                    .iter()
                    .map(|point| point.name())
                    .collect::<Vec<_>>()
                    .join(","),
            );
            CardCompatibility {
                card_id: card.id.as_str().into(),
                card_version: CORE_SUITE_VERSION.into(),
                definition,
            }
        })
        .collect();
    let metrics = vec![MetricCompatibility {
        metric_id: METRIC_ID.into(),
        unit: "operations/second".into(),
        direction: "higher-is-better".into(),
        definition: BTreeMap::from([
            ("numerator".into(), "validated operation_count".into()),
            ("denominator".into(), "measured elapsed nanoseconds".into()),
        ]),
    }];
    let realized_operation_counts = raw
        .calibrations
        .iter()
        .map(|calibration| RealizedOperationCount {
            scenario_id: calibration.scenario_id.clone(),
            thread_point: calibration.thread_point.clone(),
            operation_count: calibration.operation_count,
        })
        .collect();
    let allocators = raw
        .allocators
        .iter()
        .map(|allocator| {
            let options = BTreeMap::from([
                (
                    "pprof_compiled".into(),
                    feature_name(&allocator.options.pprof_compiled).into(),
                ),
                (
                    "pprof_runtime".into(),
                    feature_name(&allocator.options.pprof_runtime).into(),
                ),
                (
                    "memory_events_compiled".into(),
                    feature_name(&allocator.options.memory_events_compiled).into(),
                ),
                (
                    "memory_events_runtime".into(),
                    feature_name(&allocator.options.memory_events_runtime).into(),
                ),
                (
                    "frame_pointers".into(),
                    feature_name(&allocator.options.frame_pointers).into(),
                ),
                (
                    "opt_arch".into(),
                    feature_name(&allocator.options.opt_arch).into(),
                ),
                (
                    "opt_simd".into(),
                    feature_name(&allocator.options.opt_simd).into(),
                ),
            ]);
            AllocatorCompatibility {
                allocator_id: allocator.allocator_id.clone(),
                allocator_version: allocator.allocator_version.clone(),
                fork_kind: allocator.source_kind.clone(),
                source_repository: allocator.canonical_repository.clone(),
                source_sha: allocator.source_sha.clone(),
                source_archive_sha256: (allocator.source_archive_sha256 != "not-applicable")
                    .then(|| allocator.source_archive_sha256.clone()),
                source_tree_sha256: allocator.source_tree_sha256.clone(),
                patches: allocator
                    .source_patches
                    .iter()
                    .map(|patch| PatchCompatibility {
                        patch_id: patch.file.clone(),
                        sha256: patch.sha256.clone(),
                    })
                    .collect(),
                lockfile_sha256: raw.allocator_lock_sha256.clone(),
                build_system: allocator.build_system.clone(),
                build_commands: allocator.build_commands.clone(),
                compiler: allocator.compiler.clone(),
                linker: allocator.linker.clone(),
                build_flags: allocator.build_flags.clone(),
                options,
                static_library_sha256: allocator.static_library_sha256.clone(),
                child_binary_sha256: allocator.child_binary_sha256.clone(),
                profiler_enabled: allocator.options.pprof_runtime == FeatureState::Enabled,
                memory_events_enabled: allocator.options.memory_events_runtime
                    == FeatureState::Enabled,
                frame_pointers_enabled: allocator.options.frame_pointers == FeatureState::Enabled,
                architecture: raw.runner.architecture.clone(),
                simd: feature_name(&allocator.options.opt_simd).into(),
            }
        })
        .collect::<Vec<_>>();
    let mut compilers = raw
        .allocators
        .iter()
        .map(|item| item.compiler.clone())
        .collect::<BTreeSet<_>>();
    let mut linkers = raw
        .allocators
        .iter()
        .map(|item| item.linker.clone())
        .collect::<BTreeSet<_>>();
    let flags = raw
        .allocators
        .iter()
        .flat_map(|item| item.build_flags.iter().cloned())
        .collect::<BTreeSet<_>>();
    let input = ComparisonKeyInput {
        schemas: SchemaCompatibility {
            raw_schema_version: RAW_SCHEMA_VERSION.into(),
            latest_schema_version: LATEST_SCHEMA_VERSION.into(),
            history_schema_version: HISTORY_SCHEMA_VERSION.into(),
            statistics_version: STATISTICS_VERSION.into(),
        },
        suite: SuiteCompatibility {
            suite_version: CORE_SUITE_VERSION.into(),
            cards,
            metrics,
        },
        realized_operation_counts,
        allocators,
        runner: RunnerCompatibility {
            runner_class: raw.runner.runner_class.clone(),
            fingerprint: raw.runner.fingerprint_sha256.clone(),
            target: raw.runner.target.clone(),
        },
        toolchain: ToolchainCompatibility {
            compiler: compilers.pop_first().unwrap_or_default()
                + &compilers
                    .into_iter()
                    .map(|value| format!("|{value}"))
                    .collect::<String>(),
            linker: linkers.pop_first().unwrap_or_default()
                + &linkers
                    .into_iter()
                    .map(|value| format!("|{value}"))
                    .collect::<String>(),
            target: raw.runner.target.clone(),
            build_flags: flags.iter().cloned().collect(),
        },
        affinity: AffinityCompatibility {
            policy: raw.runner.affinity.policy.clone(),
            logical_cpus: raw.runner.affinity.logical_cpu_ids.clone(),
        },
        power: PowerCompatibility {
            policy: raw.runner.power.frequency_policy.clone(),
            observable_values: BTreeMap::from([
                ("governor".into(), raw.runner.power.governor.clone()),
                ("boost".into(), raw.runner.power.boost.clone()),
            ]),
        },
        timestamp: raw.run.generated_at_utc.clone(),
        run_url: if raw.run.run_origin == "github-actions" {
            format!(
                "https://github.com/zackees/mimalloc-pprof/actions/runs/{}",
                raw.run.run_id
            )
        } else {
            String::new()
        },
    };
    comparison_key(&input)
        .map(|key| key.sha256)
        .map_err(|error| error.to_string())
}

fn feature_name(state: &FeatureState) -> &'static str {
    match state {
        FeatureState::Enabled => "enabled",
        FeatureState::Disabled => "disabled",
        FeatureState::NotApplicable => "not-applicable",
    }
}
