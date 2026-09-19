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
    inline_file(
        &paths.src_root.join("static.c"),
        paths,
        &mut visited,
        &mut body,
        PragmaOnce::Strip,
    );
    let header = generated_header(paths, "src/static.c", "amalgamate-c");
    format!("{header}{AMALGAMATED_C_PREAMBLE}{body}")
}

/// Emitted at the top of the amalgamated .c only. Inlining turns every header's
/// `static` helpers into main-file definitions, and clang (which exempts functions
/// defined in headers) then reports each one the TU happens not to call as
/// `-Wunused-function`. That is an artifact of amalgamation: the real per-file
/// builds under CMake keep the warning (fatal on the -Werror lanes), so genuinely
/// dead code is still caught there.
const AMALGAMATED_C_PREAMBLE: &str = "\
#if defined(__clang__) || defined(__GNUC__)
#pragma GCC diagnostic ignored \"-Wunused-function\"
#endif

";

/// Amalgamate the public headers (in this fixed order, sharing one dedup set
/// so the extension headers' own `#include "mimalloc.h"` does not duplicate
/// content already inlined for mimalloc.h) into one self-contained public header.
fn amalgamate_h(paths: &Paths) -> String {
    let mut visited = HashSet::new();
    let mut body = String::new();
    for rel in [
        "mimalloc.h",
        "mimalloc/profile.h",
        "mimalloc/memory-events.h",
        "mimalloc/dhat.h",
    ] {
        inline_file(
            &paths.include_root.join(rel),
            paths,
            &mut visited,
            &mut body,
            PragmaOnce::Keep,
        );
    }
    let header = generated_header(
        paths,
        "the public headers (mimalloc.h, mimalloc/profile.h, mimalloc/memory-events.h, mimalloc/dhat.h)",
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

/// What to do with an inlined header's `#pragma once`.
///
/// The amalgamated .c is compiled as a main file, where the pragma is meaningless
/// and clang warns about each one (`-Wpragma-once-outside-header`). Dropping it is
/// safe: the inliner emits every header once, and each keeps its `#ifndef` guard.
/// The amalgamated .h is itself a header, so it keeps them.
#[derive(Clone, Copy, PartialEq, Eq)]
enum PragmaOnce {
    Keep,
    Strip,
}

/// Recursively inline `path` into `out`, resolving every `#include "..."`
/// line it contains and skipping files already inlined (tracked in
/// `visited` by canonical path). `#include <...>` lines and every
/// preprocessor conditional/definition are passed through verbatim, except
/// `#pragma once` under [`PragmaOnce::Strip`].
fn inline_file(
    path: &Path,
    paths: &Paths,
    visited: &mut HashSet<PathBuf>,
    out: &mut String,
    pragma_once: PragmaOnce,
) {
    let canon = fs::canonicalize(path)
        .unwrap_or_else(|e| panic!("cannot resolve include path {}: {e}", path.display()));
    // Only dedup header files. Headers carry their own #pragma once / #ifndef
    // guard, so re-inlining their full text a second time is harmless (the
    // guard neutralizes the second physical copy at real-compile time) --
    // skipping it here just keeps the amalgamated output smaller.
    //
    // `.c` files must NOT be deduped this way: mimalloc's own prim dispatch
    // relies on the SAME file being `#include`d from multiple *mutually
    // exclusive* `#if`/`#elif`/`#else` branches (e.g. `prim/osx/prim.c`
    // `#include`s `../unix/prim.c` under `#elif defined(__APPLE__)`, and
    // `prim/prim.c` also `#include`s `unix/prim.c` directly under its final
    // `#else` for Linux/BSD/etc.). Since this inliner does not track
    // `#if` state, a global dedup-by-canonical-path would (and did) silently
    // drop the second occurrence's content -- even though the two
    // occurrences sit in branches that can never both be compiled, so only
    // one is ever "real" for a given platform. Dropping either one breaks
    // the platform whose branch lost its content: on Linux the `#else`
    // branch that should define `_mi_prim_alloc`/`_mi_prim_free`/etc. was
    // left completely empty, producing undefined-symbol link errors even
    // though the amalgamated file textually mentions `unix/prim.c` twice.
    let is_header = canon.extension().and_then(|e| e.to_str()) == Some("h");
    let newly_visited = visited.insert(canon.clone());
    if is_header && !newly_visited {
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
                Some(resolved) => inline_file(&resolved, paths, visited, out, pragma_once),
                None => panic!(
                    "could not resolve #include \"{inc}\" referenced from {} \
                     (searched: same directory, {}, {})",
                    canon.display(),
                    paths.include_root.display(),
                    paths.src_root.display()
                ),
            },
            None if pragma_once == PragmaOnce::Strip && is_pragma_once(line) => {}
            None => {
                out.push_str(line);
                out.push('\n');
            }
        }
    }
    out.push_str(&format!("/* ---- end inlined: {rel} ---- */\n"));
}

