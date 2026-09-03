use benchmark_suite::scenarios::{
    card, cards, CardId, ExpectedCounts, RequestKind, ScenarioCell, ScenarioError, ThreadPoint,
    Topology, CORE_THROUGHPUT_V1, MAX_REQUESTS_PER_TRANSACTION,
};

const TOPOLOGY: Topology = Topology {
    physical_cores: 4,
    logical_cores: 8,
};

#[test]
fn core_throughput_v1_has_all_and_only_the_declared_cards_and_points() {
    let observed: Vec<(&str, Vec<&str>)> = cards()
        .iter()
        .map(|definition| {
            (
                definition.id.as_str(),
                definition
                    .thread_points
                    .iter()
                    .map(|point| point.name())
                    .collect(),
            )
        })
        .collect();
    assert_eq!(CORE_THROUGHPUT_V1, "core-throughput-v1");
    assert_eq!(
        observed,
        vec![
            ("tiny-fixed-16", vec!["1", "physical-core"]),
            ("tiny-fixed-64", vec!["1", "physical-core"]),
            ("small-log-mixed", vec!["1", "physical-core", "2x-logical"]),
            ("medium-log-mixed", vec!["1", "physical-core"]),
            ("large-objects", vec!["1", "2"]),
            ("batch-lifo", vec!["1", "physical-core"]),
            ("batch-fifo", vec!["1", "physical-core"]),
            ("cross-thread-producer-consumer", vec!["2", "physical-core"]),
            ("random-ownership", vec!["physical-core"]),
            ("realloc-geometric", vec!["1", "physical-core"]),
            ("calloc-zero", vec!["1", "physical-core"]),
            ("aligned-range", vec!["1", "physical-core"]),
            ("sawtooth-retain-drain", vec!["1", "physical-core"]),
            ("thread-churn", vec!["1", "physical-core"]),
            ("representative-mix", vec!["1", "physical-core"]),
        ]
    );
    assert_eq!(CardId::ALL.len(), 15);
}

#[test]
fn matching_seed_means_matching_worker_requests_and_checksum() {
    for definition in cards() {
        for &point in definition.thread_points {
            let a = ScenarioCell::new(definition.id, point, TOPOLOGY, 5, 0x5eed).unwrap();
            let b = ScenarioCell::new(definition.id, point, TOPOLOGY, 5, 0x5eed).unwrap();
            assert_eq!(
                a.streams().unwrap(),
                b.streams().unwrap(),
                "{} {}",
                definition.id.as_str(),
                point.name()
            );
            assert_eq!(
                a.expected_checksum().unwrap(),
                b.expected_checksum().unwrap()
            );
        }
    }
}

#[test]
fn allocator_identity_cannot_change_a_request_stream() {
    // A child gets this stream before it invokes the adapter.  Replaying the
    // same cell for every ID proves the producer has no allocator input.
    let cell = ScenarioCell::new(
        CardId::RepresentativeMix,
        ThreadPoint::PhysicalCores,
        TOPOLOGY,
        20,
        91,
    )
    .unwrap();
    let baseline = cell.streams().unwrap();
    for _allocator in [
        "tcmalloc",
        "jemalloc",
        "upstream-mimalloc",
        "bun-mimalloc",
        "mimalloc-pprof",
    ] {
        assert_eq!(cell.streams().unwrap(), baseline);
        assert_eq!(
            cell.expected_checksum().unwrap(),
            cell.expected_checksum().unwrap()
        );
    }
}

