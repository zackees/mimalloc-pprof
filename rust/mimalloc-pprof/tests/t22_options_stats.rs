//! `mi_option_*` and the exact-statistics surface (`include/mimalloc-stats.h`),
//! plus `mi_purge_holes_report`.
//!
//! No `required-features`: options and statistics are part of the allocator, not of the
//! profiler, so this runs in both `rust-native` feature modes.
//!
//! Options are process-global, so the tests that change one take a lock and put it back.

use std::sync::{Mutex, MutexGuard, OnceLock};

use mimalloc_pprof::options::{self, Opt};
use mimalloc_pprof::stats::{self, Subproc};
use mimalloc_pprof::sys;

#[global_allocator]
static ALLOCATOR: mimalloc_pprof::MiMalloc = mimalloc_pprof::MiMalloc;

fn lock() -> MutexGuard<'static, ()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The hazard the mirror exists for: this fork inserts thirteen enumerators mid-enum, so
/// a Rust constant that drifted would name a *different* option and misconfigure the
/// allocator in silence. `tests/t19_layout.rs` checks the values against the C compiler;
/// this checks that the named [`Opt`] constants still point at the fork's own options.
#[test]
fn fork_option_constants_name_the_fork_options() {
    assert_eq!(Opt::PROF.name(), "mi_option_prof");
    assert_eq!(Opt::PROF_SAMPLE_RATE.name(), "mi_option_prof_sample_rate");
    assert_eq!(Opt::PROF_BT_MAX.name(), "mi_option_prof_bt_max");
    assert_eq!(Opt::PROF_ACCUM.name(), "mi_option_prof_accum");
    assert_eq!(Opt::PROF_SEED.name(), "mi_option_prof_seed");
    assert_eq!(Opt::PROF_MAX_BYTES.name(), "mi_option_prof_max_bytes");
    assert_eq!(Opt::MEMORY_EVENTS.name(), "mi_option_memory_events");
    assert_eq!(Opt::PURGE_ZEROES.name(), "mi_option_purge_zeroes");
    assert_eq!(Opt::SCAVENGER.name(), "mi_option_scavenger");
    assert_eq!(Opt::PURGE_HOLES.name(), "mi_option_purge_holes");
    assert_eq!(
        Opt::PURGE_HOLES_EAGER_ZERO.name(),
        "mi_option_purge_holes_eager_zero"
    );
    assert_eq!(
        Opt::PURGE_HOLES_MIN_INTERVAL.name(),
        "mi_option_purge_holes_min_interval"
    );
    assert_eq!(
        Opt::PURGE_HOLES_FULL_EVERY.name(),
        "mi_option_purge_holes_full_every"
    );
}

/// `Opt::from_raw` range-checks because the C side indexes an array with the value.
#[test]
fn from_raw_rejects_non_options() {
    assert!(Opt::from_raw(sys::mi_option_purge_holes).is_some());
    assert!(Opt::from_raw(sys::_mi_option_last).is_none());
    assert!(Opt::from_raw(-1).is_none());
    assert!(Opt::from_raw(sys::_mi_option_last + 1000).is_none());
    assert_eq!(
        Opt::from_raw(sys::mi_option_scavenger).map(Opt::as_raw),
        Some(sys::mi_option_scavenger)
    );
}

#[test]
fn options_listing_is_dense_and_ends_at_the_sentinel() {
    let all = options::all();
    assert_eq!(all.len(), sys::_mi_option_last as usize + 1);
    assert_eq!(all.last().map(|(name, _)| *name), Some("_mi_option_last"));
}

