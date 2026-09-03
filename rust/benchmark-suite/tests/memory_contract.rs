use std::collections::BTreeMap;
use std::process::Command;

use benchmark_suite::memory::{
    attach_memory_report, build_memory_report, fragmentation_proxy, memory_comparison_key,
    memory_scenario_cells, parse_smaps_rollup, parse_status_hwm, read_control_record,
    read_proc_snapshot, validate_memory_raw_run, validate_sampler_ownership, validate_timeline,
    write_control_record, ControlKind, ControlRecord, LiveRequestedOracle, MemoryCompatibility,
    MemoryEnvironment, MemoryRawRun, MemoryRawSample, MemoryTimelinePoint, MEMORY_SAMPLE_TARGET_NS,
    MEMORY_SCHEMA_VERSION,
};
use benchmark_suite::model::LatestReport;
use benchmark_suite::report::build_latest_report;
use benchmark_suite::scenarios::{CardId, ThreadPoint, Topology};
use benchmark_suite::validate::{synthetic_full_fixture, validate_publication_raw};

#[test]
fn proc_kilobytes_are_parsed_as_exact_bytes() {
    let smaps = "00400000-00401000 r--p 00000000 00:00 0\nRss:                1234 kB\nPss:                 999 kB\n";
    let status = "Name:\tfixture\nVmPeak:\t  5000 kB\nVmHWM:\t  4321 kB\nVmRSS:\t  1234 kB\n";
    assert_eq!(parse_smaps_rollup(smaps).unwrap(), 1_263_616);
    assert_eq!(parse_status_hwm(status).unwrap(), 4_424_704);
}

#[test]
fn missing_or_malformed_proc_fields_fail_closed() {
    for value in [
        "Pss: 1 kB\n",
        "Rss: nope kB\n",
        "Rss: 1 MB\n",
        "Rss: 1 kB\nRss: 2 kB\n",
    ] {
        assert!(parse_smaps_rollup(value).is_err(), "{value:?}");
    }
    for value in [
        "VmRSS: 1 kB\n",
        "VmHWM: nope kB\n",
        "VmHWM: 1 MB\n",
        "VmHWM: 1 kB\nVmHWM: 2 kB\n",
    ] {
        assert!(parse_status_hwm(value).is_err(), "{value:?}");
    }
}

#[test]
fn fragmentation_rejects_nonpositive_inputs_without_clamping() {
    assert_eq!(fragmentation_proxy(4096, 2048).unwrap(), 2.0);
    assert!(fragmentation_proxy(0, 2048).is_err());
    assert!(fragmentation_proxy(-1, 2048).is_err());
    assert!(fragmentation_proxy(4096, 0).is_err());
}

#[test]
fn sampler_timestamps_are_bounded_and_inside_the_workload_window() {
    let points = vec![
        MemoryTimelinePoint {
            elapsed_ns: 10_000_000,
            rss_bytes: 100,
        },
        MemoryTimelinePoint {
            elapsed_ns: 15_000_000,
            rss_bytes: 200,
        },
        MemoryTimelinePoint {
            elapsed_ns: 20_500_000,
            rss_bytes: 150,
        },
    ];
    let summary = validate_timeline(&points, 9_000_000, 21_000_000, 5_000_000).unwrap();
    assert_eq!(summary.sample_count, 3);
    assert_eq!(summary.maximum_interval_ns, 5_500_000);

    let mut outside = points.clone();
    outside[0].elapsed_ns = 8_000_000;
    assert!(validate_timeline(&outside, 9_000_000, 21_000_000, 5_000_000).is_err());
    let sparse = vec![
        MemoryTimelinePoint {
            elapsed_ns: 10_000_000,
            rss_bytes: 100,
        },
        MemoryTimelinePoint {
            elapsed_ns: 111_000_000,
            rss_bytes: 200,
        },
    ];
    assert!(validate_timeline(&sparse, 9_000_000, 120_000_000, 5_000_000).is_err());

    let missed_leading_edge = vec![
        MemoryTimelinePoint {
            elapsed_ns: 110_000_001,
            rss_bytes: 100,
        },
        MemoryTimelinePoint {
            elapsed_ns: 115_000_001,
            rss_bytes: 200,
        },
    ];
    assert!(validate_timeline(&missed_leading_edge, 10_000_000, 116_000_000, 5_000_000).is_err());

    let missed_trailing_edge = vec![
        MemoryTimelinePoint {
            elapsed_ns: 10_000_000,
            rss_bytes: 100,
        },
        MemoryTimelinePoint {
            elapsed_ns: 15_000_000,
            rss_bytes: 200,
        },
    ];
    assert!(validate_timeline(&missed_trailing_edge, 9_000_000, 115_000_001, 5_000_000).is_err());
}

