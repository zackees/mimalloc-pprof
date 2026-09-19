use std::alloc::{alloc, dealloc, realloc, Layout};
use std::collections::HashMap;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use benchmark_suite::execution::AllocatorAdapter;
use benchmark_suite::model::{
    AllocatorIdentity, BenchmarkChildRequest, RunnerMetadata, ToolchainMetadata,
    CHILD_PROTOCOL_VERSION,
};
use benchmark_suite::pprof_tax;
use benchmark_suite::pprof_tax::PprofTaxConfiguration;
use benchmark_suite::pprof_tax_adapter::FakeProfiler;
use benchmark_suite::pprof_tax_child::{execute_pprof_tax_child_request, PprofTaxChildRequest};
use benchmark_suite::provenance::sha256_file;
use benchmark_suite::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

/// Leak-detecting mock allocator, copied from `tests/scaling_contract.rs` so
/// this crate does not need a native adapter linked.
struct MockAdapter {
    id: &'static str,
    layouts: Mutex<HashMap<usize, Layout>>,
    frees: AtomicU64,
}

impl MockAdapter {
    fn new(id: &'static str) -> Self {
        Self {
            id,
            layouts: Mutex::new(HashMap::new()),
            frees: AtomicU64::new(0),
        }
    }
}

impl Drop for MockAdapter {
    fn drop(&mut self) {
        assert!(
            self.layouts.get_mut().unwrap().is_empty(),
            "pprof-tax workload leaked blocks"
        );
    }
}

