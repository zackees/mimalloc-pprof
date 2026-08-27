use mimalloc_pprof::{dhat, prof, MiMalloc};

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

#[test]
fn published_feature_contract_keeps_dhat_and_selects_pprof() {
    if dhat::is_enabled() {
        dhat::stop();
    }
    assert!(
        dhat::start(),
        "exact DHAT must remain available in every build"
    );

    let allocation = vec![0x5au8; 64 * 1024];
    std::hint::black_box(&allocation);
    let stats = dhat::stats();
    assert!(stats.enabled);
    assert!(stats.total_blocks > 0);
    assert!(stats.total_bytes >= allocation.len() as u64);

    dhat::stop();
    assert!(!dhat::is_enabled());

    #[cfg(feature = "pprof")]
    {
        assert!(prof::start(4096), "default features must compile pprof in");
        assert!(prof::is_enabled());
        prof::stop();
    }

    #[cfg(not(feature = "pprof"))]
    {
        assert!(
            !prof::start(4096),
            "no-default-features must use pprof stubs"
        );
        assert!(!prof::is_enabled());
    }
}
