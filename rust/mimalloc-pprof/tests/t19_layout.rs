//! ABI layout gate for every hand-written mirror in `src/sys.rs`.
//!
//! `src/sys.rs` mirrors C structs field-for-field and C enumerators value-for-value by
//! hand. Both drift silently: a mirrored struct whose fields moved writes the wrong
//! field, and a mirrored `mi_option_t` whose values shifted sets a *different* option
//! than the caller named. Neither produces a diagnostic — this fork inserts thirteen
//! enumerators mid-enum (indices 47..=59), which is exactly the shape of bug that
//! silently misconfigures an allocator.
//!
//! So the C compiler publishes what it actually laid out (`../layout_probe.c`) and this
//! test compares it, **by name**, against what Rust laid out. A field renamed or dropped
//! on either side fails as a missing key rather than as a coincidentally-equal number.
//!
//! Runs in both feature modes: the public *types* in `include/mimalloc/profile.h` are
//! declared unconditionally, so the layout the mirrors must match is the same whether or
//! not `MI_PPROF` is on.

use std::collections::BTreeMap;
use std::ffi::CStr;

use mimalloc_pprof::sys;

/// The C-side layout table, keyed by the names `layout_probe.c` publishes.
fn c_layout() -> BTreeMap<String, usize> {
    let mut count = 0_usize;
    let table = unsafe { sys::mi_rs_layout_table(&raw mut count) };
    assert!(!table.is_null(), "mi_rs_layout_table returned NULL");
    assert!(count > 0, "mi_rs_layout_table published an empty table");
    let mut map = BTreeMap::new();
    for i in 0..count {
        let entry = unsafe { &*table.add(i) };
        let name = unsafe { CStr::from_ptr(entry.name) }
            .to_str()
            .expect("layout keys are ASCII")
            .to_owned();
        assert!(
            map.insert(name.clone(), entry.value).is_none(),
            "duplicate layout key {name}"
        );
    }
    map
}

/// Collect `sizeof` plus one `offset_of` per named field, using the C spelling of the
/// type so the keys line up with `layout_probe.c`'s `MI_RS_SIZEOF`/`MI_RS_OFFSET`.
macro_rules! layout {
    ($c_name:literal, $rust_ty:ty $(, $field:ident)* $(,)?) => {{
        // `mut` is unused for a size-only invocation (a type with no named fields).
        #[allow(unused_mut)]
        let mut v: Vec<(String, usize)> =
            vec![(format!("sizeof:{}", $c_name), size_of::<$rust_ty>())];
        $(
            v.push((
                format!("offset:{}.{}", $c_name, stringify!($field)),
                core::mem::offset_of!($rust_ty, $field),
            ));
        )*
        v
    }};
}

fn check(c: &BTreeMap<String, usize>, expected: Vec<(String, usize)>) {
    for (key, rust_value) in expected {
        let c_value = c
            .get(&key)
            .copied()
            .unwrap_or_else(|| panic!("{key}: the C layout table has no such key"));
        assert_eq!(
            c_value, rust_value,
            "{key}: C says {c_value}, the Rust mirror in src/sys.rs says {rust_value}"
        );
    }
}

#[test]
fn profiler_structs_match_c() {
    let c = c_layout();
    check(
        &c,
        layout!(
            "mi_prof_stats_t",
            sys::mi_prof_stats_t,
            size,
            version,
            enabled,
            accum,
            sample_rate,
            live_samples,
            live_bytes,
            accum_samples,
            accum_bytes,
            unique_stacks,
            arena_committed,
            stack_table_overflows,
            dropped_samples,
            heap_committed,
            heap_reserved,
            heap_malloc_requested,
            heap_pages,
            heap_pages_abandoned,
            heap_count,
            theap_count,
            heap_purged,
            heap_stats_detailed,
        ),
    );
    check(
        &c,
        layout!(
            "mi_prof_config_t",
            sys::mi_prof_config_t,
            size,
            version,
            mode,
            sample_interval,
            max_profiler_bytes,
            seed,
            accum,
            max_stack_depth,
            dump_at_exit,
            dump_format,
        ),
    );
    check(
        &c,
        layout!(
            "mi_prof_sample_info_t",
            sys::mi_prof_sample_info_t,
            stack,
            depth,
            live_objects,
            live_bytes,
            accum_objects,
            accum_bytes,
        ),
    );
    check(
        &c,
        layout!(
            "mi_prof_module_info_t",
            sys::mi_prof_module_info_t,
            path,
            base,
            size,
        ),
    );
}

