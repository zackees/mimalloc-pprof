//! Child-side profiler-state protocol for the pprof compilation/runtime tax
//! matrix (issue #187).
//!
//! One [`PprofTaxChildRequest`] drives exactly one freshly spawned child: it
//! wraps the ordinary `core-throughput-v1` [`BenchmarkChildRequest`] with the
//! profiler-state transitions the measurement needs around it. The protocol
//! is intentionally linear so every state transition is auditable from the
//! response alone:
//!
//! 1. Read telemetry; it must show the profiler disabled.
//! 2. If the configuration is active, start it and confirm the sampling
//!    interval before any workload runs.
//! 3. Run the workload. Warmup samples are cleared with a public
//!    `mi_prof_reset` immediately before the measured region begins, so reset
//!    never touches allocator state, only profiler state.
//! 4. If active, read telemetry once more, dump the proto file, then stop —
//!    in that order, because `mi_prof_stop` frees records and zeroes
//!    counters.

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::execution::{
    execute_child_request_with_observer, AllocatorAdapter, ExecutionResult, MeasurementObserver,
};
use crate::memory::signed_delta;
use crate::model::{BenchmarkChildRequest, BenchmarkChildResponse};
use crate::pprof_tax;
use crate::pprof_tax_adapter::{ProfilerControl, ProfilerTelemetry};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxChildRequest {
    pub protocol_version: String,
    pub configuration_id: String,
    pub compiled_configuration_id: String,
    pub pprof_active: bool,
    pub sampling_interval_bytes: Option<u64>,
    pub profiler_seed: u64,
    pub profile_path: Option<String>,
    pub inner: BenchmarkChildRequest,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxChildResponse {
    pub protocol_version: String,
    pub configuration_id: String,
    pub compiled_configuration_id: String,
    pub executable_sha256: String,
    pub pprof_compiled: bool,
    pub pprof_enabled_before_start: bool,
    pub interval_confirmed: Option<u64>,
    pub telemetry: ProfilerTelemetry,
    pub end_rss_delta_bytes: Option<i64>,
    pub profile_written: bool,
    pub inner: BenchmarkChildResponse,
}

/// Read the resident set size of this process from `/proc/self/smaps_rollup`.
/// Linux-only: every other target reports `None` so the crate keeps
/// compiling, but production pprof-tax collection is Linux-only anyway.
#[cfg(target_os = "linux")]
fn current_process_rss_bytes() -> Option<u64> {
    std::fs::read_to_string("/proc/self/smaps_rollup")
        .ok()
        .and_then(|text| crate::memory::parse_smaps_rollup(&text).ok())
}

#[cfg(not(target_os = "linux"))]
fn current_process_rss_bytes() -> Option<u64> {
    None
}

/// Clears warmup samples via the public `mi_prof_reset` (profiler-state only)
/// immediately before the measured region, and records baseline/end RSS
/// around the exact same window the workload's timing uses.
struct PprofTaxObserver<'a, P: ProfilerControl> {
    profiler: &'a P,
    active: bool,
    baseline_rss_bytes: Option<u64>,
    end_rss_bytes: Option<u64>,
}

impl<P: ProfilerControl> MeasurementObserver for PprofTaxObserver<'_, P> {
    fn baseline_ready_and_wait_for_begin(&mut self) -> Result<(), String> {
        if self.active {
            self.profiler.reset();
        }
        self.baseline_rss_bytes = current_process_rss_bytes();
        Ok(())
    }

    fn workload_active(&mut self) -> Result<(), String> {
        Ok(())
    }

    fn workload_drained(&mut self, _outcome: &ExecutionResult) -> Result<(), String> {
        self.end_rss_bytes = current_process_rss_bytes();
        Ok(())
    }
}

