use benchmark_suite::{
    model::{
        AllocatorIdentity, BenchmarkChildRequest, RawRun, RawSample, RunnerMetadata,
        ToolchainMetadata, CHILD_PROTOCOL_VERSION,
    },
    orchestration::{
        balanced_block_orders, calibrate_cell_with, detect_planted_serialized_control,
        run_balanced_cell_with, validate_complete_raw_run, validate_near_balanced, CellRunPlan,
        ChildProgram, FrozenCalibration, ALLOCATOR_IDS,
    },
    CORE_SUITE_VERSION, RAW_SCHEMA_VERSION,
};
use std::time::Duration;

#[test]
fn fifteen_and_sixteen_block_orders_are_balanced_and_replayable() {
    for blocks in [15, 16] {
        let first = balanced_block_orders(blocks, 0xfeed).expect("orders");
        assert_eq!(
            first,
            balanced_block_orders(blocks, 0xfeed).expect("replay")
        );
        validate_near_balanced(&first).expect("near balanced ordinals");
    }
}

#[test]
fn calibration_count_is_identical_for_every_allocator() {
    let calibration =
        FrozenCalibration::new("tiny-fixed-16", 1, 1_000_000).expect("valid calibration");
    for allocator in ALLOCATOR_IDS {
        assert_eq!(calibration.apply_to(allocator).unwrap(), 1_000_000);
    }
}

#[test]
fn raw_run_requires_every_allocator_in_every_block() {
    let samples = (0..15)
        .flat_map(|block_id| {
            ALLOCATOR_IDS
                .into_iter()
                .enumerate()
                .map(move |(ordinal, allocator_id)| RawSample {
                    schema_version: RAW_SCHEMA_VERSION.into(),
                    suite_version: CORE_SUITE_VERSION.into(),
                    run_kind: "headline".into(),
                    execution_mode: "normal".into(),
                    run_seed: 1,
                    block_id,
                    ordinal: ordinal as u8,
                    workload_seed: block_id as u64,
                    allocator_id: allocator_id.into(),
                    allocator_version: "test".into(),
                    allocator_source_sha: "a".repeat(40),
                    allocator_library_sha256: "b".repeat(64),
                    child_binary_sha256: format!("{:0<64}", &"c".repeat(ordinal + 1)),
                    scenario_id: "tiny-fixed-16".into(),
                    scenario_version: CORE_SUITE_VERSION.into(),
                    thread_point: "1".into(),
                    thread_count: 1,
                    operation_unit: "transaction".into(),
                    operation_count: 10,
                    requested_transactions: 10,
                    completed_transactions: 10,
                    allocation_calls: 10,
                    calloc_calls: 0,
                    aligned_allocation_calls: 0,
                    free_calls: 10,
                    realloc_calls: 0,
                    setup_ns: 1,
                    warmup_ns: 1,
                    elapsed_ns: 1,
                    teardown_ns: 1,
                    throughput_operations_per_second: 10.0,
                    checksum: 1,
                    peak_live_requested_bytes: 16,
                    timed_out: false,
                    crashed: false,
                    exit_code: 0,
                    signal: None,
                    runner: RunnerMetadata {
                        os: "linux".into(),
                        architecture: "x86_64".into(),
                        physical_cores: 1,
                        logical_cores: 1,
                    },
                    toolchain: ToolchainMetadata {
                        rustc: "rustc test".into(),
                        target: "x86_64-unknown-linux-gnu".into(),
                        compiler: "cc".into(),
                        linker: "cc".into(),
                    },
                    reproduction_command: "benchmark-child".into(),
                })
        })
        .collect();
    let run = RawRun {
        schema_version: RAW_SCHEMA_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run_kind: "headline".into(),
        execution_mode: "normal".into(),
        run_seed: 1,
        samples,
    };
    validate_complete_raw_run(&run, 15).expect("complete blocks");
    let mut incomplete = run.clone();
    incomplete.samples.pop();
    assert!(validate_complete_raw_run(&incomplete, 15).is_err());
}

