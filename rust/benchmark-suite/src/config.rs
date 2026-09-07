use serde::Deserialize;

/// Complete immutable identity for a directly-linked allocator child.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AllocatorLock {
    pub schema_version: u32,
    pub target: String,
    pub allocators: Vec<AllocatorPin>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AllocatorPin {
    pub id: String,
    pub pin: String,
    pub source: AllocatorSource,
    pub build: AllocatorBuild,
    #[serde(rename = "expected_static_library")]
    pub static_library: String,
    pub adapter_kind: String,
    pub license: String,
    pub patches: AllocatorPatches,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AllocatorSource {
    pub kind: String,
    #[serde(rename = "canonical_repository")]
    pub repository: String,
    pub commit: String,
    pub archive_url: Option<String>,
    pub archive_sha256: Option<String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AllocatorBuild {
    pub system: String,
    pub commands: Vec<Vec<String>>,
    pub flags: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AllocatorPatches {
    pub source: Vec<SourcePatch>,
    pub build: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct SourcePatch {
    pub file: String,
    pub sha256: String,
}

impl AllocatorLock {
    pub fn parse_and_validate(input: &str) -> Result<Self, String> {
        let lock: Self = serde_json::from_str(input).map_err(|error| error.to_string())?;
        let expected = [
            "tcmalloc",
            "jemalloc",
            "upstream-mimalloc",
            "bun-mimalloc",
            "mimalloc-pprof",
        ];
        if lock.schema_version != 1
            || lock.target != "x86_64-unknown-linux-gnu"
            || lock
                .allocators
                .iter()
                .map(|pin| pin.id.as_str())
                .collect::<Vec<_>>()
                != expected
        {
            return Err("lockfile must define the exact five Linux allocator IDs in order".into());
        }
        for pin in &lock.allocators {
            if pin.repository_is_invalid()
                || pin.build.commands.is_empty()
                || pin.static_library.is_empty()
                || !pin.patches.build.is_empty()
                || pin
                    .patches
                    .source
                    .iter()
                    .any(|patch| !is_patch_filename(&patch.file) || !is_hex(&patch.sha256, 64))
            {
                return Err(format!("allocator {} has incomplete provenance", pin.id));
            }
            let pin_matches = match pin.id.as_str() {
                "tcmalloc" => {
                    pin.pin == "c316de3ee8ffb5aff6547aa10151bc8dda3a2942"
                        && pin.source.commit == "c316de3ee8ffb5aff6547aa10151bc8dda3a2942"
                }
                "jemalloc" => {
                    pin.pin == "5.3.1@81034ce1f1373e37dc865038e1bc8eeecf559ce8"
                        && pin.source.commit == "81034ce1f1373e37dc865038e1bc8eeecf559ce8"
                }
                "upstream-mimalloc" => {
                    pin.pin == "dev3@6def7be9"
                        && pin.source.commit == "6def7be9458fb8a97b8323af3fb0b0ae04387065"
                }
                "bun-mimalloc" => {
                    pin.pin == "bun-dev3-v2@b20b60d9"
                        && pin.source.commit == "b20b60d959093b1bc0e24306ec72ccacb3e46fb9"
                }
                "mimalloc-pprof" => {
                    pin.pin == "workflow-source" && pin.source.commit == "workflow-source"
                }
                _ => false,
            };
            if !pin_matches {
                return Err(format!(
                    "allocator {} does not match its prescribed pin",
                    pin.id
                ));
            }
            if pin.id == "mimalloc-pprof" {
                if pin.pin != "workflow-source"
                    || pin.source.kind != "checkout"
                    || pin.source.commit != "workflow-source"
                {
                    return Err("mimalloc-pprof must use the workflow source identity".into());
                }
                if !pin.patches.source.is_empty() {
                    return Err("mimalloc-pprof may not patch the workflow source checkout".into());
                }
            } else if pin.source.kind != "archive"
                || !is_hex(&pin.source.commit, 40)
                || !is_hex(pin.source.archive_sha256.as_deref().unwrap_or_default(), 64)
                || pin
                    .source
                    .archive_url
                    .as_deref()
                    .unwrap_or_default()
                    .is_empty()
            {
                return Err(format!(
                    "allocator {} has a floating or incomplete archive identity",
                    pin.id
                ));
            }
        }
        Ok(lock)
    }
}

impl AllocatorPin {
    fn repository_is_invalid(&self) -> bool {
        !self.source.repository.starts_with("https://")
            || self.adapter_kind.is_empty()
            || self.license.is_empty()
    }
}

fn is_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_patch_filename(value: &str) -> bool {
    !value.is_empty()
        && value.ends_with(".patch")
        && !value.contains(['/', '\\'])
        && value != "."
        && value != ".."
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uppercase_source_identity_is_rejected() {
        let lock = include_str!("../allocators/allocator-lock.json");
        AllocatorLock::parse_and_validate(lock).unwrap();
        let uppercase = lock.replace(
            "c316de3ee8ffb5aff6547aa10151bc8dda3a2942",
            "C316de3ee8ffb5aff6547aa10151bc8dda3a2942",
        );
        assert!(AllocatorLock::parse_and_validate(&uppercase).is_err());
    }
}
