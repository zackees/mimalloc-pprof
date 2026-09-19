use mimalloc_pprof::{dhat, prof, MiMalloc};

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

#[test]
fn published_feature_contract_selects_dhat_and_pprof() {
    if dhat::is_enabled() {
        dhat::stop();
    }

    #[cfg(feature = "dhat")]
    {
        assert!(
            dhat::start(),
            "the dhat feature must compile the observer in"
        );

        let allocation = vec![0x5au8; 64 * 1024];
        std::hint::black_box(&allocation);
        let stats = dhat::stats();
        assert!(stats.enabled);
        assert!(stats.total_blocks > 0);
        assert!(stats.total_bytes >= allocation.len() as u64);

        dhat::stop();
        assert!(!dhat::is_enabled());
    }

    #[cfg(not(feature = "dhat"))]
    {
        assert!(
            !dhat::start(),
            "without the dhat feature DHAT must use the C stubs"
        );
        assert!(!dhat::is_enabled());
        assert!(!dhat::stats().enabled);
    }

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
