use std::collections::BTreeMap;

use benchmark_suite::execution::expected_touch_checksum;
use benchmark_suite::latency::{
    block_bootstrap_quantile_effect, deterministic_sample_indices, overhead_is_valid,
    summarize_latency, transaction_definition, validate_latency_raw_run, ContextSwitchCounts,
    LatencyChildResponse, LatencyClock, LatencyObservation, LatencyRawRun, LatencyRawSample,
    LatencyScheduling, LATENCY_CHILD_PROTOCOL_VERSION, LATENCY_SCHEMA_VERSION,
};
use benchmark_suite::model::CellCalibration;
use benchmark_suite::scenarios::{card, CardId, ScenarioCell, ThreadPoint, Topology};
use benchmark_suite::validate::synthetic_full_fixture;

fn response(control: bool, values: &[u64]) -> LatencyChildResponse {
    LatencyChildResponse {
        protocol_version: LATENCY_CHILD_PROTOCOL_VERSION.into(),
        metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
        control,
        completed_transactions: values.len() as u64,
        checksum: 1,
        observations: values
            .iter()
            .enumerate()
            .map(|(index, duration_ns)| LatencyObservation {
                thread_index: 0,
                transaction_index: index as u64,
                duration_ns: *duration_ns,
            })
            .collect(),
        scheduling: LatencyScheduling {
            affinity_policy: "linux:unrestricted".into(),
            actual_cpu_ids: vec![Some(0)],
            thread_count: 1,
            physical_cores: 1,
            logical_cores: 1,
            context_switches: ContextSwitchCounts {
                voluntary: 0,
                involuntary: 0,
            },
            runner_class: "test".into(),
            clock: LatencyClock {
                source: "monotonic".into(),
                implementation: "fixture".into(),
                resolution_ns: 1,
            },
        },
    }
}

fn sample(block: u32, allocator: &str, measured: &[u64]) -> LatencyRawSample {
    LatencyRawSample {
        metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
        block_id: block,
        ordinal: 0,
        workload_seed: 17 + u64::from(block),
        allocator_id: allocator.into(),
        allocator_source_sha: "a".repeat(40),
        child_binary_sha256: "b".repeat(64),
        scenario_id: "tiny-fixed-64".into(),
        thread_point: "1".into(),
        thread_count: 1,
        sample_denominator: 1,
        transaction_definition: transaction_definition(CardId::TinyFixed64).into(),
        measured: response(false, measured),
        control: response(true, &vec![1; measured.len()]),
    }
}

#[test]
fn reciprocal_throughput_is_not_latency_input() {
    let reciprocal = br#"{"metric_schema_version":"transaction-latency-v1","ns_per_op":12.5}"#;
    assert!(serde_json::from_slice::<benchmark_suite::latency::LatencyRawRun>(reciprocal).is_err());
}

#[test]
fn deterministic_schedule_replays_and_pairs_allocators() {
    let first = deterministic_sample_indices(123, 2, 10_000, 1024).unwrap();
    let second = deterministic_sample_indices(123, 2, 10_000, 1024).unwrap();
    let other_seed = deterministic_sample_indices(124, 2, 10_000, 1024).unwrap();
    assert_eq!(first, second);
    assert_ne!(first, other_seed);
    assert!(first.windows(2).all(|pair| pair[1] - pair[0] == 1024));
}

#[test]
fn type7_quantiles_mad_iqr_and_extreme_tail_are_retained() {
    let ordinary = summarize_latency(&[1, 2, 3, 4]).unwrap();
    assert_eq!(ordinary.p50_ns, 2.5);
    assert_eq!(ordinary.p95_ns, 3.8499999999999996);
    assert_eq!(ordinary.iqr_ns, 1.5);
    assert_eq!(ordinary.median_absolute_deviation_ns, 1.0);

    let mut tail = vec![100; 99];
    tail.push(100_000);
    let summary = summarize_latency(&tail).unwrap();
    assert_eq!(summary.count, 100);
    assert_eq!(summary.max_ns, 100_000);
    assert!(summary.p99_ns > 100.0);
}

#[test]
fn zero_duration_and_overhead_thresholds_fail_closed() {
    assert!(summarize_latency(&[1, 0, 2]).is_err());
    let measured = summarize_latency(&vec![1_000; 100]).unwrap();
    let good_control = summarize_latency(&vec![40; 100]).unwrap();
    let median_too_high = summarize_latency(&vec![51; 100]).unwrap();
    let tail_too_close = summarize_latency(&vec![600; 100]).unwrap();
    assert!(overhead_is_valid(&measured, &good_control));
    assert!(!overhead_is_valid(&measured, &median_too_high));
    assert!(!overhead_is_valid(&measured, &tail_too_close));
}