impl AllocatorAdapter for MockAdapter {
    fn allocator_id(&self) -> &str {
        self.id
    }
    fn allocator_version(&self) -> &str {
        "test"
    }
    fn source_sha(&self) -> &str {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
    fn library_sha256(&self) -> &str {
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
    fn alloc(&self, size: usize) -> Result<NonNull<u8>, String> {
        let layout = Layout::from_size_align(size.max(1), 16).unwrap();
        let pointer = NonNull::new(unsafe { alloc(layout) }).ok_or("mock allocation failed")?;
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
        Ok(pointer)
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.alloc(count * size)
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        let address = pointer.as_ptr() as usize;
        let old = self
            .layouts
            .lock()
            .unwrap()
            .remove(&address)
            .ok_or("unknown realloc pointer")?;
        let updated = NonNull::new(unsafe { realloc(pointer.as_ptr(), old, size.max(1)) })
            .ok_or("mock realloc failed")?;
        self.layouts.lock().unwrap().insert(
            updated.as_ptr() as usize,
            Layout::from_size_align(size.max(1), old.align()).unwrap(),
        );
        Ok(updated)
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        let layout = Layout::from_size_align(size.max(1), alignment).unwrap();
        let pointer = NonNull::new(unsafe { alloc(layout) }).ok_or("mock aligned alloc failed")?;
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
        Ok(pointer)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        let layout = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .expect("mock free of an unknown pointer");
        self.frees.fetch_add(1, Ordering::Relaxed);
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

/// Unique scratch directory under the system temp dir, removed on drop even
/// if the test panics.
struct TempDirGuard(std::path::PathBuf);

impl TempDirGuard {
    fn new(label: &str) -> Self {
        let mut path = std::env::temp_dir();
        let unique = format!(
            "pprof-tax-child-contract-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        path.push(unique);
        std::fs::create_dir_all(&path).expect("create pprof-tax contract temp dir");
        Self(path)
    }

    fn profile_path(&self) -> std::path::PathBuf {
        self.0.join("profile.pb")
    }
}

impl Drop for TempDirGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn configuration_for(id: &str) -> &'static PprofTaxConfiguration {
    pprof_tax::configuration(id)
        .unwrap_or_else(|| panic!("pprof-tax configuration table is missing {id}"))
}

fn mock_identity() -> AllocatorIdentity {
    AllocatorIdentity {
        allocator_id: "mock-allocator".into(),
        allocator_version: "test".into(),
        source_sha: "a".repeat(40),
        library_sha256: "b".repeat(64),
        // Must equal the executable digest every test passes ("d" * 64): the
        // child rejects a request whose echoed binary digest differs.
        child_binary_sha256: "d".repeat(64),
    }
}

fn inner_request(
    allocator: AllocatorIdentity,
    transactions_per_worker: u64,
) -> BenchmarkChildRequest {
    BenchmarkChildRequest {
        protocol_version: CHILD_PROTOCOL_VERSION.into(),
        schema_version: RAW_SCHEMA_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run_kind: "headline".into(),
        execution_mode: "normal".into(),
        run_seed: 0x6d69_6d61_6c6c_6f63,
        block_id: 0,
        ordinal: 0,
        workload_seed: 0x1234_5678_9abc_def1,
        allocator,
        scenario_id: "tiny-fixed-64".into(),
        scenario_version: CORE_SUITE_VERSION.into(),
        thread_point: "1".into(),
        physical_cores: 1,
        logical_cores: 1,
        transactions_per_worker,
        warmup_transactions_per_worker: 0,
        reproduction_command: "test".into(),
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
    }
}

fn pprof_tax_request(
    configuration: &PprofTaxConfiguration,
    inner: BenchmarkChildRequest,
    profile_path: Option<String>,
) -> PprofTaxChildRequest {
    PprofTaxChildRequest {
        protocol_version: pprof_tax::PPROF_TAX_CHILD_PROTOCOL_VERSION.into(),
        configuration_id: configuration.configuration_id.to_string(),
        compiled_configuration_id: configuration.compiled_configuration_id.to_string(),
        pprof_active: configuration.pprof_active,
        sampling_interval_bytes: configuration.sampling_interval_bytes,
        profiler_seed: 0x2468_1357_9bdf_1234,
        profile_path,
        inner,
    }
}

#[test]
fn sparse_active_request_dumps_a_stable_profile_and_calls_the_profiler_in_order() {
    let configuration = configuration_for("fork-pprof-sparse");
    assert!(
        configuration.pprof_active,
        "fork-pprof-sparse must be an active configuration"
    );
    let executable_sha256 = "d".repeat(64);

    let mut hashes = Vec::new();
    for attempt in 0..2 {
        let guard = TempDirGuard::new(&format!("sparse-active-{attempt}"));
        let profile_path = guard.profile_path();

        let profiler = FakeProfiler::new(
            configuration.compiled_configuration_id,
            configuration.pprof_compiled,
        )
        .with_planted(12, 4096, 0, 65536);
        let adapter = MockAdapter::new("mock-allocator");
        let inner = inner_request(mock_identity(), 4);
        let request = pprof_tax_request(
            configuration,
            inner,
            Some(profile_path.to_string_lossy().into_owned()),
        );

        let response =
            execute_pprof_tax_child_request(&adapter, &profiler, request, &executable_sha256)
                .expect("sparse active request succeeds");

        assert!(response.telemetry.sample_count > 0);
        assert!(response.profile_written);
        assert!(
            profile_path.exists(),
            "dump_proto must create the profile file"
        );

        let calls = profiler.calls();
        assert_eq!(
            calls.len(),
            7,
            "unexpected pprof-tax profiler call sequence: {calls:?}"
        );
        assert_eq!(calls[0], "telemetry");
        assert!(calls[1].starts_with("start:"), "{calls:?}");
        assert_eq!(calls[2], "telemetry");
        assert_eq!(calls[3], "reset");
        assert_eq!(calls[4], "telemetry");
        assert_eq!(calls[5], "dump");
        assert_eq!(calls[6], "stop");

        hashes.push(sha256_file(&profile_path).expect("hash the dumped profile"));
    }
    assert_eq!(
        hashes[0], hashes[1],
        "identical planted telemetry must dump byte-identical profiles"
    );
}

#[test]
fn fork_pprof_off_request_never_starts_or_dumps() {
    let configuration = configuration_for("fork-pprof-off");
    assert!(
        !configuration.pprof_active,
        "fork-pprof-off must be an inactive configuration"
    );
    let profiler = FakeProfiler::new(
        configuration.compiled_configuration_id,
        configuration.pprof_compiled,
    );
    let adapter = MockAdapter::new("mock-allocator");
    let inner = inner_request(mock_identity(), 4);
    let request = pprof_tax_request(configuration, inner, None);
    let executable_sha256 = "d".repeat(64);

    let response =
        execute_pprof_tax_child_request(&adapter, &profiler, request, &executable_sha256)
            .expect("inactive pprof-tax request succeeds");

    assert!(!response.telemetry.enabled);
    assert!(!response.profile_written);
    assert!(response.interval_confirmed.is_none());
    let calls = profiler.calls();
    assert!(
        !calls
            .iter()
            .any(|call| call.starts_with("start") || call == "dump"),
        "an inactive request must never start or dump: {calls:?}"
    );
}

#[test]
fn requests_that_disagree_with_the_linked_profilers_compiled_state_are_rejected() {
    for id in ["fork-pprof-sparse", "fork-pprof-off"] {
        let configuration = configuration_for(id);
        // Deliberately invert the linked profiler's compiled flag so the
        // request's declared configuration and the profiler's compiled state
        // disagree, whichever way that configuration is actually defined.
        let profiler = FakeProfiler::new(
            configuration.compiled_configuration_id,
            !configuration.pprof_compiled,
        );
        let adapter = MockAdapter::new("mock-allocator");
        let inner = inner_request(mock_identity(), 4);
        let profile_path = configuration
            .pprof_active
            .then(|| "/nonexistent/pprof-tax-mismatch/profile.pb".to_string());
        let request = pprof_tax_request(configuration, inner, profile_path);
        let executable_sha256 = "d".repeat(64);

        let error =
            execute_pprof_tax_child_request(&adapter, &profiler, request, &executable_sha256)
                .expect_err(&format!(
                    "{id} against a profiler with an inverted compiled flag must be rejected"
                ));
        assert!(
            error.contains("disagree"),
            "unexpected error for {id}: {error}"
        );
    }
}

#[test]
fn active_request_without_a_profile_path_is_rejected() {
    let configuration = configuration_for("fork-pprof-sparse");
    let profiler = FakeProfiler::new(
        configuration.compiled_configuration_id,
        configuration.pprof_compiled,
    );
    let adapter = MockAdapter::new("mock-allocator");
    let inner = inner_request(mock_identity(), 4);
    let request = pprof_tax_request(configuration, inner, None);
    let executable_sha256 = "d".repeat(64);

    let error = execute_pprof_tax_child_request(&adapter, &profiler, request, &executable_sha256)
        .expect_err("an active request without a profile_path must be rejected");
    assert!(error.contains("profile_path"), "{error}");
}

#[test]
fn a_failing_dump_surfaces_an_error_and_never_reaches_stop() {
    let configuration = configuration_for("fork-pprof-sparse");
    let profiler = FakeProfiler::new(
        configuration.compiled_configuration_id,
        configuration.pprof_compiled,
    )
    .failing_dump();
    let adapter = MockAdapter::new("mock-allocator");
    let inner = inner_request(mock_identity(), 4);
    let guard = TempDirGuard::new("failing-dump");
    let profile_path = guard.profile_path();
    let request = pprof_tax_request(
        configuration,
        inner,
        Some(profile_path.to_string_lossy().into_owned()),
    );
    let executable_sha256 = "d".repeat(64);

    let error = execute_pprof_tax_child_request(&adapter, &profiler, request, &executable_sha256)
        .expect_err("a failing dump must surface an error");
    assert!(!error.is_empty());

    let calls = profiler.calls();
    assert!(
        calls.iter().any(|call| call == "dump"),
        "a failing dump must still be attempted: {calls:?}"
    );
    assert!(
        !calls.iter().any(|call| call == "stop"),
        "mi_prof_stop must never run after a failed dump: {calls:?}"
    );
}

#[test]
fn response_telemetry_json_reports_dropped_records_and_profiler_arena_bytes() {
    let configuration = configuration_for("fork-pprof-sparse");
    let profiler = FakeProfiler::new(
        configuration.compiled_configuration_id,
        configuration.pprof_compiled,
    )
    .with_planted(3, 1024, 2, 8192);
    let adapter = MockAdapter::new("mock-allocator");
    let inner = inner_request(mock_identity(), 4);
    let guard = TempDirGuard::new("telemetry-json");
    let profile_path = guard.profile_path();
    let request = pprof_tax_request(
        configuration,
        inner,
        Some(profile_path.to_string_lossy().into_owned()),
    );
    let executable_sha256 = "d".repeat(64);

    let response =
        execute_pprof_tax_child_request(&adapter, &profiler, request, &executable_sha256)
            .expect("planted active request succeeds");
    let encoded = serde_json::to_string(&response).expect("serialize pprof-tax child response");
    assert!(encoded.contains("\"dropped_records\""), "{encoded}");
    assert!(encoded.contains("\"profiler_arena_bytes\""), "{encoded}");
}

/// The `--pprof-tax-identity` mode has no isolated seam to exercise without a
/// native adapter; this mirrors how other contract tests skip when no
/// adapter is linked into the workspace test build, but still checks the
/// invariant the mode depends on whenever a build is linked.
#[test]
fn pprof_tax_identity_configuration_is_registered_when_linked() {
    use benchmark_suite::pprof_tax_adapter::BUILD_PPROF_TAX_CONFIGURATION;
    if BUILD_PPROF_TAX_CONFIGURATION == "none" {
        eprintln!("skipping: no linked pprof-tax build in this workspace build");
        return;
    }
    // The linked build names a *compiled* configuration id (e.g.
    // `fork-pprof-on`), not one of the seven runtime configuration ids.
    assert!(
        pprof_tax::PPROF_TAX_COMPILED_CONFIGURATION_IDS.contains(&BUILD_PPROF_TAX_CONFIGURATION),
        "a linked build's own compiled configuration id must be registered in the pprof-tax table"
    );
    assert!(
        pprof_tax::PPROF_TAX_CONFIGURATIONS
            .iter()
            .any(|entry| entry.compiled_configuration_id == BUILD_PPROF_TAX_CONFIGURATION),
        "at least one runtime configuration must run on the linked compiled configuration"
    );
}
