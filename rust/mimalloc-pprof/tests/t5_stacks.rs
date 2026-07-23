mod common;

#[inline(never)]
fn a() {
    b()
}
#[inline(never)]
fn b() {
    c()
}
#[inline(never)]
fn c() {
    let mut values = Vec::<u8>::with_capacity(4096);
    values.push(7);
    assert_eq!(values[0], 7);
    std::hint::black_box(&values);
    std::mem::forget(values);
}

#[test]
fn samples_include_multiple_caller_pcs() {
    common::start(1, 59);
    for _ in 0..128 {
        a();
    }
    let text = common::dump();
    let sample = common::sample_lines(&text)
        .into_iter()
        .next()
        .expect("sample line");
    assert!(sample.matches("0x").count() >= 3, "{sample}");
    common::stop();
}
