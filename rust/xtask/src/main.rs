//! Dev-tooling for `mimalloc-pprof`: regenerates the vendored, single-file C
//! amalgamation that `rust/mimalloc-pprof`'s `build.rs` compiles.
//!
//! This is a **local-include inliner**, not a C preprocessor: it recursively
//! resolves `#include "quoted/local/path"` lines by splicing the referenced
//! file's full text in place (deduping already-inlined files by canonical
//! path), and leaves `#include <angle-bracket>` lines and every
//! `#if`/`#ifdef`/`#define`/... directive completely untouched.
//!
//! Subcommands:
//! - `amalgamate-c`: src/static.c -> rust/mimalloc-pprof/vendor/mimalloc-pprof-amalgamated.c
//! - `amalgamate-h`: the three public headers -> .../mimalloc-pprof-amalgamated.h
//! - `check`: regenerate both in-memory and diff against the checked-in vendor/
//!   copies (ignoring the commit-SHA stamp line), used by CI to catch drift
//!   between src/include/ and the vendored files.

use std::collections::HashSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

struct Paths {
    repo_root: PathBuf,
    include_root: PathBuf,
    src_root: PathBuf,
    vendor_dir: PathBuf,
}

impl Paths {
    fn discover() -> Self {
        // rust/xtask/Cargo.toml -> repo root is two levels up from rust/xtask.
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent() // rust/
            .and_then(Path::parent) // repo root
            .expect("rust/xtask is expected to live at <repo_root>/rust/xtask")
            .to_path_buf();
        // Canonicalize repo_root too, not just the paths passed to display_rel: on
        // Windows, fs::canonicalize prefixes paths with the extended-length \\?\
        // marker, which repo_root (straight from CARGO_MANIFEST_DIR) does not have.
        // strip_prefix requires an exact component-wise match, so without this the
        // prefix silently fails to strip and display_rel falls back to embedding
        // the full absolute, machine-specific path in the generated output --
        // exactly the non-portable content `check` exists to catch as drift.
        let repo_root = fs::canonicalize(&repo_root).unwrap_or(repo_root);
        let include_root = repo_root.join("include");
        let src_root = repo_root.join("src");
        let vendor_dir = repo_root.join("rust").join("mimalloc-pprof").join("vendor");
        Paths {
            repo_root,
            include_root,
            src_root,
            vendor_dir,
        }
    }

    /// Render `path` relative to the repo root with forward slashes, so the
    /// generated output is identical regardless of where the repo is checked
    /// out (absolute paths would make `check` fail spuriously between a
    /// dev machine and CI).
    fn display_rel(&self, path: &Path) -> String {
        let rel = path.strip_prefix(&self.repo_root).unwrap_or(path);
        rel.to_string_lossy().replace('\\', "/")
    }
}

const AMALGAMATED_C_NAME: &str = "mimalloc-pprof-amalgamated.c";
const AMALGAMATED_H_NAME: &str = "mimalloc-pprof-amalgamated.h";

/// A couple of public headers are pulled in via `#include <angle-bracket>`
/// (not quotes) from inside files the inliner *does* process -- notably
/// `include/mimalloc/types.h`'s `#include <mimalloc-stats.h>`, which in turn
/// pulls in `#include <mimalloc.h>`. Angle-bracket includes are intentionally
/// left untouched by the inliner (they're indistinguishable, syntactically,
/// from genuine system headers), but that means the compiler still needs to
/// find these two specific local headers on its search path. Rather than
/// pointing `-I` at the real `include/` directory (which would break the
/// "vendor/ is fully self-contained" property build.rs relies on), verbatim
/// copies of just these two files are synced into vendor/ alongside the
/// amalgamated .c, and build.rs adds a single `-I<vendor>` (pointing inside
/// vendor, not outside it). `mimalloc.h`'s copy is inert at compile time:
/// its content is already fully inlined earlier in the amalgamated .c, so
/// its own `#ifndef MIMALLOC_H` guard is already tripped by the time the
/// angle-bracket `#include <mimalloc.h>` re-opens it -- the file just needs
/// to physically exist for the `#include` to resolve.
const ANGLE_BRACKET_SUPPORT_HEADERS: &[&str] = &["mimalloc.h", "mimalloc-stats.h"];

