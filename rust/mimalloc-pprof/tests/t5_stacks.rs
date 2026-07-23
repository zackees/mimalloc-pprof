mod common;

#[inline(never)]
fn a() -> usize {
    std::hint::black_box(b())
}
#[inline(never)]
fn b() -> usize {
    std::hint::black_box(c())
}
#[inline(never)]
fn c() -> usize {
    let mut values = Vec::<u8>::with_capacity(4096);
    values.push(7);
    assert_eq!(values[0], 7);
    let values = std::hint::black_box(values);
    std::mem::forget(values);
    7
}

#[test]
fn samples_include_multiple_caller_pcs() {
    common::start(1, 59);
    for _ in 0..128 {
        std::hint::black_box(a());
    }
    let text = common::dump();
    let sample = common::sample_lines(&text)
        .into_iter()
        .next()
        .expect("sample line");
    assert!(sample.matches("0x").count() >= 3, "{sample}");
    common::stop();
}
