mod common;
use mimalloc_pprof::{prof, sys};
use std::process::Command;

fn stack_count() -> usize {
    let mut stacks = 0;
    unsafe {
        sys::mi_prof_debug_stats(std::ptr::null_mut(), std::ptr::null_mut(), &mut stacks);
    }
    stacks
}

#[test]
fn accumulation_modes() {
    let child = std::env::var_os("MIMALLOC_PPROF_T4_CHILD").is_some();
    if !child {
        for accum in [false, true] {
            let mut command = Command::new(std::env::current_exe().unwrap());
            command
                .arg("--exact")
                .arg("accumulation_modes")
                .env("MIMALLOC_PPROF_T4_CHILD", "1");
            if accum {
                command.env("MIMALLOC_PROF_ACCUM", "1");
            } else {
                command.env_remove("MIMALLOC_PROF_ACCUM");
            }
            assert!(command.status().unwrap().success(), "accum={accum}");
        }
        return;
    }

    let accum = std::env::var("MIMALLOC_PROF_ACCUM").as_deref() == Ok("1");
    common::start(1, 83);
    let mut blocks: Vec<Option<Box<[u8; 256]>>> =
        (0..128).map(|_| Some(Box::new([0; 256]))).collect();
    let (_, _, upper0, upper_bytes0, _) = common::header(&common::dump());
    for item in blocks.iter_mut().take(64) {
        item.take();
    }
    let (live1, live_bytes1, upper1, upper_bytes1, _) = common::header(&common::dump());
    if accum {
        assert!(upper1 >= upper0 && upper1 >= live1);
        assert!(upper_bytes1 >= upper_bytes0 && upper_bytes1 >= live_bytes1);
    } else {
        assert_eq!((upper0, upper_bytes0, upper1, upper_bytes1), (0, 0, 0, 0));
    }
    for item in &mut blocks {
        item.take();
    }
    drop(blocks);
    if accum {
        let before_reset = stack_count();
        assert!(before_reset > 0);
        prof::reset();
        let (_, _, upper, upper_bytes, _) = common::header(&common::dump());
        assert_eq!((upper, upper_bytes), (0, 0));
    }
    common::stop();
}