fn main() {
    let args: Vec<String> = env::args().collect();
    let cmd = args.get(1).map(String::as_str).unwrap_or("");
    let paths = Paths::discover();

    match cmd {
        "amalgamate-c" => {
            let out = amalgamate_c(&paths);
            write_vendor_file(&paths, AMALGAMATED_C_NAME, &out);
            sync_support_headers(&paths);
        }
        "amalgamate-h" => {
            let out = amalgamate_h(&paths);
            write_vendor_file(&paths, AMALGAMATED_H_NAME, &out);
        }
        "check" => {
            if !check(&paths) {
                std::process::exit(1);
            }
            println!("xtask check: vendored amalgamation matches src/include/.");
        }
        other => {
            eprintln!("unknown or missing subcommand: {other:?}");
            eprintln!("usage: cargo run -p xtask -- <amalgamate-c|amalgamate-h|check>");
            std::process::exit(2);
        }
    }
}

fn write_vendor_file(paths: &Paths, file_name: &str, content: &str) {
    fs::create_dir_all(&paths.vendor_dir)
        .unwrap_or_else(|e| panic!("failed to create {}: {e}", paths.vendor_dir.display()));
    let out_path = paths.vendor_dir.join(file_name);
    fs::write(&out_path, content)
        .unwrap_or_else(|e| panic!("failed to write {}: {e}", out_path.display()));
    println!("wrote {}", out_path.display());
}

/// Verbatim (unmodified) copies of [`ANGLE_BRACKET_SUPPORT_HEADERS`] into
/// vendor/, so the amalgamated .c can resolve its two local angle-bracket
/// includes without an `-I` flag pointing outside vendor/.
fn sync_support_headers(paths: &Paths) {
    for name in ANGLE_BRACKET_SUPPORT_HEADERS {
        let src = paths.include_root.join(name);
        let content = fs::read_to_string(&src)
            .unwrap_or_else(|e| panic!("failed to read {}: {e}", src.display()));
        write_vendor_file(paths, name, &content);
    }
}

/// Amalgamate `src/static.c` (and everything it transitively `#include`s
/// with quotes) into one self-contained translation unit.
fn amalgamate_c(paths: &Paths) -> String {
    let mut visited = HashSet::new();
    let mut body = String::new();
    inline_file(&paths.src_root.join("static.c"), paths, &mut visited, &mut body);
    let header = generated_header(paths, "src/static.c", "amalgamate-c");
    format!("{header}{body}")
}

/// Amalgamate the three public headers (in this fixed order, sharing one
/// dedup set so profile.h/memory-events.h's own `#include "mimalloc.h"`
/// doesn't duplicate content already inlined for mimalloc.h) into one
/// self-contained public header.
fn amalgamate_h(paths: &Paths) -> String {
    let mut visited = HashSet::new();
    let mut body = String::new();
    for rel in ["mimalloc.h", "mimalloc/profile.h", "mimalloc/memory-events.h"] {
        inline_file(&paths.include_root.join(rel), paths, &mut visited, &mut body);
    }
    let header = generated_header(
        paths,
        "the three public headers (mimalloc.h, mimalloc/profile.h, mimalloc/memory-events.h)",
        "amalgamate-h",
    );
    format!("{header}{body}")
}

fn generated_header(paths: &Paths, source_desc: &str, subcommand: &str) -> String {
    let sha = git_short_sha(&paths.repo_root);
    format!(
        "/* GENERATED FILE -- DO NOT EDIT. Produced by rust/xtask from commit {sha} of {source_desc}. Regenerate with: cargo run -p xtask -- {subcommand} */\n\n"
    )
}

