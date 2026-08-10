use std::collections::BTreeMap;
use std::io::Read;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::config::AllocatorLock;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ToolchainProvenance {
    pub compiler: String,
    pub linker: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourcePatchProvenance {
    pub file: String,
    pub sha256: String,
}

/// The subset of one producer record required to launch and identify a child.
/// Unknown producer fields remain allowed so the Python artifact can retain
/// richer link/symbol/build evidence without duplicating that schema in Rust.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AllocatorProvenance {
    #[serde(rename = "id")]
    pub allocator_id: String,
    #[serde(rename = "version")]
    pub allocator_version: String,
    pub canonical_repository: String,
    pub source_sha: String,
    pub source_archive_url: Option<String>,
    #[serde(rename = "source_archive_sha256")]
    pub source_archive_sha256: Option<String>,
    pub source_tree_sha256: String,
    pub source_patches: Vec<SourcePatchProvenance>,
    #[serde(rename = "library_sha256")]
    pub static_library_sha256: String,
    #[serde(rename = "library")]
    pub static_library: String,
    #[serde(rename = "child_binary")]
    pub child_binary: String,
    pub child_binary_sha256: String,
    #[serde(rename = "commands")]
    pub build_commands: Vec<Vec<String>>,
    pub toolchain: ToolchainProvenance,
    pub build_flags: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct IntentionalMimallocDifference {
    pub field: String,
    #[serde(rename = "upstream-mimalloc")]
    pub upstream_mimalloc: String,
    #[serde(rename = "mimalloc-pprof")]
    pub mimalloc_pprof: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MimallocOptionComparison {
    pub equivalent_fields: BTreeMap<String, String>,
    pub intentional_difference: IntentionalMimallocDifference,
    pub runtime_disabled_state: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProducerProvenance {
    pub schema_version: u32,
    pub lockfile_sha256: String,
    pub environment: BTreeMap<String, String>,
    pub tool_versions: BTreeMap<String, String>,
    pub build_elapsed_seconds: f64,
    pub mimalloc_option_comparison: MimallocOptionComparison,
    pub allocators: Vec<AllocatorProvenance>,
}

impl ProducerProvenance {
    pub fn parse_and_validate(input: &str, lock: &AllocatorLock) -> Result<Self, String> {
        let provenance: Self = serde_json::from_str(input).map_err(|error| error.to_string())?;
        if provenance.schema_version != 1 || provenance.allocators.len() != 4 {
            return Err("producer provenance must contain exactly four allocator builds".into());
        }
        if !provenance.build_elapsed_seconds.is_finite() || provenance.build_elapsed_seconds < 0.0 {
            return Err("producer build elapsed time must be finite and nonnegative".into());
        }
        provenance.validate_mimalloc_options()?;
        let embedded_lock_sha256 =
            sha256_bytes(include_bytes!("../allocators/allocator-lock.json"));
        if provenance.lockfile_sha256 != embedded_lock_sha256 {
            return Err("producer provenance was built from a different allocator lockfile".into());
        }
        validate_distinct_child_hashes(&provenance.allocators)?;
        for pin in &lock.allocators {
            let built = provenance
                .allocators
                .iter()
                .find(|item| item.allocator_id == pin.id)
                .ok_or_else(|| format!("producer provenance is missing {}", pin.id))?;
            let expected_source = if pin.id == "mimalloc-pprof" {
                // The workflow source pin is resolved to the checked-out full
                // commit by the deterministic producer.
                None
            } else {
                Some(pin.source.commit.as_str())
            };
            if expected_source.is_some_and(|expected| built.source_sha != expected)
                || built.canonical_repository != pin.source.repository
                || built.source_archive_url.as_deref() != pin.source.archive_url.as_deref()
                || (pin.id != "mimalloc-pprof"
                    && built.source_archive_sha256.as_deref()
                        != pin.source.archive_sha256.as_deref())
                || !is_lower_hex(&built.source_sha, 40)
                || !is_lower_hex(&built.source_tree_sha256, 64)
                || (pin.id != "mimalloc-pprof"
                    && !is_lower_hex(
                        built.source_archive_sha256.as_deref().unwrap_or_default(),
                        64,
                    ))
                || !is_lower_hex(&built.static_library_sha256, 64)
                || !is_lower_hex(&built.child_binary_sha256, 64)
                || built.allocator_version.is_empty()
                || built.static_library.is_empty()
                || built.child_binary.is_empty()
                || built.build_commands.is_empty()
                || built.toolchain.compiler.is_empty()
                || built.toolchain.linker.is_empty()
                || built.source_patches
                    != pin
                        .patches
                        .source
                        .iter()
                        .map(|patch| SourcePatchProvenance {
                            file: patch.file.clone(),
                            sha256: patch.sha256.clone(),
                        })
                        .collect::<Vec<_>>()
            {
                return Err(format!("producer provenance for {} is invalid", pin.id));
            }
        }
        Ok(provenance)
    }

    fn validate_mimalloc_options(&self) -> Result<(), String> {
        let expected_equivalent = BTreeMap::from([
            ("MI_BUILD_SHARED".into(), "OFF".into()),
            ("MI_BUILD_STATIC".into(), "ON".into()),
            ("MI_BUILD_TESTS".into(), "OFF".into()),
            ("MI_OPT_ARCH".into(), "OFF".into()),
            ("MI_OPT_SIMD".into(), "ON".into()),
            ("build_type".into(), "Release".into()),
            ("frame_pointers".into(), "-fno-omit-frame-pointer".into()),
            ("optimization".into(), "-O3".into()),
        ]);
        let expected_runtime = BTreeMap::from([
            ("MIMALLOC_MEMORY_EVENTS".into(), "0".into()),
            ("MIMALLOC_PROF".into(), "0".into()),
        ]);
        let difference = &self.mimalloc_option_comparison.intentional_difference;
        if self.mimalloc_option_comparison.equivalent_fields != expected_equivalent
            || self.mimalloc_option_comparison.runtime_disabled_state != expected_runtime
            || difference.field != "MI_PPROF"
            || difference.upstream_mimalloc != "OFF"
            || difference.mimalloc_pprof != "ON"
        {
            return Err("mimalloc build/runtime option comparison is incomplete or false".into());
        }
        Ok(())
    }

    /// Hash the exact files immediately before launch. Provenance strings are
    /// never accepted as proof that the artifacts on disk are unchanged.
    pub fn validate_artifact_hashes(&self) -> Result<(), String> {
        for allocator in &self.allocators {
            validate_file_hash(
                &allocator.static_library,
                &allocator.static_library_sha256,
                &format!("{} static library", allocator.allocator_id),
            )?;
            validate_file_hash(
                &allocator.child_binary,
                &allocator.child_binary_sha256,
                &format!("{} child binary", allocator.allocator_id),
            )?;
        }
        Ok(())
    }
}

fn validate_file_hash(path: &str, expected: &str, label: &str) -> Result<(), String> {
    if !Path::new(path).is_file() {
        return Err(format!("{label} does not exist: {path}"));
    }
    let actual = sha256_file(Path::new(path))?;
    if actual != expected {
        return Err(format!(
            "{label} SHA-256 mismatch: expected {expected}, got {actual}"
        ));
    }
    Ok(())
}

/// Reject accidental same-binary/multiple-allocator link reuse.
pub fn validate_distinct_child_hashes(provenance: &[AllocatorProvenance]) -> Result<(), String> {
    if provenance.len() != 4 {
        return Err("expected four allocator child artifacts".into());
    }
    for (index, item) in provenance.iter().enumerate() {
        if provenance
            .iter()
            .skip(index + 1)
            .any(|other| other.child_binary_sha256 == item.child_binary_sha256)
        {
            return Err("allocator child hashes must be distinct".into());
        }
    }
    Ok(())
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = std::fs::File::open(path)
        .map_err(|error| format!("open {} for SHA-256: {error}", path.display()))?;
    let mut hash = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("read {} for SHA-256: {error}", path.display()))?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    Ok(hex_digest(hash.finish()))
}

pub fn sha256_bytes(bytes: &[u8]) -> String {
    let mut hash = Sha256::new();
    hash.update(bytes);
    hex_digest(hash.finish())
}

fn hex_digest(digest: [u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(64);
    for byte in digest {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

struct Sha256 {
    state: [u32; 8],
    buffer: [u8; 64],
    buffered: usize,
    bit_length: u64,
}

impl Sha256 {
    fn new() -> Self {
        Self {
            state: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            buffer: [0; 64],
            buffered: 0,
            bit_length: 0,
        }
    }

    fn update(&mut self, mut input: &[u8]) {
        self.bit_length = self
            .bit_length
            .wrapping_add((input.len() as u64).wrapping_mul(8));
        if self.buffered > 0 {
            let count = (64 - self.buffered).min(input.len());
            self.buffer[self.buffered..self.buffered + count].copy_from_slice(&input[..count]);
            self.buffered += count;
            input = &input[count..];
            if self.buffered == 64 {
                let block = self.buffer;
                self.transform(&block);
                self.buffered = 0;
            } else {
                return;
            }
        }
        while input.len() >= 64 {
            let block: &[u8; 64] = input[..64].try_into().expect("64-byte SHA-256 block");
            self.transform(block);
            input = &input[64..];
        }
        self.buffer[..input.len()].copy_from_slice(input);
        self.buffered = input.len();
    }

    fn finish(mut self) -> [u8; 32] {
        self.buffer[self.buffered] = 0x80;
        self.buffered += 1;
        if self.buffered > 56 {
            self.buffer[self.buffered..].fill(0);
            let block = self.buffer;
            self.transform(&block);
            self.buffer = [0; 64];
        } else {
            self.buffer[self.buffered..56].fill(0);
        }
        self.buffer[56..64].copy_from_slice(&self.bit_length.to_be_bytes());
        let block = self.buffer;
        self.transform(&block);
        let mut output = [0_u8; 32];
        for (chunk, word) in output.chunks_exact_mut(4).zip(self.state) {
            chunk.copy_from_slice(&word.to_be_bytes());
        }
        output
    }

    fn transform(&mut self, block: &[u8; 64]) {
        const K: [u32; 64] = [
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
            0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
            0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
            0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
            0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
            0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
            0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
            0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
            0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
            0xc67178f2,
        ];
        let mut schedule = [0_u32; 64];
        for (index, chunk) in block.chunks_exact(4).enumerate() {
            schedule[index] = u32::from_be_bytes(chunk.try_into().expect("four-byte word"));
        }
        for index in 16..64 {
            let s0 = schedule[index - 15].rotate_right(7)
                ^ schedule[index - 15].rotate_right(18)
                ^ (schedule[index - 15] >> 3);
            let s1 = schedule[index - 2].rotate_right(17)
                ^ schedule[index - 2].rotate_right(19)
                ^ (schedule[index - 2] >> 10);
            schedule[index] = schedule[index - 16]
                .wrapping_add(s0)
                .wrapping_add(schedule[index - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = self.state;
        for index in 0..64 {
            let sum1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choose = (e & f) ^ ((!e) & g);
            let temporary1 = h
                .wrapping_add(sum1)
                .wrapping_add(choose)
                .wrapping_add(K[index])
                .wrapping_add(schedule[index]);
            let sum0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let temporary2 = sum0.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temporary1);
            d = c;
            c = b;
            b = a;
            a = temporary1.wrapping_add(temporary2);
        }
        for (state, value) in self.state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *state = state.wrapping_add(value);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_matches_fips_vector() {
        assert_eq!(
            sha256_bytes(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_bytes(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let mut fragmented = Sha256::new();
        fragmented.update(b"a");
        fragmented.update(b"b");
        fragmented.update(b"c");
        assert_eq!(
            hex_digest(fragmented.finish()),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_bytes(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
    }
}