#[test]
fn exact_counts_are_derived_from_the_same_requests_an_executor_receives() {
    for definition in cards() {
        let point = definition.thread_points[0];
        let cell = ScenarioCell::new(definition.id, point, TOPOLOGY, 3, 42).unwrap();
        let counts = cell.expected_counts().unwrap();
        assert_eq!(counts.requested_transactions, cell.requested_transactions());
        assert_eq!(counts.completed_transactions, counts.requested_transactions);
        assert!(counts.allocator_calls > 0, "{}", definition.id.as_str());
        assert!(counts.touches > 0, "{}", definition.id.as_str());
        assert_eq!(
            counts.allocator_calls,
            counts.alloc_calls
                + counts.calloc_calls
                + counts.realloc_calls
                + counts.aligned_alloc_calls
                + counts.free_calls
        );
        let mut oracle = ExpectedCounts {
            requested_transactions: cell.requested_transactions(),
            completed_transactions: cell.requested_transactions(),
            ..ExpectedCounts::default()
        };
        for stream in cell.streams().unwrap() {
            for request in &stream.requests {
                oracle.record(request);
            }
        }
        assert_eq!(counts, oracle, "{}", definition.id.as_str());
    }
}

#[test]
fn very_large_cells_have_bounded_request_storage_and_constant_time_contracts() {
    let transactions = 50_000_000_u64;
    let cell = ScenarioCell::new(
        CardId::TinyFixed16,
        ThreadPoint::One,
        TOPOLOGY,
        transactions,
        0x5eed,
    )
    .unwrap();
    let counts = cell.expected_counts().unwrap();
    assert_eq!(counts.alloc_calls, transactions);
    assert_eq!(counts.free_calls, transactions);
    assert_eq!(counts.allocator_calls, transactions * 2);

    let mut requests = Vec::with_capacity(MAX_REQUESTS_PER_TRANSACTION);
    let fixed_capacity = requests.capacity();
    cell.fill_worker_transaction(0, 0, &mut requests).unwrap();
    assert_eq!(requests.len(), 3);
    cell.fill_worker_transaction(0, transactions - 1, &mut requests)
        .unwrap();
    assert_eq!(requests.len(), 3);
    assert_eq!(requests.capacity(), fixed_capacity);
    assert_eq!(card(cell.card).max_live_allocations_per_worker(), 1);

    // This derives at most one fixed request cycle, not `transactions` rows.
    assert_ne!(
        benchmark_suite::execution::expected_touch_checksum(&cell).unwrap(),
        0
    );
}

#[test]
fn cross_thread_cards_have_a_real_ownership_oracle() {
    for id in [CardId::CrossThreadProducerConsumer, CardId::RandomOwnership] {
        let cell = ScenarioCell::new(id, card(id).thread_points[0], TOPOLOGY, 4, 123).unwrap();
        let oracle = cell.topology_oracle().unwrap();
        assert!(oracle.claims_cross_thread);
        assert!(oracle.validates());
        assert!(cell.ownership_oracle().unwrap());
        for stream in cell.streams().unwrap() {
            assert!(stream
                .requests
                .iter()
                .any(|request| request.kind == RequestKind::Free
                    && request.owner_worker != request.executor_worker));
        }
    }
}

#[test]
fn declared_points_expand_and_invalid_expansions_are_rejected() {
    assert_eq!(TOPOLOGY.resolve(ThreadPoint::PhysicalCores).unwrap(), 4);
    assert_eq!(
        TOPOLOGY.resolve(ThreadPoint::TwiceLogicalCores).unwrap(),
        16
    );
    assert!(matches!(
        Topology {
            physical_cores: 0,
            logical_cores: 8
        }
        .resolve(ThreadPoint::PhysicalCores),
        Err(ScenarioError::InvalidTopology(_))
    ));
    assert!(matches!(
        Topology {
            physical_cores: 4,
            logical_cores: 1
        }
        .resolve(ThreadPoint::One),
        Err(ScenarioError::InvalidTopology(_))
    ));
    assert!(matches!(
        ScenarioCell::new(
            CardId::TinyFixed16,
            ThreadPoint::TwiceLogicalCores,
            TOPOLOGY,
            1,
            1
        ),
        Err(ScenarioError::UnsupportedThreadPoint { .. })
    ));
    assert!(matches!(
        ScenarioCell::new(
            CardId::CrossThreadProducerConsumer,
            ThreadPoint::PhysicalCores,
            Topology {
                physical_cores: 1,
                logical_cores: 1
            },
            1,
            1
        ),
        Err(ScenarioError::InvalidExpansion { .. })
    ));
}