/// True for a `#pragma once` directive (any spacing, optional trailing comment).
fn is_pragma_once(line: &str) -> bool {
    let Some(rest) = line.trim_start().strip_prefix('#') else {
        return false;
    };
    let Some(rest) = rest.trim_start().strip_prefix("pragma") else {
        return false;
    };
    let mut words = rest.split_whitespace();
    words.next() == Some("once")
        && words
            .next()
            .is_none_or(|w| w.starts_with("//") || w.starts_with("/*"))
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
    for base in [
        current_dir,
        paths.include_root.as_path(),
        paths.src_root.as_path(),
    ] {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn pragma_once_lines(content: &str) -> Vec<usize> {
        content
            .lines()
            .enumerate()
            .filter(|(_, line)| is_pragma_once(line))
            .map(|(i, _)| i + 1)
            .collect()
    }

    /// The amalgamated .c is compiled as a main file, where every inlined header's
    /// `#pragma once` draws clang's -Wpragma-once-outside-header (13 warnings in a
    /// downstream llvm-ld build). The inliner already emits each header once, and
    /// the headers keep their #ifndef guards, so the pragma must not survive.
    #[test]
    // These read the source tree through the compile-time CARGO_MANIFEST_DIR.
    // windows-bundles.yml cross-builds the test binaries on Linux and runs them on a
    // Windows runner where that path does not exist; the generated text is
    // platform-independent, and the Linux rust-native rows run these tests.
    #[cfg_attr(
        windows,
        ignore = "needs the source tree at the build-time path; run on Linux"
    )]
    fn amalgamated_c_has_no_pragma_once() {
        let paths = Paths::discover();
        let fresh = pragma_once_lines(&amalgamate_c(&paths));
        assert!(
            fresh.is_empty(),
            "fresh amalgamation has #pragma once at lines {fresh:?}"
        );
        let vendored = fs::read_to_string(paths.vendor_dir.join(AMALGAMATED_C_NAME))
            .expect("vendored amalgamated .c exists");
        let vendored = pragma_once_lines(&vendored);
        assert!(
            vendored.is_empty(),
            "vendored {AMALGAMATED_C_NAME} has #pragma once at lines {vendored:?}; \
             regenerate with: cargo run -p xtask -- amalgamate-c"
        );
    }

    #[test]
    // These read the source tree through the compile-time CARGO_MANIFEST_DIR.
    // windows-bundles.yml cross-builds the test binaries on Linux and runs them on a
    // Windows runner where that path does not exist; the generated text is
    // platform-independent, and the Linux rust-native rows run these tests.
    #[cfg_attr(
        windows,
        ignore = "needs the source tree at the build-time path; run on Linux"
    )]
    fn amalgamated_c_suppresses_only_amalgamation_artifacts() {
        let paths = Paths::discover();
        let fresh = amalgamate_c(&paths);
        let body = strip_stamp(&fresh);
        assert!(
            body.starts_with(AMALGAMATED_C_PREAMBLE),
            "amalgamated .c must open with the -Wunused-function preamble"
        );
        assert!(
            !strip_stamp(&amalgamate_h(&paths)).contains("diagnostic ignored"),
            "the public amalgamated header must not change consumers' diagnostics"
        );
    }

    #[test]
    fn pragma_once_detection() {
        assert!(is_pragma_once("#pragma once"));
        assert!(is_pragma_once("  #  pragma   once  // guard"));
        assert!(!is_pragma_once("#pragma comment(lib, \"advapi32\")"));
        assert!(!is_pragma_once("// #pragma once"));
    }
}
