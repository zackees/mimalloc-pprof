//! Native profiler-control seam for the pprof compilation/runtime tax panel
//! (issue #187, Phase 6D).
//!
//! One benchmark child, opt-in via `BENCH_PPROF_TAX_CONFIGURATION`, reports
//! its compiled configuration identity and drives the fork's public sampling
//! profiler (`include/mimalloc/profile.h`) exclusively through the public
//! API surfaced by `native/pprof_tax_adapter.h`. `LinkedProfiler` is the real
//! native adapter; `FakeProfiler` is an in-process double for exercising
//! callers without a native build.

#[cfg(benchmark_pprof_tax_adapter)]
use std::ffi::CStr;
use std::path::Path;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

pub const BUILD_PPROF_TAX_CONFIGURATION: &str = env!("BENCH_PPROF_TAX_CONFIGURATION");

const UNCONFIGURED_ERROR: &str = "benchmark child was not built with a pprof-tax configuration";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfilerTelemetry {
    pub compiled: bool,
    pub enabled: bool,
    pub accum: bool,
    pub sample_interval_bytes: u64,
    /// mi_prof_stats_t::accum_samples.
    pub sample_count: u64,
    /// mi_prof_stats_t::accum_bytes.
    pub sampled_bytes: u64,
    pub live_samples: u64,
    pub live_bytes: u64,
    /// mi_prof_stats_t::dropped_samples.
    pub dropped_records: u64,
    /// mi_prof_stats_t::arena_committed.
    pub profiler_arena_bytes: u64,
}

pub trait ProfilerControl {
    fn configuration_id(&self) -> &str;
    /// True iff this configuration was built with the fork's profiler API
    /// (MI_PPROF=ON); false for upstream-baseline and any fork build with
    /// MI_PPROF=OFF.
    fn pprof_compiled(&self) -> bool;
    fn start(&self, interval_bytes: u64, seed: u64) -> Result<(), String>;
    fn stop(&self);
    fn reset(&self);
    fn telemetry(&self) -> Result<ProfilerTelemetry, String>;
    fn dump_proto(&self, path: &Path) -> Result<(), String>;
}

/// The real native adapter: drives `native/adapter_pprof_tax.c`, which in
/// turn drives `mi_prof_*` (fork configurations) or is a compiled-out stub
/// (upstream-baseline).
pub struct LinkedProfiler {
    compiled: bool,
}

impl LinkedProfiler {
    /// Validates that the linked native adapter's reported identity and
    /// compiled-in profiler state agree with this build's compiled identity
    /// -- a wrong or stale native library fails here instead of silently
    /// mislabeling a measurement.
    #[cfg(benchmark_pprof_tax_adapter)]
    pub fn load() -> Result<Self, String> {
        if BUILD_PPROF_TAX_CONFIGURATION == "none" {
            return Err(UNCONFIGURED_ERROR.to_owned());
        }
        let native_id = unsafe { CStr::from_ptr(bench_pprof_configuration_id()) }
            .to_str()
            .map_err(|_| "pprof-tax adapter returned a non-UTF-8 configuration id".to_owned())?;
        if native_id != BUILD_PPROF_TAX_CONFIGURATION {
            return Err(format!(
                "linked pprof-tax configuration mismatch: expected {BUILD_PPROF_TAX_CONFIGURATION:?}, got {native_id:?}"
            ));
        }
        let compiled = unsafe { bench_pprof_compiled() } != 0;
        // The only configuration expected to link a MI_PPROF=ON library is
        // fork-pprof-on; every other configuration (including the two
        // fork-pprof-off variants) must link a MI_PPROF=OFF library. This
        // catches a build that labels its linked library incorrectly.
        let expected_compiled = BUILD_PPROF_TAX_CONFIGURATION == "fork-pprof-on";
        if compiled != expected_compiled {
            return Err(format!(
                "pprof-tax configuration {BUILD_PPROF_TAX_CONFIGURATION:?} expects compiled={expected_compiled}, but the linked profiler reported compiled={compiled}"
            ));
        }
        Ok(Self { compiled })
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    pub fn load() -> Result<Self, String> {
        Err(UNCONFIGURED_ERROR.to_owned())
    }
}

impl ProfilerControl for LinkedProfiler {
    fn configuration_id(&self) -> &str {
        BUILD_PPROF_TAX_CONFIGURATION
    }