#[test]
fn a_failed_child_aborts_without_retry_or_complete_raw_run() {
    let plan = plan();
    let mut calls = 0;
    let mut appended = 0;
    let result = run_balanced_cell_with(
        &plan,
        |_| {
            appended += 1;
            Ok(())
        },
        |_, request, _| {
            calls += 1;
            if calls == 8 {
                Err("planted child failure".into())
            } else {
                Ok(sample_for(request, 1_000_000_000))
            }
        },
    );
    assert!(result.unwrap_err().contains("planted child failure"));
    assert_eq!(calls, 8, "a failed allocator is never selectively retried");
    assert_eq!(
        appended, 7,
        "only successful samples reach append-only JSONL"
    );
}

#[test]
fn complete_controller_path_retains_all_five_raw_samples_per_block() {
    let plan = plan();
    let mut appended = 0;
    let run = run_balanced_cell_with(
        &plan,
        |_| {
            appended += 1;
            Ok(())
        },
        |_, request, _| Ok(sample_for(request, 750_000_000)),
    )
    .unwrap();
    assert_eq!(appended, 75);
    assert_eq!(run.samples.len(), 75);
    validate_complete_raw_run(&run, 15).unwrap();
}

#[test]
fn upstream_calibration_freezes_a_realized_count_in_target_interval() {
    let plan = plan();
    let upstream = plan
        .children
        .iter()
        .find(|child| child.allocator.allocator_id == "upstream-mimalloc")
        .unwrap();
    let calibration = calibrate_cell_with(
        upstream,
        &plan.request_template,
        Duration::from_secs(5),
        |_, request, _| {
            Ok(sample_for(
                request,
                request.transactions_per_worker * 100_000_000,
            ))
        },
    )
    .unwrap();
    assert_eq!(calibration.transactions_per_worker, 10);
    assert_eq!(calibration.operation_count, 10);
    assert_eq!(calibration.elapsed_ns, 1_000_000_000);
}

#[test]
fn planted_serialized_control_is_detected_through_five_child_blocks() {
    let mut normal_plan = plan();
    normal_plan.request_template.thread_point = "physical-core".into();
    normal_plan.request_template.physical_cores = 2;
    normal_plan.request_template.logical_cores = 2;
    normal_plan.request_template.runner.physical_cores = 2;
    normal_plan.request_template.runner.logical_cores = 2;
    let normal = run_balanced_cell_with(
        &normal_plan,
        |_| Ok(()),
        |_, request, _| Ok(sample_for(request, 1_000_000_000)),
    )
    .unwrap();
    let mut control_plan = normal_plan.clone();
    control_plan.request_template.execution_mode = "serialized-control".into();
    let control = run_balanced_cell_with(
        &control_plan,
        |_| Ok(()),
        |_, request, _| Ok(sample_for(request, 2_000_000_000)),
    )
    .unwrap();
    assert_eq!(
        detect_planted_serialized_control(&normal, &control).unwrap(),
        2.0
    );
}

#[test]
fn one_block_is_allowed_only_when_explicitly_marked_reduced_smoke() {
    let mut headline = plan();
    headline.blocks = 1;
    assert!(run_balanced_cell_with(
        &headline,
        |_| Ok(()),
        |_, request, _| Ok(sample_for(request, 1))
    )
    .is_err());
    headline.request_template.run_kind = "reduced-smoke".into();
    let smoke = run_balanced_cell_with(
        &headline,
        |_| Ok(()),
        |_, request, _| Ok(sample_for(request, 1)),
    )
    .unwrap();
    assert_eq!(smoke.run_kind, "reduced-smoke");
    assert_eq!(smoke.samples.len(), 5);
}