#[test]
fn dhat_struct_matches_c() {
    let c = c_layout();
    check(
        &c,
        layout!(
            "mi_dhat_stats_t",
            sys::mi_dhat_stats_t,
            size,
            version,
            enabled,
            incomplete,
            total_bytes,
            total_blocks,
            live_bytes,
            live_blocks,
            peak_bytes,
            peak_blocks,
            dropped,
            internal_bytes,
        ),
    );
}

#[test]
fn memory_events_structs_match_c() {
    let c = c_layout();
    check(
        &c,
        layout!(
            "mi_memory_change_t",
            sys::mi_memory_change_t,
            kind,
            total_bytes,
            delta_bytes,
            request_size,
        ),
    );
    check(
        &c,
        layout!(
            "mi_memory_callbacks_t",
            sys::mi_memory_callbacks_t,
            handlers,
            args,
        ),
    );
    check(
        &c,
        layout!(
            "mi_memory_snapshot_t",
            sys::mi_memory_snapshot_t,
            size,
            version,
            live_bytes,
            accum_bytes,
            live_count,
            accum_count,
        ),
    );
}

#[test]
fn purge_holes_struct_matches_c() {
    let c = c_layout();
    check(
        &c,
        layout!(
            "mi_purge_holes_stats_t",
            sys::MiPurgeHolesStats,
            purged_bytes,
            purged_blocks,
            purged_bytes_total,
            discard_calls,
            reuse_calls,
            pages_freed,
            ineligible_pages,
            ineligible_bytes,
            ineligible_free_bytes,
            unformed_bytes,
            unformed_bytes_total,
            unformed_discard_calls,
            unformed_reuse_calls,
            pages_skipped,
            blocks_visited,
            full_sweeps,
        ),
    );
}

#[test]
fn stats_struct_matches_c() {
    let c = c_layout();
    check(
        &c,
        layout!(
            "mi_stat_count_t",
            sys::mi_stat_count_t,
            total,
            peak,
            current,
        ),
    );
    check(
        &c,
        layout!("mi_stat_counter_t", sys::mi_stat_counter_t, total),
    );
    check(
        &c,
        layout!(
            "mi_stats_t",
            sys::mi_stats_t,
            size,
            version,
            pages,
            reserved,
            committed,
            reset,
            purged,
            page_committed,
            pages_abandoned,
            threads,
            malloc_normal,
            malloc_huge,
            malloc_requested,
            mmap_calls,
            commit_calls,
            reset_calls,
            purge_calls,
            arena_count,
            malloc_normal_count,
            malloc_huge_count,
            malloc_guarded_count,
            arena_rollback_count,
            arena_purges,
            pages_extended,
            pages_retire,
            page_searches,
            page_searches_count,
            segments,
            segments_abandoned,
            segments_cache,
            _segments_reserved,
            heaps,
            theaps,
            pages_reclaim_on_alloc,
            pages_reclaim_on_free,
            pages_reabandon_full,
            pages_unabandon_busy_wait,
            heaps_delete_wait,
            _stat_reserved,
            _stat_counter_reserved,
            malloc_bins,
            page_bins,
            chunk_bins,
        ),
    );
    check(&c, layout!("mi_subproc_id_t", sys::mi_subproc_id_t));
}

/// The C layout table lists every `mi_stats_t` field by walking the header's own
/// `MI_STAT_FIELDS()` macro. So if upstream adds a counter, the table grows a key this
/// test never asked about — and the Rust mirror is quietly one field short of the C
/// struct even though every offset it *does* check still matches. Catch that by
/// requiring the two field sets to be equal, not merely compatible.
#[test]
fn stats_struct_has_no_unmirrored_fields() {
    let c = c_layout();
    let mirrored: Vec<&str> = vec![
        "size",
        "version",
        "pages",
        "reserved",
        "committed",
        "reset",
        "purged",
        "page_committed",
        "pages_abandoned",
        "threads",
        "malloc_normal",
        "malloc_huge",
        "malloc_requested",
        "mmap_calls",
        "commit_calls",
        "reset_calls",
        "purge_calls",
        "arena_count",
        "malloc_normal_count",
        "malloc_huge_count",
        "malloc_guarded_count",
        "arena_rollback_count",
        "arena_purges",
        "pages_extended",
        "pages_retire",
        "page_searches",
        "page_searches_count",
        "segments",
        "segments_abandoned",
        "segments_cache",
        "_segments_reserved",
        "heaps",
        "theaps",
        "pages_reclaim_on_alloc",
        "pages_reclaim_on_free",
        "pages_reabandon_full",
        "pages_unabandon_busy_wait",
        "heaps_delete_wait",
        "_stat_reserved",
        "_stat_counter_reserved",
        "malloc_bins",
        "page_bins",
        "chunk_bins",
    ];
    let from_c: Vec<String> = c
        .keys()
        .filter_map(|k| k.strip_prefix("offset:mi_stats_t.").map(str::to_owned))
        .collect();
    for field in &from_c {
        assert!(
            mirrored.contains(&field.as_str()),
            "include/mimalloc-stats.h has a field `{field}` that src/sys.rs's \
             mi_stats_t does not mirror"
        );
    }
    assert_eq!(
        from_c.len(),
        mirrored.len(),
        "mi_stats_t field count drifted between C and src/sys.rs"
    );
}

