//! `heap_snapshot_to_file` (issue #338, Bun parity): the safe wrapper writes a file that
//! starts with the format-1 header and ends with the `END` footer, both flag settings
//! accepted, and an unwritable path is an `Err`, not a panic.

mod common;
use mimalloc_pprof::{heap_snapshot_to_file, sys};

const MAGIC: u32 = 0x5348494D; // 'MIHS'
const SEC_END: u32 = 0x444E4520; // ' END'

fn read_u32(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

#[test]
fn snapshot_has_header_and_footer() {
    let dir = std::env::temp_dir().join(format!("mimalloc-pprof-t18-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    // something to describe: a mix of sizes, some freed again
    let keep: Vec<Vec<u8>> = (0..200).map(|i| vec![1u8; 32 + (i * 41) % 3000]).collect();
    for (blocks, name) in [(false, "pages.bin"), (true, "blocks.bin")] {
        let path = dir.join(name);
        heap_snapshot_to_file(&path, blocks).expect("snapshot written");
        let bytes = std::fs::read(&path).unwrap();
        assert!(bytes.len() > 64, "{name}: too short ({} bytes)", bytes.len());
        assert_eq!(read_u32(&bytes, 0), MAGIC, "{name}: magic");
        assert_eq!(read_u32(&bytes, 4), 1, "{name}: version");
        let flags = read_u32(&bytes, 16);
        assert_eq!(flags & sys::MI_SNAPSHOT_BLOCKS, u32::from(blocks), "{name}: flags");
        // footer: u32 ' END' then u64 page_count
        let tail = bytes.len() - 12;
        assert_eq!(read_u32(&bytes, tail), SEC_END, "{name}: END footer");
        let pages = u64::from_le_bytes(bytes[tail + 4..].try_into().unwrap());
        assert!(pages > 0, "{name}: no pages recorded");
    }
    drop(keep);
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn unwritable_path_is_an_error() {
    let bad = std::env::temp_dir().join("no-such-dir-mimalloc-pprof-t18").join("x.bin");
    assert!(heap_snapshot_to_file(&bad, false).is_err());
}