#[test]
fn block_bootstrap_keeps_within_block_transactions_and_is_lower_better() {
    let samples = vec![
        sample(0, "mimalloc-pprof", &[50, 50, 50]),
        sample(0, "upstream-mimalloc", &[100, 100, 100]),
        sample(1, "mimalloc-pprof", &[100, 100, 100]),
        sample(1, "upstream-mimalloc", &[200, 200, 200]),
    ];
    let effect = block_bootstrap_quantile_effect(
        99,
        "tiny-fixed-64/1/p99",
        "mimalloc-pprof",
        "upstream-mimalloc",
        0.99,
        &samples,
    )
    .unwrap();
    assert_eq!(effect.effect, 2.0);
    assert_eq!(effect.confidence_interval.lower, 2.0);
    assert_eq!(effect.confidence_interval.upper, 2.0);
    assert_eq!(effect.block_count, 2);
    assert_eq!(
        effect.bootstrap.method,
        "percentile-whole-block-transaction-quantile-type7-v1"
    );
}

#[test]
fn transaction_labels_are_end_to_end_not_allocator_calls() {
    for card in [
        CardId::TinyFixed64,
        CardId::SmallLogMixed,
        CardId::CrossThreadProducerConsumer,
        CardId::LargeObjects,
    ] {
        let definition = transaction_definition(card);
        assert!(!definition.contains("allocator-call"));
        assert!(definition.contains("allocation"));
        assert!(definition.contains("free"));
    }
}