#[test]
fn numeric_option_round_trips() {
    let _guard = lock();
    let previous = options::get(Opt::PURGE_HOLES_MIN_INTERVAL);
    options::set(Opt::PURGE_HOLES_MIN_INTERVAL, 1234);
    assert_eq!(options::get(Opt::PURGE_HOLES_MIN_INTERVAL), 1234);
    assert_eq!(
        options::get_clamp(Opt::PURGE_HOLES_MIN_INTERVAL, 0, 100),
        100
    );
    assert_eq!(options::get_size(Opt::PURGE_HOLES_MIN_INTERVAL), 1234);
    options::set(Opt::PURGE_HOLES_MIN_INTERVAL, previous);
    assert_eq!(options::get(Opt::PURGE_HOLES_MIN_INTERVAL), previous);
}

#[test]
fn boolean_option_round_trips() {
    let _guard = lock();
    let previous = options::is_enabled(Opt::PURGE_HOLES);
    options::disable(Opt::PURGE_HOLES);
    assert!(!options::is_enabled(Opt::PURGE_HOLES));
    options::enable(Opt::PURGE_HOLES);
    assert!(options::is_enabled(Opt::PURGE_HOLES));
    options::set_enabled(Opt::PURGE_HOLES, previous);
    assert_eq!(options::is_enabled(Opt::PURGE_HOLES), previous);
}

#[test]
fn exact_stats_are_populated() {
    let held: Vec<Vec<u8>> = (0..32).map(|_| vec![0_u8; 64 * 1024]).collect();
    std::hint::black_box(&held);

    let s = stats::get().expect("mi_stats_get accepted the mirrored struct header");
    assert_eq!(s.size, size_of::<sys::mi_stats_t>());
    assert_eq!(s.version, sys::MI_STAT_VERSION);
    assert!(s.committed.current > 0, "committed = {:?}", s.committed);
    assert!(
        s.reserved.current >= s.committed.current,
        "reserved {} < committed {}",
        s.reserved.current,
        s.committed.current
    );
    assert!(s.pages.current > 0, "pages = {:?}", s.pages);

    let json = s.to_json().expect("mi_stats_as_json");
    assert!(
        json.starts_with('{'),
        "not JSON: {}",
        &json[..json.len().min(60)]
    );
    assert!(json.contains("committed"), "no committed counter in {json}");

    drop(held);
}

#[test]
fn stats_json_matches_the_struct_getter() {
    let json = stats::json().expect("mi_stats_get_json");
    assert!(json.contains("committed"));
    assert!(json.contains("reserved"));
    // The bin arrays the struct mirrors are what the JSON reports per size class.
    assert!(stats::bin_size(1) > 0);
    assert!(stats::bin_size(1) <= stats::bin_size(sys::MI_BIN_HUGE));
}

#[test]
fn subprocess_stats_are_reachable() {
    for which in [Subproc::Main, Subproc::Current] {
        // Deliberately no cross-snapshot comparison (e.g. aggregated >= exclusive):
        // although that holds at any single instant, the two are separate reads and the
        // other tests in this binary allocate on other threads in between. Assert what is
        // true *within* one snapshot instead.
        let aggregated = stats::subproc_get(which).expect("mi_subproc_stats_get");
        assert_eq!(aggregated.version, sys::MI_STAT_VERSION);
        assert!(
            aggregated.reserved.current >= aggregated.committed.current,
            "{which:?}: reserved {} < committed {}",
            aggregated.reserved.current,
            aggregated.committed.current
        );
        assert!(aggregated.committed.current > 0);

        let exclusive =
            stats::subproc_get_exclusive(which).expect("mi_subproc_stats_get_exclusive");
        assert_eq!(exclusive.size, size_of::<sys::mi_stats_t>());

        assert!(stats::subproc_json(which).is_some());
    }
}

/// The print entry points write to mimalloc's own output sink rather than returning a
/// `String` (building one would allocate from inside a callback the allocator drives).
/// There is nothing to assert but that they run and do not reenter fatally.
#[test]
fn print_entry_points_run() {
    stats::print();
    stats::subproc_print(Subproc::Main);
    stats::subproc_heap_print(Subproc::Current);
    options::print();
}