fn git_short_sha(repo_root: &Path) -> String {
    let output = Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .current_dir(repo_root)
        .output()
        .expect("failed to run `git rev-parse --short HEAD` (is git on PATH?)");
    if !output.status.success() {
        panic!(
            "git rev-parse --short HEAD failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

/// Recursively inline `path` into `out`, resolving every `#include "..."`
/// line it contains and skipping files already inlined (tracked in
/// `visited` by canonical path). `#include <...>` lines and every
/// preprocessor conditional/definition are passed through verbatim.
fn inline_file(path: &Path, paths: &Paths, visited: &mut HashSet<PathBuf>, out: &mut String) {
    let canon = fs::canonicalize(path)
        .unwrap_or_else(|e| panic!("cannot resolve include path {}: {e}", path.display()));
    if !visited.insert(canon.clone()) {
        // Already inlined from an earlier #include elsewhere; this file's own
        // #pragma once / #ifndef guard would neutralize a second physical
        // inclusion anyway, but we dedup here too so the amalgamated output
        // doesn't carry duplicate copies of the same source text.
        return;
    }

    let content = fs::read_to_string(&canon)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", canon.display()));
    let current_dir = canon
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();

    let rel = paths.display_rel(&canon);
    out.push_str(&format!("/* ---- begin inlined: {rel} ---- */\n"));
    for line in content.lines() {
        match parse_quoted_include(line) {
            Some(inc) => match resolve_include(&inc, &current_dir, paths) {
                Some(resolved) => inline_file(&resolved, paths, visited, out),
                None => panic!(
                    "could not resolve #include \"{inc}\" referenced from {} \
                     (searched: same directory, {}, {})",
                    canon.display(),
                    paths.include_root.display(),
                    paths.src_root.display()
                ),
            },
            None => {
                out.push_str(line);
                out.push('\n');
            }
        }
    }
    out.push_str(&format!("/* ---- end inlined: {rel} ---- */\n"));
}

/// If `line` is a `#include "quoted/path"` directive, return the quoted
/// path. Returns `None` for angle-bracket includes and every other line
/// (including other preprocessor directives), which are left untouched by
/// the caller.
fn parse_quoted_include(line: &str) -> Option<String> {
    let trimmed = line.trim_start();
    let rest = trimmed.strip_prefix("#include")?;
    let rest = rest.trim_start();
    let rest = rest.strip_prefix('"')?;
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

/// Resolve a quoted `#include` path the way a real compiler with
/// `-I<include_root>` would: first relative to the including file's own
/// directory, then relative to each known local search root.
fn resolve_include(inc: &str, current_dir: &Path, paths: &Paths) -> Option<PathBuf> {
    for base in [current_dir, paths.include_root.as_path(), paths.src_root.as_path()] {
        let candidate = base.join(inc);
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}

/// Strip the generated-file header (the stamp line + the blank line after
/// it) before diffing, since the commit SHA in it legitimately changes
/// every commit.
fn strip_stamp(content: &str) -> &str {
    match content.split_once("\n\n") {
        Some((_, rest)) => rest,
        None => content,
    }
}

/// Regenerate both amalgamations in-memory and compare them (modulo the SHA
/// stamp line) against the checked-in vendor/ copies. Returns `true` if
/// everything matches.
fn check(paths: &Paths) -> bool {
    let mut ok = true;

    let fresh_c = amalgamate_c(paths);
    let checked_in_c_path = paths.vendor_dir.join(AMALGAMATED_C_NAME);
    let checked_in_c = fs::read_to_string(&checked_in_c_path).unwrap_or_default();
    if strip_stamp(&fresh_c) != strip_stamp(&checked_in_c) {
        eprintln!(
            "DRIFT: {} does not match a fresh amalgamation of src/static.c and its local includes.\n\
             Regenerate with: cargo run -p xtask -- amalgamate-c",
            checked_in_c_path.display()
        );
        ok = false;
    }

    let fresh_h = amalgamate_h(paths);
    let checked_in_h_path = paths.vendor_dir.join(AMALGAMATED_H_NAME);
    let checked_in_h = fs::read_to_string(&checked_in_h_path).unwrap_or_default();
    if strip_stamp(&fresh_h) != strip_stamp(&checked_in_h) {
        eprintln!(
            "DRIFT: {} does not match a fresh amalgamation of the public headers.\n\
             Regenerate with: cargo run -p xtask -- amalgamate-h",
            checked_in_h_path.display()
        );
        ok = false;
    }

    for name in ANGLE_BRACKET_SUPPORT_HEADERS {
        let fresh = fs::read_to_string(paths.include_root.join(name)).unwrap_or_default();
        let checked_in_path = paths.vendor_dir.join(name);
        let checked_in = fs::read_to_string(&checked_in_path).unwrap_or_default();
        if fresh != checked_in {
            eprintln!(
                "DRIFT: {} does not match include/{name}.\n\
                 Regenerate with: cargo run -p xtask -- amalgamate-c",
                checked_in_path.display()
            );
            ok = false;
        }
    }

    ok
}