/// Execute one pprof-tax child request: validate its declared configuration
/// against both the static table and the linked profiler's compiled state,
/// drive the profiler-state transitions around the ordinary workload, and
/// return the combined response.
pub fn execute_pprof_tax_child_request<A: AllocatorAdapter, P: ProfilerControl>(
    adapter: &A,
    profiler: &P,
    request: PprofTaxChildRequest,
    executable_sha256: &str,
) -> Result<PprofTaxChildResponse, String> {
    if request.protocol_version != pprof_tax::PPROF_TAX_CHILD_PROTOCOL_VERSION {
        return Err("unsupported pprof-tax child protocol version".into());
    }
    let configuration = pprof_tax::configuration(&request.configuration_id).ok_or_else(|| {
        format!(
            "unknown pprof-tax configuration {}",
            request.configuration_id
        )
    })?;
    if configuration.compiled_configuration_id != request.compiled_configuration_id
        || configuration.pprof_active != request.pprof_active
        || configuration.sampling_interval_bytes != request.sampling_interval_bytes
    {
        return Err("pprof-tax child request does not match its declared configuration".into());
    }
    if profiler.configuration_id() != request.compiled_configuration_id
        || profiler.pprof_compiled() != configuration.pprof_compiled
    {
        return Err(
            "pprof-tax child request configuration labels disagree with the linked profiler".into(),
        );
    }
    // The parent-verified executable digest must be the one the inner request
    // echoes into its raw sample, so a sample can never name a binary other
    // than the one that produced it.
    if request.inner.allocator.child_binary_sha256 != executable_sha256 {
        return Err(
            "pprof-tax child request child_binary_sha256 does not match the verified executable"
                .into(),
        );
    }
    // Active requests must carry a place to dump the profile; inactive
    // requests must not, because they never call `dump_proto`.
    if request.pprof_active == request.profile_path.is_none() {
        return Err("pprof-tax child request profile_path presence must match pprof_active".into());
    }

    let initial_telemetry = profiler.telemetry()?;
    if initial_telemetry.enabled {
        return Err("pprof-tax child observed an already-enabled profiler before start".into());
    }
    let pprof_enabled_before_start = initial_telemetry.enabled;

    let interval_confirmed = if request.pprof_active {
        let interval = request
            .sampling_interval_bytes
            .ok_or("active pprof-tax request requires sampling_interval_bytes")?;
        profiler.start(interval, request.profiler_seed)?;
        let telemetry = profiler.telemetry()?;
        if !telemetry.enabled || telemetry.sample_interval_bytes != interval {
            return Err(
                "pprof-tax profiler did not confirm the requested sampling interval".into(),
            );
        }
        Some(telemetry.sample_interval_bytes)
    } else {
        None
    };

    let mut observer = PprofTaxObserver {
        profiler,
        active: request.pprof_active,
        baseline_rss_bytes: None,
        end_rss_bytes: None,
    };
    let inner = execute_child_request_with_observer(adapter, request.inner, &mut observer)?;
    let end_rss_delta_bytes = match (observer.baseline_rss_bytes, observer.end_rss_bytes) {
        (Some(baseline), Some(end)) => Some(signed_delta(end, baseline)?),
        _ => None,
    };

    let (telemetry, profile_written) = if request.pprof_active {
        let telemetry = profiler.telemetry()?;
        let profile_path = request
            .profile_path
            .as_deref()
            .ok_or_else(|| "active pprof-tax request lost its profile_path".to_string())?;
        profiler.dump_proto(Path::new(profile_path))?;
        profiler.stop();
        (telemetry, true)
    } else {
        let telemetry = profiler.telemetry()?;
        if telemetry.enabled {
            return Err("pprof-tax profiler unexpectedly reports enabled while inactive".into());
        }
        (telemetry, false)
    };

    Ok(PprofTaxChildResponse {
        protocol_version: pprof_tax::PPROF_TAX_CHILD_PROTOCOL_VERSION.into(),
        configuration_id: request.configuration_id,
        compiled_configuration_id: request.compiled_configuration_id,
        executable_sha256: executable_sha256.to_string(),
        pprof_compiled: configuration.pprof_compiled,
        pprof_enabled_before_start,
        interval_confirmed,
        telemetry,
        end_rss_delta_bytes,
        profile_written,
        inner,
    })
}
