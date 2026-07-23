mod common;

#[test]
fn heap_v2_text_has_samples_and_maps() {
    common::start(65_536, 17);
    let leaked: Vec<_> = (0..1000)
        .map(|_| Box::leak(Box::new([0_u8; 4096])))
        .collect();
    let text = common::dump();
    let (_, _, _, _, rate) = common::header(&text);
    assert_eq!(rate, 65_536);
    assert!(!common::sample_lines(&text).is_empty());
    assert!(text.contains("MAPPED_LIBRARIES:\n"));
    assert!(text
        .lines()
        .skip_while(|l| *l != "MAPPED_LIBRARIES:")
        .skip(1)
        .any(|l| !l.is_empty()));
    std::mem::forget(leaked);
    common::stop();
}