fn plan() -> CellRunPlan {
    let children = ALLOCATOR_IDS
        .into_iter()
        .enumerate()
        .map(|(index, allocator_id)| ChildProgram {
            allocator: AllocatorIdentity {
                allocator_id: allocator_id.into(),
                allocator_version: "test".into(),
                source_sha: ["a", "b", "c", "d", "e"][index].repeat(40),
                library_sha256: ["1", "2", "3", "4", "5"][index].repeat(64),
                child_binary_sha256: ["6", "7", "8", "9", "a"][index].repeat(64),
            },
            program: format!("benchmark-child-{allocator_id}").into(),
            arguments: Vec::new(),
            toolchain: ToolchainMetadata {
                rustc: "rustc test".into(),
                target: "x86_64-unknown-linux-gnu".into(),
                compiler: "cc".into(),
                linker: "cc".into(),
            },
            environment: Vec::new(),
        })
        .collect::<Vec<_>>();
    CellRunPlan {
        request_template: BenchmarkChildRequest {
            protocol_version: CHILD_PROTOCOL_VERSION.into(),
            schema_version: RAW_SCHEMA_VERSION.into(),
            suite_version: CORE_SUITE_VERSION.into(),
            run_kind: "headline".into(),
            execution_mode: "normal".into(),
            run_seed: 0x1234,
            block_id: 0,
            ordinal: 0,
            workload_seed: 0,
            allocator: children[0].allocator.clone(),
            scenario_id: "tiny-fixed-16".into(),
            scenario_version: CORE_SUITE_VERSION.into(),
            thread_point: "1".into(),
            physical_cores: 1,
            logical_cores: 1,
            transactions_per_worker: 1,
            warmup_transactions_per_worker: 0,
            reproduction_command: "replaced by controller".into(),
            runner: RunnerMetadata {
                os: "linux".into(),
                architecture: "x86_64".into(),
                physical_cores: 1,
                logical_cores: 1,
            },
            toolchain: ToolchainMetadata {
                rustc: "rustc test".into(),
                target: "x86_64-unknown-linux-gnu".into(),
                compiler: "cc".into(),
                linker: "cc".into(),
            },
        },
        children,
        blocks: 15,
        timeout: Duration::from_secs(5),
        request_directory: None,
    }
}

fn sample_for(request: &BenchmarkChildRequest, elapsed_ns: u64) -> RawSample {
    let threads = if request.thread_point == "physical-core" {
        request.physical_cores as u64
    } else {
        1
    };
    let transactions = request.transactions_per_worker * threads;
    RawSample {
        schema_version: request.schema_version.clone(),
        suite_version: request.suite_version.clone(),
        run_kind: request.run_kind.clone(),
        execution_mode: request.execution_mode.clone(),
        run_seed: request.run_seed,
        block_id: request.block_id,
        ordinal: request.ordinal,
        workload_seed: request.workload_seed,
        allocator_id: request.allocator.allocator_id.clone(),
        allocator_version: request.allocator.allocator_version.clone(),
        allocator_source_sha: request.allocator.source_sha.clone(),
        allocator_library_sha256: request.allocator.library_sha256.clone(),
        child_binary_sha256: request.allocator.child_binary_sha256.clone(),
        scenario_id: request.scenario_id.clone(),
        scenario_version: request.scenario_version.clone(),
        thread_point: request.thread_point.clone(),
        thread_count: threads as u32,
        operation_unit: "transaction".into(),
        operation_count: transactions,
        requested_transactions: transactions,
        completed_transactions: transactions,
        allocation_calls: transactions,
        calloc_calls: 0,
        aligned_allocation_calls: 0,
        free_calls: transactions,
        realloc_calls: 0,
        setup_ns: 1,
        warmup_ns: 0,
        elapsed_ns,
        teardown_ns: 1,
        throughput_operations_per_second: transactions as f64 * 1_000_000_000.0 / elapsed_ns as f64,
        checksum: 99,
        peak_live_requested_bytes: 16,
        timed_out: false,
        crashed: false,
        exit_code: 0,
        signal: None,
        runner: request.runner.clone(),
        toolchain: request.toolchain.clone(),
        reproduction_command: request.reproduction_command.clone(),
    }
}