#[test]
fn versions_and_constants_match_c() {
    let c = c_layout();
    let expected: Vec<(&str, usize)> = vec![
        (
            "const:MI_PROF_STAT_VERSION",
            sys::MI_PROF_STAT_VERSION as usize,
        ),
        (
            "const:MI_PROF_CONFIG_VERSION",
            sys::MI_PROF_CONFIG_VERSION as usize,
        ),
        (
            "const:MI_PROF_FORMAT_TEXT",
            sys::MI_PROF_FORMAT_TEXT as usize,
        ),
        (
            "const:MI_PROF_FORMAT_PROTO",
            sys::MI_PROF_FORMAT_PROTO as usize,
        ),
        (
            "const:MI_PROF_CONFIG_FALLBACK",
            sys::MI_PROF_CONFIG_FALLBACK as usize,
        ),
        (
            "const:MI_PROF_CONFIG_OVERRIDE",
            sys::MI_PROF_CONFIG_OVERRIDE as usize,
        ),
        (
            "const:MI_DHAT_STATS_VERSION",
            sys::MI_DHAT_STATS_VERSION as usize,
        ),
        (
            "const:MI_MEMORY_SNAPSHOT_VERSION",
            sys::MI_MEMORY_SNAPSHOT_VERSION as usize,
        ),
        ("const:MI_MEMORY_ALLOCATE", sys::MI_MEMORY_ALLOCATE as usize),
        ("const:MI_MEMORY_FREE", sys::MI_MEMORY_FREE as usize),
        ("const:MI_MEMORY_RESIZE", sys::MI_MEMORY_RESIZE as usize),
        ("const:MI_MEMORY_CHANGE_COUNT", sys::MI_MEMORY_CHANGE_COUNT),
        ("const:MI_STAT_VERSION", sys::MI_STAT_VERSION),
        ("const:MI_BIN_HUGE", sys::MI_BIN_HUGE),
        ("const:MI_CBIN_COUNT", sys::MI_CBIN_COUNT),
    ];
    check(
        &c,
        expected
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    );
}

/// The hazard this whole file exists for: `mi_option_t` is positional, and this fork
/// inserted thirteen enumerators before `_mi_option_last`. A mirror copied from upstream
/// would compile, run, and set the wrong option forever.
#[test]
fn option_values_match_c() {
    let c = c_layout();
    let expected: Vec<(String, usize)> = sys::MI_OPTIONS_IN_ORDER
        .iter()
        .map(|(name, value)| {
            assert!(*value >= 0, "{name}: negative option value {value}");
            (format!("option:{name}"), *value as usize)
        })
        .collect();
    check(&c, expected);

    // Sequential from zero, with no gaps: that is what makes an inserted enumerator
    // shift every later one, and what an upstream merge can quietly break.
    for (index, (name, value)) in sys::MI_OPTIONS_IN_ORDER.iter().enumerate() {
        assert_eq!(
            *value as usize, index,
            "{name} is at index {index} of MI_OPTIONS_IN_ORDER but has value {value}"
        );
    }
    let (last_name, last_value) = sys::MI_OPTIONS_IN_ORDER
        .last()
        .copied()
        .expect("MI_OPTIONS_IN_ORDER is non-empty");
    assert_eq!(last_name, "_mi_option_last");
    assert_eq!(last_value, sys::_mi_option_last);
}

/// The deprecated upstream aliases resolve to the option they alias, not to a slot of
/// their own.
#[test]
fn deprecated_option_aliases_match_c() {
    let c = c_layout();
    check(
        &c,
        vec![
            (
                "option:mi_option_large_os_pages".to_owned(),
                sys::mi_option_large_os_pages as usize,
            ),
            (
                "option:mi_option_eager_region_commit".to_owned(),
                sys::mi_option_eager_region_commit as usize,
            ),
            (
                "option:mi_option_reset_decommits".to_owned(),
                sys::mi_option_reset_decommits as usize,
            ),
            (
                "option:mi_option_reset_delay".to_owned(),
                sys::mi_option_reset_delay as usize,
            ),
            (
                "option:mi_option_limit_os_alloc".to_owned(),
                sys::mi_option_limit_os_alloc as usize,
            ),
        ],
    );
}