#[test]
fn sampler_must_observe_a_distinct_child_process() {
    assert!(validate_sampler_ownership(100, 101).is_ok());
    assert!(validate_sampler_ownership(100, 100).is_err());
}

#[test]
fn fixed_control_records_round_trip_without_dynamic_framing() {
    let record = ControlRecord {
        kind: ControlKind::WorkloadDrained,
        current_live_requested_bytes: 0,
        peak_live_requested_bytes: 12_345,
        checksum: 67_890,
    };
    let mut bytes = Vec::new();
    write_control_record(&mut bytes, record).unwrap();
    assert_eq!(bytes.len(), 32);
    assert_eq!(read_control_record(&mut bytes.as_slice()).unwrap(), record);
    bytes[5] = 1;
    assert!(read_control_record(&mut bytes.as_slice()).is_err());
}

#[test]
#[cfg(target_os = "linux")]
fn local_proc_sampler_reads_a_distinct_live_process() {
    let mut child = Command::new("sleep").arg("2").spawn().unwrap();
    let snapshot = read_proc_snapshot(child.id()).unwrap();
    assert!(snapshot.rss_bytes > 0);
    assert!(snapshot.hwm_bytes > 0);
    assert_ne!(std::process::id(), child.id());
    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn live_requested_oracle_matches_retain_then_drain() {
    let mut oracle = LiveRequestedOracle::default();
    oracle.allocate(4096).unwrap();
    oracle.allocate(8192).unwrap();
    oracle.free(4096).unwrap();
    oracle.allocate(2048).unwrap();
    oracle.free(8192).unwrap();
    oracle.free(2048).unwrap();
    assert_eq!(oracle.current_bytes(), 0);
    assert_eq!(oracle.peak_bytes(), 12_288);
    assert!(oracle.free(1).is_err());
}

#[test]
fn memory_matrix_is_exact_and_protocol_has_no_purge_message() {
    let cells = memory_scenario_cells(Topology {
        physical_cores: 8,
        logical_cores: 16,
    })
    .unwrap();
    assert_eq!(cells.len(), 7);
    assert!(cells.contains(&(CardId::LargeObjects, ThreadPoint::One, 1)));
    assert!(cells.contains(&(CardId::LargeObjects, ThreadPoint::Two, 2)));
    assert!(cells.contains(&(CardId::SawtoothRetainDrain, ThreadPoint::One, 1)));
    assert!(cells.contains(&(CardId::SawtoothRetainDrain, ThreadPoint::PhysicalCores, 8)));
    assert!(cells.contains(&(CardId::SmallLogMixed, ThreadPoint::PhysicalCores, 8)));
    assert!(cells.contains(&(
        CardId::CrossThreadProducerConsumer,
        ThreadPoint::PhysicalCores,
        8
    )));
    assert!(cells.contains(&(CardId::ThreadChurn, ThreadPoint::PhysicalCores, 8)));
    assert_eq!(
        ControlKind::ALL,
        [
            ControlKind::BaselineReady,
            ControlKind::Begin,
            ControlKind::WorkloadActive,
            ControlKind::WorkloadDrained,
            ControlKind::ExitResult,
        ]
    );
}

fn compatibility() -> MemoryCompatibility {
    MemoryCompatibility {
        metric_schema_version: MEMORY_SCHEMA_VERSION.into(),
        page_size_bytes: 4096,
        kernel: "6.8.0-fixture".into(),
        sampling_target_interval_ns: 5_000_000,
        purge_policy: "natural-only".into(),
        transparent_hugepage: "always [madvise] never".into(),
        cgroup_memory_max: "2147483648".into(),
        cgroup_memory_high: "max".into(),
        allocator_runtime_options: BTreeMap::from([
            ("MIMALLOC_MEMORY_EVENTS".into(), "0".into()),
            ("MIMALLOC_PROF".into(), "0".into()),
        ]),
    }
}

#[test]
fn memory_comparison_key_changes_for_protocol_compatibility_fields() {
    let original = memory_comparison_key(&compatibility()).unwrap();
    for mutate in [
        |value: &mut MemoryCompatibility| value.page_size_bytes = 65_536,
        |value: &mut MemoryCompatibility| value.kernel = "6.9.0-fixture".into(),
        |value: &mut MemoryCompatibility| value.metric_schema_version = "other-v1".into(),
    ] {
        let mut changed = compatibility();
        mutate(&mut changed);
        assert_ne!(memory_comparison_key(&changed).unwrap(), original);
    }
}

fn memory_fixture() -> MemoryRawRun {
    let throughput = synthetic_full_fixture().unwrap();
    let topology = Topology {
        physical_cores: throughput.runner.physical_cores as usize,
        logical_cores: throughput.runner.logical_cores as usize,
    };
    let cells = memory_scenario_cells(topology).unwrap();
    let cell_keys = cells
        .iter()
        .map(|(card, point, _)| (card.as_str(), point.name()))
        .collect::<Vec<_>>();
    let environment = MemoryEnvironment {
        page_size_bytes: 4096,
        kernel: throughput.runner.kernel.clone(),
        transparent_hugepage: "always [madvise] never".into(),
        cgroup_memory_max: "2147483648".into(),
        cgroup_memory_high: "max".into(),
        hosted_runner: throughput.runner.runner_class == "github-hosted",
        purge_policy: "natural-only".into(),
        allocator_runtime_options: BTreeMap::from([
            ("MIMALLOC_MEMORY_EVENTS".into(), "0".into()),
            ("MIMALLOC_PROF".into(), "0".into()),
        ]),
    };
    let baseline = 100 * 1024 * 1024_u64;
    let samples = throughput
        .samples
        .iter()
        .filter(|sample| {
            cell_keys.contains(&(sample.scenario_id.as_str(), sample.thread_point.as_str()))
        })
        .cloned()
        .map(|child_sample| {
            let delta_mib = match child_sample.allocator_id.as_str() {
                "tcmalloc" => 120,
                "jemalloc" => 110,
                "upstream-mimalloc" => 100,
                "bun-mimalloc" => 95,
                "mimalloc-pprof" => 90,
                _ => unreachable!(),
            } + u64::from(child_sample.block_id);
            let delta = delta_mib * 1024 * 1024;
            let sampled_peak = baseline + delta;
            let post = baseline + delta / 2;
            MemoryRawSample {
                metric_schema_version: MEMORY_SCHEMA_VERSION.into(),
                block_id: child_sample.block_id,
                ordinal: child_sample.ordinal,
                workload_seed: child_sample.workload_seed,
                allocator_id: child_sample.allocator_id.clone(),
                allocator_source_sha: child_sample.allocator_source_sha.clone(),
                child_binary_sha256: child_sample.child_binary_sha256.clone(),
                scenario_id: child_sample.scenario_id.clone(),
                thread_point: child_sample.thread_point.clone(),
                thread_count: child_sample.thread_count,
                baseline_ready_ns: 1_000_000,
                workload_active_ns: 9_000_000,
                workload_drained_ns: 21_000_000,
                post_drain_sample_100ms_ns: 121_000_000,
                post_drain_sample_1s_ns: 1_021_000_000,
                post_drain_sample_5s_ns: 5_021_000_000,
                sampler_pid: 100,
                sampled_pid: 101 + child_sample.ordinal as u32,
                baseline_rss_bytes: baseline,
                baseline_hwm_bytes: baseline,
                sampled_peak_rss_bytes: sampled_peak,
                kernel_peak_hwm_bytes: sampled_peak,
                peak_live_requested_bytes: child_sample.peak_live_requested_bytes,
                post_drain_rss_100ms_bytes: post,
                post_drain_rss_1s_bytes: post,
                post_drain_rss_5s_bytes: post,
                sampled_peak_rss_delta_bytes: delta as i64,
                post_drain_rss_delta_100ms_bytes: (delta / 2) as i64,
                post_drain_rss_delta_1s_bytes: (delta / 2) as i64,
                post_drain_rss_delta_5s_bytes: (delta / 2) as i64,
                fragmentation_proxy: delta as f64 / child_sample.peak_live_requested_bytes as f64,
                hwm_discrepancy: false,
                hwm_tolerance_bytes: (8 * 1024 * 1024).max(delta / 5),
                sampling: benchmark_suite::memory::SamplingIntervalDistribution {
                    target_interval_ns: MEMORY_SAMPLE_TARGET_NS,
                    sample_count: 3,
                    minimum_interval_ns: 5_000_000,
                    median_interval_ns: 5_000_000,
                    p95_interval_ns: 5_000_000,
                    maximum_interval_ns: 5_000_000,
                },
                timeline: vec![
                    MemoryTimelinePoint {
                        elapsed_ns: 10_000_000,
                        rss_bytes: baseline + delta / 2,
                    },
                    MemoryTimelinePoint {
                        elapsed_ns: 15_000_000,
                        rss_bytes: sampled_peak,
                    },
                    MemoryTimelinePoint {
                        elapsed_ns: 20_000_000,
                        rss_bytes: baseline + delta * 3 / 4,
                    },
                ],
                environment: environment.clone(),
                child_sample,
            }
        })
        .collect();
    MemoryRawRun {
        metric_schema_version: MEMORY_SCHEMA_VERSION.into(),
        status: "complete".into(),
        run_seed: throughput.run_seed,
        run: throughput.run,
        runner: throughput.runner,
        allocator_lock_sha256: throughput.allocator_lock_sha256,
        allocators: throughput.allocators,
        calibrations: throughput
            .calibrations
            .into_iter()
            .filter(|value| {
                cell_keys.contains(&(value.scenario_id.as_str(), value.thread_point.as_str()))
            })
            .collect(),
        samples,
    }
}

#[test]
fn complete_memory_fixture_uses_paired_lower_is_better_statistics() {
    let raw = memory_fixture();
    validate_memory_raw_run(&raw).unwrap();
    let report = build_memory_report(&raw).unwrap();
    assert_eq!(report.status, "complete");
    assert_eq!(report.raw_samples.len(), 7 * 15 * 5);
    assert_eq!(report.absolute_summaries.len(), 7 * 5 * 5);
    assert_eq!(report.paired_summaries.len(), 7 * 5 * 4);
    let planted = report
        .paired_summaries
        .iter()
        .find(|summary| {
            summary.metric_id == "sampled-peak-rss-bytes"
                && summary.summary.candidate_id == "mimalloc-pprof"
        })
        .unwrap();
    assert!(planted.summary.effect > 1.0);
}

#[test]
fn incomplete_mixed_or_untouched_memory_data_is_rejected() {
    let mut incomplete = memory_fixture();
    incomplete.samples.pop();
    assert!(validate_memory_raw_run(&incomplete).is_err());

    let mut mixed = memory_fixture();
    mixed.samples[0].environment.page_size_bytes = 65_536;
    assert!(validate_memory_raw_run(&mixed).is_err());

    let mut untouched = memory_fixture();
    untouched.samples[0].child_sample.checksum = 0;
    assert!(validate_memory_raw_run(&untouched).is_err());

    let mut wrong_seed = memory_fixture();
    wrong_seed.samples[0].child_sample.run_seed ^= 1;
    assert!(validate_memory_raw_run(&wrong_seed).is_err());

    let mut wrong_ordinal = memory_fixture();
    wrong_ordinal.samples[0].child_sample.ordinal = 3;
    assert!(validate_memory_raw_run(&wrong_ordinal).is_err());

    let mut wrong_provenance = memory_fixture();
    wrong_provenance.samples[0]
        .child_sample
        .allocator_library_sha256 = "f".repeat(64);
    assert!(validate_memory_raw_run(&wrong_provenance).is_err());

    let mut wrong_calibration = memory_fixture();
    wrong_calibration.calibrations[0].operation_count += 1;
    assert!(validate_memory_raw_run(&wrong_calibration).is_err());

    let mut wrong_calibration_threads = memory_fixture();
    wrong_calibration_threads.calibrations[0].thread_count += 1;
    assert!(validate_memory_raw_run(&wrong_calibration_threads).is_err());

    let mut mismatched_pair = memory_fixture();
    let original = mismatched_pair.samples[0].child_sample.checksum;
    mismatched_pair.samples[0].child_sample.checksum = original.wrapping_add(1);
    mismatched_pair.samples[0].child_sample.checksum =
        mismatched_pair.samples[0].child_sample.checksum.max(1);
    assert!(validate_memory_raw_run(&mismatched_pair).is_err());

    let mut zero_seed = memory_fixture();
    zero_seed.run_seed = 0;
    for sample in &mut zero_seed.samples {
        sample.child_sample.run_seed = 0;
    }
    assert!(validate_memory_raw_run(&zero_seed).is_err());
}

#[test]
fn old_latest_stays_pending_until_complete_memory_is_attached() {
    let throughput = synthetic_full_fixture().unwrap();
    let validation = validate_publication_raw(&throughput).unwrap();
    let (old, _) = build_latest_report(&throughput, validation).unwrap();
    let old_json = serde_json::to_vec(&old).unwrap();
    let mut decoded: LatestReport = serde_json::from_slice(&old_json).unwrap();
    assert!(decoded.memory.is_none());
    assert!(decoded
        .pending_metrics
        .iter()
        .any(|value| value.metric_id == "memory"));

    let report = build_memory_report(&memory_fixture()).unwrap();
    attach_memory_report(&mut decoded, report).unwrap();
    assert!(decoded.memory.is_some());
    assert!(!decoded
        .pending_metrics
        .iter()
        .any(|value| value.metric_id == "memory"));
}
