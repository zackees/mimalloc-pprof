mod common;

#[test]
fn sampled_stream_estimates_one_hundred_mib() {
    common::start(524_288, 43);
    let blocks: Vec<_> = (0..25_600).map(|_| Box::new([0_u8; 4096])).collect();
    let text = common::dump();
    let (objects, _, _, _, rate) = common::header(&text);
    assert_eq!(rate, 524_288);
    assert!((100..=400).contains(&objects));
    let estimated = objects * rate;
    assert!((50 * 1024 * 1024..=200 * 1024 * 1024).contains(&estimated));
    std::mem::forget(blocks);
    common::stop();
}
