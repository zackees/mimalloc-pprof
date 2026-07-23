#![allow(dead_code)]

use mimalloc_pprof::{prof, MiMalloc};
use regex::Regex;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

pub fn start(rate: usize, seed: u64) {
    if prof::is_enabled() {
        prof::stop();
    }
    assert!(prof::start_seeded(rate, seed));
}

pub fn dump() -> String {
    String::from_utf8(prof::dump_to_vec()).expect("heap profile is UTF-8")
}

pub fn header(text: &str) -> (u64, u64, u64, u64, u64) {
    let re = Regex::new(
        r"^heap profile: +([0-9]+): ([0-9]+) \[ *([0-9]+): ([0-9]+)\] @ heap_v2/([0-9]+)$",
    )
    .unwrap();
    let caps = re
        .captures(text.lines().next().unwrap())
        .expect("heap_v2 header");
    (
        caps[1].parse().unwrap(),
        caps[2].parse().unwrap(),
        caps[3].parse().unwrap(),
        caps[4].parse().unwrap(),
        caps[5].parse().unwrap(),
    )
}

pub fn sample_lines(text: &str) -> Vec<&str> {
    let re = Regex::new(r"^ +[0-9]+: [0-9]+ \[ *[0-9]+: [0-9]+\] @( 0x[0-9a-fA-F]+)+$").unwrap();
    text.lines().filter(|line| re.is_match(line)).collect()
}

pub fn stop() {
    prof::stop();
}
