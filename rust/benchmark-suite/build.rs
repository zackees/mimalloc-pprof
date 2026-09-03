use std::env;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const LINK_ENV: &[&str] = &[
    "BENCH_ALLOCATOR_ID",
    "BENCH_ALLOCATOR_VERSION",
    "BENCH_ALLOCATOR_SOURCE_SHA",
    "BENCH_ALLOCATOR_LIBRARY",
    "BENCH_ALLOCATOR_LIBRARY_SHA256",
    "BENCH_ALLOCATOR_INCLUDE_DIRS",
    "BENCH_ALLOCATOR_LINK_MANIFEST",
];

fn main() {
    println!("cargo:rustc-check-cfg=cfg(benchmark_native_adapter)");
    for name in LINK_ENV {
        println!("cargo:rerun-if-env-changed={name}");
    }
    println!("cargo:rerun-if-env-changed=CC");
    println!("cargo:rerun-if-env-changed=CXX");
    println!("cargo:rerun-if-env-changed=AR");
    println!("cargo:rerun-if-changed=native/allocator_adapter.h");
    println!("cargo:rerun-if-changed=native/adapter_mimalloc.c");
    println!("cargo:rerun-if-changed=native/adapter_jemalloc.c");
    println!("cargo:rerun-if-changed=native/adapter_tcmalloc.cc");

    let present = LINK_ENV
        .iter()
        .filter(|name| env::var_os(name).is_some())
        .count();
    if present == 0 {
        emit_unlinked_identity();
        return;
    }
    if present != LINK_ENV.len() {
        panic!(
            "native benchmark child link requires all of {}; refusing a partial identity",
            LINK_ENV.join(", ")
        );
    }

    let target_os = env::var("CARGO_CFG_TARGET_OS").expect("Cargo target OS");
    let target_arch = env::var("CARGO_CFG_TARGET_ARCH").expect("Cargo target architecture");
    if target_os != "linux" || target_arch != "x86_64" {
        panic!(
            "native benchmark children support only x86_64 Linux, got {target_arch}-{target_os}"
        );
    }

    let allocator_id = required_env("BENCH_ALLOCATOR_ID");
    let allocator_version = required_env("BENCH_ALLOCATOR_VERSION");
    let source_sha = required_env("BENCH_ALLOCATOR_SOURCE_SHA");
    let library_sha = required_env("BENCH_ALLOCATOR_LIBRARY_SHA256");
    validate_identity(&allocator_id, &allocator_version, &source_sha, &library_sha);

    let primary_library = canonical_file(&required_env("BENCH_ALLOCATOR_LIBRARY"));
    require_archive(&primary_library);
    let manifest = canonical_file(&required_env("BENCH_ALLOCATOR_LINK_MANIFEST"));
    let link_inputs = parse_link_manifest(&manifest, &primary_library);
    let include_dirs = parse_include_dirs(&required_env("BENCH_ALLOCATOR_INCLUDE_DIRS"));
    let adapter_archive = compile_adapter(&allocator_id, &allocator_version, &include_dirs);

    println!("cargo:rustc-cfg=benchmark_native_adapter");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_ID={allocator_id}");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_VERSION={allocator_version}");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_SOURCE_SHA={source_sha}");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_LIBRARY_SHA256={library_sha}");

    // Absolute archives and an explicit group retain Bazel's complete TCMalloc
    // closure and make the actual final link auditable from the build log.
    println!("cargo:rustc-link-arg-bin=benchmark-child=-Wl,--start-group");
    println!(
        "cargo:rustc-link-arg-bin=benchmark-child={}",
        adapter_archive.display()
    );
    for input in link_inputs {
        match input {
            LinkInput::Archive { path, always_link } => {
                if always_link {
                    println!("cargo:rustc-link-arg-bin=benchmark-child=-Wl,--whole-archive");
                }
                println!(
                    "cargo:rustc-link-arg-bin=benchmark-child={}",
                    path.display()
                );
                if always_link {
                    println!("cargo:rustc-link-arg-bin=benchmark-child=-Wl,--no-whole-archive");
                }
            }
            LinkInput::Flag(flag) => {
                println!("cargo:rustc-link-arg-bin=benchmark-child={flag}");
            }
        }
    }
    println!("cargo:rustc-link-arg-bin=benchmark-child=-Wl,--end-group");
    if allocator_id == "tcmalloc" {
        println!("cargo:rustc-link-arg-bin=benchmark-child=-lstdc++");
        println!("cargo:rustc-link-arg-bin=benchmark-child=-pthread");
        println!("cargo:rustc-link-arg-bin=benchmark-child=-ldl");
        println!("cargo:rustc-link-arg-bin=benchmark-child=-lm");
    }
}