fn complete_raw_fixture() -> LatencyRawRun {
    let throughput = synthetic_full_fixture().unwrap();
    let topology = Topology {
        physical_cores: throughput.runner.physical_cores as usize,
        logical_cores: throughput.runner.logical_cores as usize,
    };
    let cells = benchmark_suite::latency::latency_scenario_cells(topology).unwrap();
    let cell_keys = cells
        .iter()
        .map(|(card, point, _)| (card.as_str(), point.name()))
        .collect::<Vec<_>>();
    let mut transactions = BTreeMap::new();
    let calibrations = cells
        .iter()
        .map(|(card_id, point, _)| {
            let threads = topology.resolve(*point).unwrap();
            let count = benchmark_suite::latency::minimum_transactions_per_worker(
                threads, 15, 1, 10_000,
            )
            .unwrap();
            transactions.insert((card_id.as_str(), point.name()), count);
            let cell = ScenarioCell::new(*card_id, *point, topology, count, 1).unwrap();
            CellCalibration {
                scenario_id: card_id.as_str().into(),
                thread_point: point.name().into(),
                thread_count: threads as u32,
                transactions_per_worker: count,
                warmup_transactions_per_worker: 1,
                operation_count: card(*card_id).operation_count(&cell.expected_counts().unwrap()),
                elapsed_ns: 1_000_000,
            }
        })
        .collect::<Vec<_>>();
    let orders = benchmark_suite::orchestration::balanced_block_orders(15, throughput.run_seed)
        .unwrap();
    let samples = throughput
        .samples
        .iter()
        .filter(|sample| {
            cell_keys.contains(&(sample.scenario_id.as_str(), sample.thread_point.as_str()))
        })
        .map(|sample| {
            let order = &orders[sample.block_id as usize];
            let card_id = CardId::parse(&sample.scenario_id).unwrap();
            let point = ThreadPoint::parse(&sample.thread_point).unwrap();
            let count = transactions[&(card_id.as_str(), point.name())];
            let cell = ScenarioCell::new(card_id, point, topology, count, order.workload_seed)
                .unwrap();
            let schedule = (0..cell.threads)
                .flat_map(|worker| {
                    deterministic_sample_indices(
                        cell.seed,
                        worker as u32,
                        cell.transactions_per_worker,
                        1,
                    )
                    .unwrap()
                    .into_iter()
                    .map(move |transaction_index| (worker as u32, transaction_index))
                })
                .collect::<Vec<_>>();
            let allocator_offset = match sample.allocator_id.as_str() {
                "tcmalloc" => 300,
                "jemalloc" => 200,
                "upstream-mimalloc" => 100,
                "bun-mimalloc" => 50,
                "mimalloc-pprof" => 0,
                _ => unreachable!(),
            };
            let scheduling = LatencyScheduling {
                affinity_policy: throughput.runner.affinity.policy.clone(),
                actual_cpu_ids: vec![None; cell.threads],
                thread_count: cell.threads as u32,
                physical_cores: throughput.runner.physical_cores,
                logical_cores: throughput.runner.logical_cores,
                context_switches: ContextSwitchCounts {
                    voluntary: 0,
                    involuntary: 0,
                },
                runner_class: throughput.runner.runner_class.clone(),
                clock: LatencyClock {
                    source: "monotonic".into(),
                    implementation: "fixture-clock".into(),
                    resolution_ns: 1,
                },
            };
            let observations = schedule
                .iter()
                .map(|(thread_index, transaction_index)| LatencyObservation {
                    thread_index: *thread_index,
                    transaction_index: *transaction_index,
                    duration_ns: 1_000 + allocator_offset + transaction_index % 41,
                })
                .collect::<Vec<_>>();
            let controls = schedule
                .iter()
                .map(|(thread_index, transaction_index)| LatencyObservation {
                    thread_index: *thread_index,
                    transaction_index: *transaction_index,
                    duration_ns: 40 + transaction_index % 3,
                })
                .collect::<Vec<_>>();
            LatencyRawSample {
                metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
                block_id: sample.block_id,
                ordinal: order
                    .allocator_ids
                    .iter()
                    .position(|allocator| allocator == &sample.allocator_id)
                    .unwrap() as u8,
                workload_seed: order.workload_seed,
                allocator_id: sample.allocator_id.clone(),
                allocator_source_sha: sample.allocator_source_sha.clone(),
                child_binary_sha256: sample.child_binary_sha256.clone(),
                scenario_id: sample.scenario_id.clone(),
                thread_point: sample.thread_point.clone(),
                thread_count: cell.threads as u32,
                sample_denominator: 1,
                transaction_definition: transaction_definition(card_id).into(),
                measured: LatencyChildResponse {
                    protocol_version: LATENCY_CHILD_PROTOCOL_VERSION.into(),
                    metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
                    control: false,
                    completed_transactions: cell.requested_transactions(),
                    checksum: expected_touch_checksum(&cell).unwrap(),
                    observations,
                    scheduling: scheduling.clone(),
                },
                control: LatencyChildResponse {
                    protocol_version: LATENCY_CHILD_PROTOCOL_VERSION.into(),
                    metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
                    control: true,
                    completed_transactions: cell.requested_transactions(),
                    checksum: 1,
                    observations: controls,
                    scheduling,
                },
            }
        })
        .collect::<Vec<_>>();
    LatencyRawRun {
        metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
        status: "complete".into(),
        run_seed: throughput.run_seed,
        run: throughput.run,
        runner: throughput.runner,
        allocator_lock_sha256: throughput.allocator_lock_sha256,
        allocators: throughput.allocators,
        calibrations,
        sampling_denominators: cells
            .into_iter()
            .map(|(card, point, _)| (format!("{}/{}", card.as_str(), point.name()), 1))
            .collect(),
        samples,
    }
}

#[test]
fn complete_latency_raw_matrix_is_bound_to_calibration_provenance_and_schedule() {
    let raw = complete_raw_fixture();
    validate_latency_raw_run(&raw).unwrap();

    {
        let mut wrong_schedule = raw.clone();
        wrong_schedule.samples[0].measured.observations[0].transaction_index += 1;
        assert!(validate_latency_raw_run(&wrong_schedule).is_err());
    }
    {
        let mut wrong_provenance = raw.clone();
        wrong_provenance.samples[0].child_binary_sha256 = "f".repeat(64);
        assert!(validate_latency_raw_run(&wrong_provenance).is_err());
    }
    {
        let mut mixed_clock = raw.clone();
        mixed_clock.samples[0]
            .measured
            .scheduling
            .clock
            .resolution_ns = 2;
        mixed_clock.samples[0]
            .control
            .scheduling
            .clock
            .resolution_ns = 2;
        assert!(validate_latency_raw_run(&mixed_clock).is_err());
    }
    let mut wrong_checksum = raw;
    wrong_checksum.samples[0].measured.checksum ^= 1;
    assert!(validate_latency_raw_run(&wrong_checksum).is_err());
}