    fn pprof_compiled(&self) -> bool {
        self.compiled
    }

    #[cfg(benchmark_pprof_tax_adapter)]
    fn start(&self, interval_bytes: u64, seed: u64) -> Result<(), String> {
        if !self.compiled {
            return Err("pprof-tax profiler is not compiled into this configuration".to_owned());
        }
        if interval_bytes == 0 {
            return Err("pprof-tax start requires a nonzero sample interval".to_owned());
        }
        if unsafe { bench_pprof_start(interval_bytes, seed) } == 0 {
            return Err("native pprof-tax profiler refused to start".to_owned());
        }
        Ok(())
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    fn start(&self, _interval_bytes: u64, _seed: u64) -> Result<(), String> {
        Err(UNCONFIGURED_ERROR.to_owned())
    }

    #[cfg(benchmark_pprof_tax_adapter)]
    fn stop(&self) {
        unsafe { bench_pprof_stop() }
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    fn stop(&self) {}

    #[cfg(benchmark_pprof_tax_adapter)]
    fn reset(&self) {
        unsafe { bench_pprof_reset() }
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    fn reset(&self) {}

    // mi_prof_stop frees every record and zeroes every counter, so a caller
    // must read telemetry (and dump, if needed) before calling stop -- this
    // adapter does not reorder or cache around that requirement.
    #[cfg(benchmark_pprof_tax_adapter)]
    fn telemetry(&self) -> Result<ProfilerTelemetry, String> {
        if !self.compiled {
            return Ok(ProfilerTelemetry::default());
        }
        let mut raw = RawPprofStats::default();
        if unsafe { bench_pprof_stats(&mut raw) } == 0 {
            return Ok(ProfilerTelemetry::default());
        }
        Ok(ProfilerTelemetry {
            compiled: raw.compiled != 0,
            enabled: raw.enabled != 0,
            accum: raw.accum != 0,
            sample_interval_bytes: raw.sample_interval_bytes,
            sample_count: raw.accum_samples,
            sampled_bytes: raw.accum_bytes,
            live_samples: raw.live_samples,
            live_bytes: raw.live_bytes,
            dropped_records: raw.dropped_samples,
            profiler_arena_bytes: raw.arena_committed,
        })
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    fn telemetry(&self) -> Result<ProfilerTelemetry, String> {
        Ok(ProfilerTelemetry::default())
    }

    #[cfg(benchmark_pprof_tax_adapter)]
    fn dump_proto(&self, path: &Path) -> Result<(), String> {
        if !self.compiled {
            return Err("pprof-tax profiler is not compiled into this configuration".to_owned());
        }
        let c_path = path_to_cstring(path)?;
        if unsafe { bench_pprof_dump_proto(c_path.as_ptr()) } == 0 {
            return Err(format!(
                "native pprof-tax dump to {} failed",
                path.display()
            ));
        }
        Ok(())
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    fn dump_proto(&self, _path: &Path) -> Result<(), String> {
        Err(UNCONFIGURED_ERROR.to_owned())
    }
}

#[cfg(benchmark_pprof_tax_adapter)]
fn path_to_cstring(path: &Path) -> Result<std::ffi::CString, String> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        std::ffi::CString::new(path.as_os_str().as_bytes())
            .map_err(|_| "pprof-tax dump path contains an embedded NUL byte".to_owned())
    }
    #[cfg(not(unix))]
    {
        let text = path
            .to_str()
            .ok_or_else(|| "pprof-tax dump path is not valid UTF-8".to_owned())?;
        std::ffi::CString::new(text)
            .map_err(|_| "pprof-tax dump path contains an embedded NUL byte".to_owned())
    }
}

#[cfg(benchmark_pprof_tax_adapter)]
#[repr(C)]
#[derive(Default)]
struct RawPprofStats {
    compiled: i32,
    enabled: i32,
    accum: i32,
    sample_interval_bytes: u64,
    accum_samples: u64,
    accum_bytes: u64,
    live_samples: u64,
    live_bytes: u64,
    dropped_samples: u64,
    arena_committed: u64,
}

#[cfg(benchmark_pprof_tax_adapter)]
unsafe extern "C" {
    fn bench_pprof_configuration_id() -> *const std::os::raw::c_char;
    fn bench_pprof_compiled() -> i32;
    fn bench_pprof_start(interval_bytes: u64, seed: u64) -> i32;
    fn bench_pprof_stop();
    fn bench_pprof_reset();
    fn bench_pprof_stats(out: *mut RawPprofStats) -> i32;
    fn bench_pprof_dump_proto(path: *const std::os::raw::c_char) -> i32;
}

#[derive(Clone, Copy, Default)]
struct PlantedCounters {
    sample_count: u64,
    sampled_bytes: u64,
    dropped_records: u64,
    profiler_arena_bytes: u64,
}

struct FakeProfilerState {
    enabled: bool,
    interval_bytes: u64,
    calls: Vec<String>,
}

/// An in-process double for [`ProfilerControl`], for exercising callers
/// (T3's panel logic) without a native pprof-tax build. Interior mutability
/// is a `Mutex` so the trait's `&self` methods can still log and mutate.
pub struct FakeProfiler {
    configuration_id: String,
    compiled: bool,
    planted: PlantedCounters,
    failing_dump: bool,
    state: Mutex<FakeProfilerState>,
}

impl FakeProfiler {
    pub fn new(configuration_id: &str, compiled: bool) -> Self {
        Self {
            configuration_id: configuration_id.to_owned(),
            compiled,
            planted: PlantedCounters::default(),
            failing_dump: false,
            state: Mutex::new(FakeProfilerState {
                enabled: false,
                interval_bytes: 0,
                calls: Vec::new(),
            }),
        }
    }

    /// Plants the counters `telemetry` reports while the fake is enabled.
    /// `reset` intentionally leaves these untouched -- they model
    /// warmup-independent, persistent profiler state.
    pub fn with_planted(
        mut self,
        sample_count: u64,
        sampled_bytes: u64,
        dropped_records: u64,
        profiler_arena_bytes: u64,
    ) -> Self {
        self.planted = PlantedCounters {
            sample_count,
            sampled_bytes,
            dropped_records,
            profiler_arena_bytes,
        };
        self
    }

    /// Makes every subsequent `dump_proto` call fail, regardless of enabled
    /// state.
    pub fn failing_dump(mut self) -> Self {
        self.failing_dump = true;
        self
    }

    /// The ordered log of calls made through [`ProfilerControl`].
    pub fn calls(&self) -> Vec<String> {
        self.lock_state().calls.clone()
    }

    fn lock_state(&self) -> std::sync::MutexGuard<'_, FakeProfilerState> {
        self.state.lock().expect("fake profiler mutex poisoned")
    }

    fn log(&self, entry: String) {
        self.lock_state().calls.push(entry);
    }
}

impl ProfilerControl for FakeProfiler {
    fn configuration_id(&self) -> &str {
        &self.configuration_id
    }

    fn pprof_compiled(&self) -> bool {
        self.compiled
    }

    fn start(&self, interval_bytes: u64, seed: u64) -> Result<(), String> {
        self.log(format!("start:{interval_bytes}:{seed}"));
        if !self.compiled {
            return Err("fake pprof-tax profiler is not compiled".to_owned());
        }
        if interval_bytes == 0 {
            return Err("fake pprof-tax start requires a nonzero sample interval".to_owned());
        }
        let mut state = self.lock_state();
        state.enabled = true;
        state.interval_bytes = interval_bytes;
        Ok(())
    }

    fn stop(&self) {
        self.log("stop".to_owned());
        self.lock_state().enabled = false;
    }

    fn reset(&self) {
        self.log("reset".to_owned());
    }

    fn telemetry(&self) -> Result<ProfilerTelemetry, String> {
        self.log("telemetry".to_owned());
        let state = self.lock_state();
        if !state.enabled {
            return Ok(ProfilerTelemetry {
                compiled: self.compiled,
                ..ProfilerTelemetry::default()
            });
        }
        Ok(ProfilerTelemetry {
            compiled: self.compiled,
            enabled: true,
            accum: true,
            sample_interval_bytes: state.interval_bytes,
            sample_count: self.planted.sample_count,
            sampled_bytes: self.planted.sampled_bytes,
            live_samples: 0,
            live_bytes: 0,
            dropped_records: self.planted.dropped_records,
            profiler_arena_bytes: self.planted.profiler_arena_bytes,
        })
    }

    fn dump_proto(&self, path: &Path) -> Result<(), String> {
        self.log("dump".to_owned());
        let enabled = self.lock_state().enabled;
        if self.failing_dump || !enabled {
            return Err(format!(
                "fake pprof-tax dump to {} failed (enabled={enabled}, failing_dump={})",
                path.display(),
                self.failing_dump
            ));
        }
        let contents = format!(
            "fake-pprof-profile:{}:{}",
            self.configuration_id, self.planted.sample_count
        );
        std::fs::write(path, contents.as_bytes())
            .map_err(|error| format!("write fake pprof-tax dump to {}: {error}", path.display()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fake_profiler_start_requires_compiled_and_nonzero_interval() {
        let uncompiled = FakeProfiler::new("fork-pprof-off", false);
        assert!(uncompiled.start(4096, 1).is_err());

        let compiled = FakeProfiler::new("fork-pprof-on", true);
        assert!(compiled.start(0, 1).is_err());
    }

    #[test]
    fn fake_profiler_reports_planted_counters_only_while_enabled() {
        let fake = FakeProfiler::new("fork-pprof-on", true).with_planted(10, 2048, 1, 4096);

        let idle = fake.telemetry().unwrap();
        assert!(!idle.enabled);
        assert!(!idle.accum);
        assert_eq!(idle.sample_count, 0);
        assert_eq!(idle.sampled_bytes, 0);
        assert_eq!(idle.dropped_records, 0);
        assert_eq!(idle.profiler_arena_bytes, 0);

        fake.start(512, 7).unwrap();
        let running = fake.telemetry().unwrap();
        assert!(running.compiled);
        assert!(running.enabled);
        assert!(running.accum);
        assert_eq!(running.sample_interval_bytes, 512);
        assert_eq!(running.sample_count, 10);
        assert_eq!(running.sampled_bytes, 2048);
        assert_eq!(running.dropped_records, 1);
        assert_eq!(running.profiler_arena_bytes, 4096);

        fake.reset();
        let after_reset = fake.telemetry().unwrap();
        assert_eq!(after_reset.sample_count, 10);

        fake.stop();
        let stopped = fake.telemetry().unwrap();
        assert!(!stopped.enabled);
        assert_eq!(stopped.sample_count, 0);
    }

    #[test]
    fn fake_profiler_dump_requires_enabled_and_can_be_forced_to_fail() {
        let path = std::env::temp_dir().join(format!(
            "pprof-tax-adapter-test-{}-{}.pb",
            std::process::id(),
            line!()
        ));

        let fake = FakeProfiler::new("fork-pprof-on", true);
        assert!(fake.dump_proto(&path).is_err());

        fake.start(1024, 3).unwrap();
        fake.dump_proto(&path).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(bytes, b"fake-pprof-profile:fork-pprof-on:0");
        let _ = std::fs::remove_file(&path);

        let failing = FakeProfiler::new("fork-pprof-on", true).failing_dump();
        failing.start(1024, 3).unwrap();
        assert!(failing.dump_proto(&path).is_err());
    }

    #[test]
    fn fake_profiler_call_log_is_ordered() {
        let path = std::env::temp_dir().join(format!(
            "pprof-tax-adapter-test-{}-{}.pb",
            std::process::id(),
            line!()
        ));
        let fake = FakeProfiler::new("fork-pprof-on", true);

        fake.start(1024, 5).unwrap();
        fake.reset();
        let _ = fake.telemetry();
        let _ = fake.dump_proto(&path);
        let _ = std::fs::remove_file(&path);
        fake.stop();

        assert_eq!(
            fake.calls(),
            vec![
                "start:1024:5".to_owned(),
                "reset".to_owned(),
                "telemetry".to_owned(),
                "dump".to_owned(),
                "stop".to_owned(),
            ]
        );
    }

    #[cfg(not(benchmark_pprof_tax_adapter))]
    #[test]
    fn linked_profiler_load_errors_in_an_unlinked_build() {
        if BUILD_PPROF_TAX_CONFIGURATION == "none" {
            match LinkedProfiler::load() {
                Ok(_) => panic!("unlinked build must not report a pprof-tax configuration"),
                Err(error) => assert_eq!(error, UNCONFIGURED_ERROR),
            }
        }
    }
}