fn emit_unlinked_identity() {
    println!("cargo:rustc-env=BENCH_ALLOCATOR_ID=unlinked-test-adapter");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_VERSION=unlinked");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_SOURCE_SHA=unlinked");
    println!("cargo:rustc-env=BENCH_ALLOCATOR_LIBRARY_SHA256=unlinked");
}

fn required_env(name: &str) -> String {
    let value = env::var(name).unwrap_or_else(|_| panic!("missing {name}"));
    if value.is_empty() || value.contains(['\n', '\r', '\0']) {
        panic!("{name} must be a non-empty single-line value");
    }
    value
}

fn validate_identity(id: &str, version: &str, source_sha: &str, library_sha: &str) {
    const IDS: &[&str] = &[
        "tcmalloc",
        "jemalloc",
        "upstream-mimalloc",
        "bun-mimalloc",
        "mimalloc-pprof",
    ];
    if !IDS.contains(&id) {
        panic!("unsupported BENCH_ALLOCATOR_ID {id:?}");
    }
    if !is_lower_hex(source_sha, 40) {
        panic!("BENCH_ALLOCATOR_SOURCE_SHA must be a full lowercase Git SHA");
    }
    if !is_lower_hex(library_sha, 64) {
        panic!("BENCH_ALLOCATOR_LIBRARY_SHA256 must be a lowercase SHA-256");
    }
    if version.len() > 128
        || !version
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._@+-".contains(&byte))
    {
        panic!("BENCH_ALLOCATOR_VERSION contains unsupported characters");
    }
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn canonical_file(value: &str) -> PathBuf {
    let path = PathBuf::from(value);
    if !path.is_absolute() {
        panic!(
            "native link inputs must use absolute paths: {}",
            path.display()
        );
    }
    let canonical = fs::canonicalize(&path)
        .unwrap_or_else(|error| panic!("cannot resolve {}: {error}", path.display()));
    if !canonical.is_file() {
        panic!("native link input is not a file: {}", canonical.display());
    }
    canonical
}

fn require_archive(path: &Path) {
    let bytes =
        fs::read(path).unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    if !bytes.starts_with(b"!<arch>\n") && !bytes.starts_with(b"!<thin>\n") {
        panic!(
            "native allocator input is not a static archive: {}",
            path.display()
        );
    }
}

fn parse_include_dirs(value: &str) -> Vec<PathBuf> {
    env::split_paths(OsStr::new(value))
        .map(|path| {
            let canonical = fs::canonicalize(&path).unwrap_or_else(|error| {
                panic!("cannot resolve include {}: {error}", path.display())
            });
            if !canonical.is_dir() {
                panic!(
                    "adapter include input is not a directory: {}",
                    canonical.display()
                );
            }
            canonical
        })
        .collect()
}

enum LinkInput {
    Archive { path: PathBuf, always_link: bool },
    Flag(String),
}

fn parse_link_manifest(path: &Path, primary: &Path) -> Vec<LinkInput> {
    let input = fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    let mut result = Vec::new();
    let mut found_primary = false;
    for (index, line) in input.lines().enumerate() {
        let (kind, value) = line
            .split_once('\t')
            .unwrap_or_else(|| panic!("{}:{} must be KIND<TAB>VALUE", path.display(), index + 1));
        match kind {
            "archive" | "always" => {
                let archive = canonical_file(value);
                require_archive(&archive);
                found_primary |= archive == primary;
                result.push(LinkInput::Archive {
                    path: archive,
                    always_link: kind == "always",
                });
            }
            "flag" => {
                if value.is_empty()
                    || value.contains(char::is_whitespace)
                    || !value.starts_with('-')
                {
                    panic!("{}:{} has an unsafe linker flag", path.display(), index + 1);
                }
                result.push(LinkInput::Flag(value.to_owned()));
            }
            _ => panic!(
                "{}:{} has unknown link-input kind {kind:?}",
                path.display(),
                index + 1
            ),
        }
    }
    if result.is_empty() || !found_primary {
        panic!("link manifest must contain the locked primary allocator archive");
    }
    result
}

fn compile_adapter(id: &str, version: &str, includes: &[PathBuf]) -> PathBuf {
    let manifest_dir = PathBuf::from(required_env("CARGO_MANIFEST_DIR"));
    let out_dir = PathBuf::from(required_env("OUT_DIR"));
    let (source_name, compiler_var, compiler_default, cxx) = match id {
        "tcmalloc" => ("adapter_tcmalloc.cc", "CXX", "c++", true),
        "jemalloc" => ("adapter_jemalloc.c", "CC", "cc", false),
        "upstream-mimalloc" | "bun-mimalloc" | "mimalloc-pprof" => {
            ("adapter_mimalloc.c", "CC", "cc", false)
        }
        _ => unreachable!(),
    };
    let source = manifest_dir.join("native").join(source_name);
    let object = out_dir.join("benchmark_allocator_adapter.o");
    let archive = out_dir.join("libbenchmark_allocator_adapter.a");
    let compiler = env::var(compiler_var).unwrap_or_else(|_| compiler_default.to_owned());
    let mut command = tool_command(&compiler, compiler_var);
    command
        .arg("-c")
        .arg(&source)
        .arg("-o")
        .arg(&object)
        .arg("-O3")
        .arg("-fno-omit-frame-pointer")
        .arg("-fPIC")
        .arg("-I")
        .arg(manifest_dir.join("native"))
        .arg(format!("-DBENCH_ALLOCATOR_ID=\"{id}\""))
        .arg(format!("-DBENCH_ALLOCATOR_VERSION=\"{version}\""));
    if cxx {
        command.arg("-std=c++17");
    } else {
        command.arg("-std=c11");
    }
    for include in includes {
        command.arg("-I").arg(include);
    }
    let status = command
        .status()
        .unwrap_or_else(|error| panic!("cannot execute adapter compiler {compiler}: {error}"));
    if !status.success() {
        panic!("adapter compiler exited with {status}");
    }
    let ar = env::var("AR").unwrap_or_else(|_| "ar".to_owned());
    let status = tool_command(&ar, "AR")
        .arg("crs")
        .arg(&archive)
        .arg(&object)
        .status()
        .unwrap_or_else(|error| panic!("cannot execute archive tool {ar}: {error}"));
    if !status.success() {
        panic!("adapter archive tool exited with {status}");
    }
    archive
}

fn tool_command(value: &str, variable: &str) -> Command {
    let parts: Vec<_> = value.split_ascii_whitespace().collect();
    if parts.is_empty()
        || parts.iter().any(|part| {
            part.is_empty()
                || part
                    .bytes()
                    .any(|byte| matches!(byte, b'\'' | b'"' | b'`' | b'$' | b';' | b'|' | b'&'))
        })
    {
        panic!("{variable} has an unsupported compiler-wrapper command: {value:?}");
    }
    let mut command = Command::new(parts[0]);
    command.args(&parts[1..]);
    command
}
