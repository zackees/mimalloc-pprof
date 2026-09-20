#!/usr/bin/env python3
"""Build the four COMPILED configurations of the pprof-tax matrix (issue #187, phase 6D).

The pprof-tax panel answers one question with hard numbers: what does compiling the
profiler cost, holding everything else fixed? "Everything else fixed" only means
something if the four builds below are provably the same build with exactly one knob
changed -- so this script pins every other input (source, compiler, linker, CMake
options) and records a cryptographically identified manifest a Rust validator can
check byte-for-byte, rather than trusting that four `cmake` invocations typed by hand
stayed in lock-step.

    upstream-baseline               microsoft/mimalloc at the lockfile pin, no MI_PPROF
    fork-pprof-off                  this tree, MI_PPROF=OFF
    fork-pprof-on                   this tree, MI_PPROF=ON (implicitly adds
                                     -fno-omit-frame-pointer, like the profiler build)
    fork-pprof-off-frame-pointers   this tree, MI_PPROF=OFF, the same frame-pointer
                                     flag forced by hand -- isolates "the profiler" cost
                                     from "frame pointers" cost

Each configuration is: a fresh `cmake -S ... -B ... -G Ninja` configure plus
`cmake --build`, a `benchmark-child` Rust binary cargo-built and linked against the
resulting static library, and an identity probe (`--pprof-tax-identity`) that proves
the running binary really is the library this script just built and hashed.

`check_equivalence` then fails loudly, before anything downstream trusts the numbers,
if any pair of configurations differs anywhere it should not: a second fork commit
sneaking into one build directory, a stale compiler on PATH, an accidental extra
CMake flag. The Rust side re-derives the same checks from the manifest and remains
authoritative; this function exists so a local `--selftest` run catches the same
mistakes without a full compile.
"""

from __future__ import annotations

# `build_benchmark_allocators` is a sibling script in ci/, not an installed package;
# it is importable because this file's own directory is on sys.path when run as
# `python3 ci/build_pprof_tax_configurations.py`, and because pyproject's
# `pythonpath = ["ci"]` puts it there under pytest. No sys.path surgery.
# ruff: noqa: I001

import argparse
import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import build_benchmark_allocators as allocators

#: The Rust pprof-tax validator (rust/benchmark-suite) rejects any manifest whose
#: upstream-baseline configuration was not built from exactly this commit -- the same
#: pin CLAUDE.md records as the v3 overlay base.
UPSTREAM_COMMIT = "6def7be9458fb8a97b8323af3fb0b0ae04387065"

#: Canonical order: every list/dict this script produces that is keyed or indexed by
#: configuration walks this tuple, so the manifest's `compiled_configurations` array
#: is deterministic run to run.
COMPILED_CONFIGURATION_IDS = (
    "upstream-baseline",
    "fork-pprof-off",
    "fork-pprof-on",
    "fork-pprof-off-frame-pointers",
)

#: The CMake cache keys `check_equivalence` compares across configurations. Anything
#: not in this list is allowed to differ freely (build directory paths, timestamps,
#: generator-internal bookkeeping); anything in it must match except for the one
#: intended knob each pair of configurations isolates.
EQUIVALENCE_CACHE_KEYS = (
    "CMAKE_AR",
    "CMAKE_BUILD_TYPE",
    "CMAKE_C_COMPILER",
    "CMAKE_C_FLAGS",
    "CMAKE_C_FLAGS_RELEASE",
    "CMAKE_EXE_LINKER_FLAGS",
    "CMAKE_INTERPROCEDURAL_OPTIMIZATION",
    "CMAKE_STATIC_LINKER_FLAGS",
    "MI_BUILD_SHARED",
    "MI_BUILD_STATIC",
    "MI_BUILD_TESTS",
    "MI_DEBUG_FULL",
    "MI_DHAT",
    # #414: both became opt-in CMake options; projecting them keeps a silent drift in
    # either one out of the tax measurement (upstream's cache has neither, which is why
    # they join MI_PPROF/MI_DHAT in the upstream-vs-fork allowance below).
    "MI_DIAGNOSTICS",
    "MI_MEMEVT",
    "MI_OPT_ARCH",
    "MI_OPT_SIMD",
    "MI_OVERRIDE",
    "MI_PPROF",
    "MI_SECURE",
    "MI_TRACK_ASAN",
    "MI_TRACK_VALGRIND",
)

COMMON_CMAKE_ARGS: tuple[str, ...] = (
    "-DCMAKE_BUILD_TYPE=Release",
    "-DMI_BUILD_STATIC=ON",
    "-DMI_BUILD_SHARED=OFF",
    "-DMI_BUILD_TESTS=OFF",
    "-DMI_OPT_ARCH=OFF",
    "-DMI_OPT_SIMD=ON",
)

#: Per-configuration CMake arguments appended after COMMON_CMAKE_ARGS and
#: -DCMAKE_C_COMPILER=<resolved cc>. upstream-baseline carries no -DMI_PPROF at all --
#: upstream has no such option, and passing an unknown -D is silently accepted by
#: CMake, which would hide a typo rather than fail loudly.
CONFIGURATION_EXTRA_CMAKE_ARGS: dict[str, tuple[str, ...]] = {
    "upstream-baseline": ("-DCMAKE_C_FLAGS_RELEASE=-O3",),
    "fork-pprof-off": ("-DMI_PPROF=OFF", "-DCMAKE_C_FLAGS_RELEASE=-O3"),
    "fork-pprof-on": ("-DMI_PPROF=ON", "-DCMAKE_C_FLAGS_RELEASE=-O3"),
    "fork-pprof-off-frame-pointers": (
        "-DMI_PPROF=OFF",
        "-DCMAKE_C_FLAGS_RELEASE=-O3 -fno-omit-frame-pointer",
    ),
}

CONFIGURATION_ALLOCATOR_ID: dict[str, str] = {
    "upstream-baseline": "upstream-mimalloc",
    "fork-pprof-off": "mimalloc-pprof",
    "fork-pprof-on": "mimalloc-pprof",
    "fork-pprof-off-frame-pointers": "mimalloc-pprof",
}

CONFIGURATION_PPROF_COMPILED: dict[str, bool] = {
    "upstream-baseline": False,
    "fork-pprof-off": False,
    "fork-pprof-on": True,
    "fork-pprof-off-frame-pointers": False,
}

#: What put -fno-omit-frame-pointer (or nothing) into CMAKE_C_FLAGS_RELEASE, named so
#: the manifest can distinguish "the profiler build's own CMakeLists.txt did this" from
#: "this script forced the same flag by hand for an apples-to-apples comparison".
CONFIGURATION_FRAME_POINTER_POLICY: dict[str, str] = {
    "upstream-baseline": "omitted",
    "fork-pprof-off": "omitted",
    "fork-pprof-on": "cmake-mi-pprof-implicit",
    "fork-pprof-off-frame-pointers": "forced-flag",
}

#: Environment variables recorded verbatim (value or null) in the manifest's
#: "environment" section -- the inputs a reproduction needs to know were pinned versus
#: left to the ambient shell.
RECORDED_ENVIRONMENT_NAMES = (
    "CC",
    "CXX",
    "AR",
    "CFLAGS",
    "LDFLAGS",
    "RUSTFLAGS",
    "SOURCE_DATE_EPOCH",
)

IDENTITY_PROBE_KEYS = frozenset(
    {
        "configuration_id",
        "allocator_id",
        "allocator_version",
        "source_sha",
        "library_sha256",
        "executable_sha256",
        "pprof_compiled",
        "pprof_enabled",
    }
)


class BuildError(RuntimeError):
    """The pprof-tax build cannot proceed, or its result violates the manifest
    contract the Rust validator (rust/benchmark-suite) checks independently."""


class EquivalenceError(BuildError):
    """Two or more compiled configurations diverge outside the one knob they are
    meant to isolate."""


def cmake_arguments(
    compiled_id: str, source_dir: Path, build_dir: Path, c_compiler: str
) -> list[str]:
    """The exact `cmake` configure argv this script runs for `compiled_id`."""
    extra = CONFIGURATION_EXTRA_CMAKE_ARGS.get(compiled_id)
    if extra is None:
        raise BuildError(f"unknown pprof-tax configuration id: {compiled_id!r}")
    return [
        "cmake",
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        "-G",
        "Ninja",
        *COMMON_CMAKE_ARGS,
        f"-DCMAKE_C_COMPILER={c_compiler}",
        *extra,
    ]


def parse_cmake_cache(text: str) -> dict[str, str]:
    """Parse `KEY:TYPE=VALUE` lines from a CMakeCache.txt, dropping comments and
    blanks. `:INTERNAL` entries are kept -- projection, not parsing, decides which
    keys matter."""
    cache: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        key_and_type, separator, value = line.partition("=")
        if not separator:
            continue
        key, _colon, _type = key_and_type.partition(":")
        if not key:
            continue
        cache[key] = value
    return cache


def project_cache(cache: Mapping[str, str], compiled_id: str) -> dict[str, str | None]:
    """Project a parsed CMakeCache.txt onto EQUIVALENCE_CACHE_KEYS, `None` for any
    missing key. upstream-baseline's MI_PPROF/MI_DHAT are forced to `None` even if a
    stale cache entry happens to carry one -- those options do not exist on upstream's
    CMakeLists.txt, so a value there could only be leftover cache noise."""
    projected: dict[str, str | None] = {key: cache.get(key) for key in EQUIVALENCE_CACHE_KEYS}
    if compiled_id == "upstream-baseline":
        projected["MI_PPROF"] = None
        projected["MI_DHAT"] = None
    return projected


def _require_cache(entry: Mapping[str, object], compiled_id: str) -> Mapping[str, object]:
    cache = entry.get("cmake_cache")
    if not isinstance(cache, dict):
        raise EquivalenceError(f"{compiled_id} is missing its cmake_cache projection")
    return cast(Mapping[str, object], cache)


def _cache_diff(cache_a: Mapping[str, object], cache_b: Mapping[str, object]) -> set[str]:
    return {key for key in EQUIVALENCE_CACHE_KEYS if cache_a.get(key) != cache_b.get(key)}


def _check_cache_diff(
    label_a: str,
    label_b: str,
    cache_a: Mapping[str, object],
    cache_b: Mapping[str, object],
    allowed_keys: set[str],
) -> None:
    unexpected = _cache_diff(cache_a, cache_b) - allowed_keys
    if unexpected:
        raise EquivalenceError(
            f"{label_a} vs {label_b} cache projections differ outside {sorted(allowed_keys)}: "
            f"{sorted(unexpected)}"
        )


def _check_frame_pointer_flag(
    fp_cache: Mapping[str, object], off_cache: Mapping[str, object]
) -> None:
    """fork-pprof-off-frame-pointers must equal fork-pprof-off everywhere except
    CMAKE_C_FLAGS_RELEASE, and there it must equal fork-pprof-off's tokens plus
    exactly one trailing -fno-omit-frame-pointer -- not a flag dropped, doubled, or
    reordered into a value that happens to compare unequal for an unrelated reason."""
    unexpected = _cache_diff(fp_cache, off_cache) - {"CMAKE_C_FLAGS_RELEASE"}
    if unexpected:
        raise EquivalenceError(
            "fork-pprof-off-frame-pointers vs fork-pprof-off cache projections differ outside "
            f"CMAKE_C_FLAGS_RELEASE: {sorted(unexpected)}"
        )
    off_flags = off_cache.get("CMAKE_C_FLAGS_RELEASE")
    fp_flags = fp_cache.get("CMAKE_C_FLAGS_RELEASE")
    if not isinstance(off_flags, str) or not isinstance(fp_flags, str):
        raise EquivalenceError(
            "fork-pprof-off-frame-pointers and fork-pprof-off must both record "
            "CMAKE_C_FLAGS_RELEASE as a string"
        )
    expected_tokens = [*off_flags.split(), "-fno-omit-frame-pointer"]
    if fp_flags.split() != expected_tokens:
        raise EquivalenceError(
            "fork-pprof-off-frame-pointers CMAKE_C_FLAGS_RELEASE must equal fork-pprof-off's "
            f"tokens plus exactly one -fno-omit-frame-pointer: off={off_flags.split()!r} "
            f"fp={fp_flags.split()!r}"
        )


def check_equivalence(configurations: Sequence[Mapping[str, object]]) -> None:
    """Fail loudly if any pair of the four compiled configurations diverges outside
    the one knob it exists to isolate. Mirrors the Rust validator, which remains
    authoritative; this is the same check, runnable offline against a manifest that
    has not been built yet."""
    by_id: dict[str, Mapping[str, object]] = {}
    for entry in configurations:
        compiled_id = entry.get("compiled_configuration_id")
        if not isinstance(compiled_id, str):
            raise EquivalenceError("every configuration needs a compiled_configuration_id")
        by_id[compiled_id] = entry
    missing = [cid for cid in COMPILED_CONFIGURATION_IDS if cid not in by_id]
    if missing:
        raise EquivalenceError(f"check_equivalence is missing configurations: {missing}")

    fork_ids = ("fork-pprof-off", "fork-pprof-on", "fork-pprof-off-frame-pointers")
    fork_source_shas = {by_id[cid].get("source_sha") for cid in fork_ids}
    if len(fork_source_shas) != 1:
        raise EquivalenceError(f"fork configurations do not share a source sha: {fork_source_shas}")

    compiler_identities = {
        by_id[cid].get("c_compiler_identity") for cid in COMPILED_CONFIGURATION_IDS
    }
    if len(compiler_identities) != 1:
        raise EquivalenceError(
            f"compiler identities differ across configurations: {compiler_identities}"
        )
    linker_identities = {by_id[cid].get("linker_identity") for cid in COMPILED_CONFIGURATION_IDS}
    if len(linker_identities) != 1:
        raise EquivalenceError(
            f"linker identities differ across configurations: {linker_identities}"
        )

    off_cache = _require_cache(by_id["fork-pprof-off"], "fork-pprof-off")
    on_cache = _require_cache(by_id["fork-pprof-on"], "fork-pprof-on")
    _check_cache_diff("fork-pprof-on", "fork-pprof-off", on_cache, off_cache, {"MI_PPROF"})

    fp_cache = _require_cache(
        by_id["fork-pprof-off-frame-pointers"], "fork-pprof-off-frame-pointers"
    )
    _check_frame_pointer_flag(fp_cache, off_cache)

    upstream_cache = _require_cache(by_id["upstream-baseline"], "upstream-baseline")
    _check_cache_diff(
        "upstream-baseline",
        "fork-pprof-off",
        upstream_cache,
        off_cache,
        {"MI_PPROF", "MI_DHAT", "MI_MEMEVT", "MI_DIAGNOSTICS"},
    )


def validate_identity_probe(probe: object, expected: Mapping[str, object]) -> None:
    """Check `benchmark-child --pprof-tax-identity`'s JSON against what this script
    itself just built and hashed. `expected` carries configuration_id, allocator_id,
    allocator_version, source_sha, library_sha256, executable_sha256 and
    pprof_compiled; pprof_enabled must always read back False -- the runtime profiler
    stays off for every configuration, including the one built with it compiled in."""
    if not isinstance(probe, dict):
        raise BuildError("pprof-tax identity probe did not emit a JSON object")
    probe_map = cast(Mapping[str, object], probe)
    keys = frozenset(probe_map.keys())
    if keys != IDENTITY_PROBE_KEYS:
        raise BuildError(
            f"pprof-tax identity probe keys must be exactly {sorted(IDENTITY_PROBE_KEYS)}, "
            f"got {sorted(keys)}"
        )
    for field in (
        "configuration_id",
        "allocator_id",
        "allocator_version",
        "source_sha",
        "library_sha256",
        "executable_sha256",
        "pprof_compiled",
    ):
        if probe_map.get(field) != expected.get(field):
            raise BuildError(
                f"pprof-tax identity probe {field} mismatch: expected {expected.get(field)!r}, "
                f"got {probe_map.get(field)!r}"
            )
    if probe_map.get("pprof_enabled") is not False:
        raise BuildError(
            "pprof-tax identity probe pprof_enabled must be False, got "
            f"{probe_map.get('pprof_enabled')!r}"
        )


# ---------------------------------------------------------------------------
# Building. Everything below actually runs cmake/cargo/the compiled child and is not
# exercised by the offline test suite, which only calls the pure functions above.
# ---------------------------------------------------------------------------


def resolve_c_compiler() -> str:
    environment = allocators.command_environment()
    requested = environment.get("CC", "cc")
    resolved = shutil.which(requested, path=environment.get("PATH", os.defpath))
    if resolved is None:
        raise BuildError(f"cannot resolve C compiler {requested!r} on PATH")
    return str(Path(resolved).resolve())


def _write_link_manifest(compiled_id: str, library: Path, pprof_tax_root: Path) -> Path:
    manifest_dir = pprof_tax_root / "link-manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / f"{compiled_id}.txt"
    manifest.write_text(f"archive\t{library.resolve()}\n", encoding="utf-8")
    return manifest.resolve()


def _build_child(
    compiled_id: str,
    allocator_id: str,
    allocator_version: str,
    source_sha: str,
    source_dir: Path,
    build_dir: Path,
    library: Path,
    library_sha256: str,
    pprof_tax_root: Path,
    logs: Path,
) -> Path:
    """Cargo-build `benchmark-child` linked against this configuration's static
    library. Mirrors `build_benchmark_allocators.build_child`'s soldr resolution and
    logging, keyed by compiled configuration id rather than allocator id (three of
    the four configurations share one allocator id) and with the extra
    BENCH_PPROF_TAX_CONFIGURATION the pprof-tax identity probe reads back."""
    link_manifest = _write_link_manifest(compiled_id, library, pprof_tax_root)
    target_dir = (pprof_tax_root / "cargo-target" / compiled_id).resolve()
    cargo_args = [
        "cargo",
        "build",
        "--manifest-path",
        str(allocators.repository_root() / "rust" / "Cargo.toml"),
        "-p",
        "benchmark-suite",
        "--bin",
        "benchmark-child",
        "--release",
        "--locked",
    ]
    environment = allocators.command_environment()
    environment.update(
        {
            "CARGO_TARGET_DIR": str(target_dir),
            "BENCH_ALLOCATOR_ID": allocator_id,
            "BENCH_ALLOCATOR_VERSION": allocator_version,
            "BENCH_ALLOCATOR_SOURCE_SHA": source_sha,
            "BENCH_ALLOCATOR_LIBRARY": str(library.resolve()),
            "BENCH_ALLOCATOR_LIBRARY_SHA256": library_sha256,
            "BENCH_ALLOCATOR_INCLUDE_DIRS": os.pathsep.join(
                str(path.resolve())
                for path in allocators.adapter_include_directories(
                    allocator_id, source_dir, build_dir
                )
            ),
            "BENCH_ALLOCATOR_LINK_MANIFEST": str(link_manifest),
            "BENCH_PPROF_TAX_CONFIGURATION": compiled_id,
        }
    )
    resolved_soldr = shutil.which("soldr", path=environment.get("PATH", os.defpath))
    if resolved_soldr is not None:
        command = [resolved_soldr, *cargo_args]
        display = " ".join(command)
    else:
        quoted = " ".join(shlex.quote(arg) for arg in cargo_args)
        command = ["bash", "-c", f"soldr {quoted}"]
        display = "$ " + " ".join(command)
    with (logs / f"{compiled_id}-child.log").open("w", encoding="utf-8") as log:
        log.write(display + "\n")
        log.flush()
        subprocess.run(
            command,
            cwd=allocators.repository_root() / "rust",
            env=environment,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    built = target_dir / "release" / "benchmark-child"
    if not built.is_file():
        raise BuildError(f"Cargo did not produce {compiled_id} benchmark child at {built}")
    output_dir = pprof_tax_root / "children" / compiled_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "benchmark-child"
    shutil.copyfile(built, output)
    output.chmod(0o755)
    return output


def _run_identity_probe(
    compiled_id: str, binary: Path, executable_sha256: str
) -> dict[str, object]:
    environment = allocators.benchmark_runtime_environment()
    environment["BENCH_CHILD_BINARY_SHA256"] = executable_sha256
    result = subprocess.run(
        [str(binary), "--pprof-tax-identity"],
        cwd=binary.parent,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise BuildError(f"{compiled_id} identity probe did not emit JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise BuildError(f"{compiled_id} identity probe did not emit a JSON object")
    return cast(dict[str, object], parsed)


def build_configuration(
    compiled_id: str,
    source_dir: Path,
    pprof_tax_root: Path,
    jobs: int,
    c_compiler: str,
    c_compiler_identity: str,
    linker_identity: str,
    allocator_id: str,
    allocator_version: str,
    source_sha: str,
    logs: Path,
) -> dict[str, object]:
    """Configure, build, cargo-link and identity-probe one of the four configurations,
    into a fresh build directory deleted and recreated before every build."""
    build_dir = pprof_tax_root / "build" / compiled_id
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    configure_command = cmake_arguments(compiled_id, source_dir, build_dir, c_compiler)
    build_command = ["cmake", "--build", str(build_dir), "--parallel", str(jobs)]
    with (logs / f"{compiled_id}.log").open("w", encoding="utf-8") as log:
        for command in (configure_command, build_command):
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            subprocess.run(
                command,
                cwd=source_dir,
                env=allocators.command_environment(),
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    library = build_dir / "libmimalloc.a"
    if not library.is_file():
        raise BuildError(f"{compiled_id} did not produce {library}")
    cache = parse_cmake_cache((build_dir / "CMakeCache.txt").read_text(encoding="utf-8"))
    projected_cache = project_cache(cache, compiled_id)
    library_sha256 = allocators.sha256_file(library)

    child = _build_child(
        compiled_id,
        allocator_id,
        allocator_version,
        source_sha,
        source_dir,
        build_dir,
        library,
        library_sha256,
        pprof_tax_root,
        logs,
    )
    executable_sha256 = allocators.sha256_file(child)

    pprof_compiled = CONFIGURATION_PPROF_COMPILED[compiled_id]
    expected_probe: dict[str, object] = {
        "configuration_id": compiled_id,
        "allocator_id": allocator_id,
        "allocator_version": allocator_version,
        "source_sha": source_sha,
        "library_sha256": library_sha256,
        "executable_sha256": executable_sha256,
        "pprof_compiled": pprof_compiled,
    }
    probe = _run_identity_probe(compiled_id, child, executable_sha256)
    validate_identity_probe(probe, expected_probe)

    return {
        "compiled_configuration_id": compiled_id,
        "allocator_id": allocator_id,
        "allocator_version": allocator_version,
        "source_sha": source_sha,
        "pprof_compiled": pprof_compiled,
        "frame_pointer_policy": CONFIGURATION_FRAME_POINTER_POLICY[compiled_id],
        "cmake_arguments": configure_command,
        "cmake_cache": projected_cache,
        "c_compiler_identity": c_compiler_identity,
        "linker_identity": linker_identity,
        "static_library_sha256": library_sha256,
        "executable_path": str(child.resolve()),
        "executable_sha256": executable_sha256,
        "identity_probe": probe,
    }


def build_pprof_tax_manifest(lockfile: Path, build_root: Path, jobs: int) -> dict[str, object]:
    pprof_tax_root = build_root.resolve() / "pprof-tax"
    logs = pprof_tax_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    records = allocators.read_lockfile(lockfile)
    by_id = {allocators.require_string(r.get("id"), "allocator.id"): r for r in records}
    upstream_record = by_id["upstream-mimalloc"]
    fork_record = by_id["mimalloc-pprof"]

    upstream_source = allocators.require_mapping(
        upstream_record.get("source"), "upstream-mimalloc.source"
    )
    upstream_commit = allocators.require_string(
        upstream_source.get("commit"), "upstream-mimalloc.source.commit"
    )
    if upstream_commit != UPSTREAM_COMMIT:
        raise BuildError(
            f"upstream-mimalloc lockfile commit is {upstream_commit}, expected {UPSTREAM_COMMIT} "
            "-- the Rust pprof-tax validator rejects any other commit"
        )

    allocators.ensure_checkout_engine_matches_head()
    workflow_commit = allocators.checkout_commit()

    archive_url = allocators.require_string(
        upstream_source.get("archive_url"), "upstream-mimalloc.source.archive_url"
    )
    archive_sha256 = allocators.require_string(
        upstream_source.get("archive_sha256"), "upstream-mimalloc.source.archive_sha256"
    )
    archive = pprof_tax_root / "archives" / allocators.archive_filename(upstream_record)
    allocators.download_archive(archive_url, archive_sha256, archive)
    unpacked = pprof_tax_root / "src" / "upstream"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    allocators.extract_archive(archive, unpacked)
    upstream_source_dir = allocators.first_source_directory(unpacked)
    fork_source_dir = allocators.repository_root()

    source_dirs: dict[str, Path] = {
        "upstream-baseline": upstream_source_dir,
        "fork-pprof-off": fork_source_dir,
        "fork-pprof-on": fork_source_dir,
        "fork-pprof-off-frame-pointers": fork_source_dir,
    }

    upstream_source_sha = allocators.source_commit(upstream_record, workflow_commit)
    fork_source_sha = allocators.source_commit(fork_record, workflow_commit)
    upstream_allocator_version = allocators.adapter_version(upstream_record, workflow_commit)
    fork_allocator_version = allocators.adapter_version(fork_record, workflow_commit)

    c_compiler = resolve_c_compiler()
    c_compiler_identity = allocators.checked_tool_version([c_compiler])
    linker_identity = allocators.checked_tool_version(["ld"])

    configurations: list[dict[str, object]] = []
    for compiled_id in COMPILED_CONFIGURATION_IDS:
        if compiled_id == "upstream-baseline":
            allocator_version, source_sha = upstream_allocator_version, upstream_source_sha
        else:
            allocator_version, source_sha = fork_allocator_version, fork_source_sha
        configurations.append(
            build_configuration(
                compiled_id=compiled_id,
                source_dir=source_dirs[compiled_id],
                pprof_tax_root=pprof_tax_root,
                jobs=jobs,
                c_compiler=c_compiler,
                c_compiler_identity=c_compiler_identity,
                linker_identity=linker_identity,
                allocator_id=CONFIGURATION_ALLOCATOR_ID[compiled_id],
                allocator_version=allocator_version,
                source_sha=source_sha,
                logs=logs,
            )
        )

    check_equivalence(configurations)

    environment_section: dict[str, str | None] = {
        name: os.environ.get(name) for name in RECORDED_ENVIRONMENT_NAMES
    }

    return {
        "manifest_schema_version": "pprof-tax-manifest-v1",
        "target": "x86_64-unknown-linux-gnu",
        "fork_source_sha": fork_source_sha,
        "upstream_source_sha": upstream_source_sha,
        "upstream_archive_sha256": archive_sha256,
        "toolchain": {
            "c_compiler": c_compiler,
            "c_compiler_identity": c_compiler_identity,
            "linker_identity": linker_identity,
            "cmake": allocators.checked_tool_version(["cmake"]),
            "ninja": allocators.checked_tool_version(["ninja"]),
            "rustc": allocators.checked_tool_version(["rustc"]),
            "cargo": allocators.checked_tool_version(["cargo"]),
        },
        "environment": environment_section,
        "compiled_configurations": configurations,
    }


# ---------------------------------------------------------------------------
# Offline selftest: the same pure functions the pytest suite exercises, runnable with
# no compiler, no network and no build directory.
# ---------------------------------------------------------------------------


def _selftest_configurations() -> list[dict[str, object]]:
    """A consistent, made-up set of four configurations -- everything matches except
    the one knob each pair is allowed to differ in."""

    def cache_with(**overrides: object) -> dict[str, object]:
        merged: dict[str, object] = dict.fromkeys(EQUIVALENCE_CACHE_KEYS, "SAME")
        merged["CMAKE_C_FLAGS_RELEASE"] = "-O3"
        merged.update(overrides)
        return merged

    configurations: list[dict[str, object]] = []
    configurations.append(
        {
            "compiled_configuration_id": "upstream-baseline",
            "source_sha": "up-sha",
            "c_compiler_identity": "cc (Ubuntu) 13",
            "linker_identity": "GNU ld 2.42",
            "cmake_cache": cache_with(MI_PPROF=None, MI_DHAT=None),
        }
    )
    configurations.append(
        {
            "compiled_configuration_id": "fork-pprof-off",
            "source_sha": "fork-sha",
            "c_compiler_identity": "cc (Ubuntu) 13",
            "linker_identity": "GNU ld 2.42",
            "cmake_cache": cache_with(MI_PPROF="OFF"),
        }
    )
    configurations.append(
        {
            "compiled_configuration_id": "fork-pprof-on",
            "source_sha": "fork-sha",
            "c_compiler_identity": "cc (Ubuntu) 13",
            "linker_identity": "GNU ld 2.42",
            "cmake_cache": cache_with(MI_PPROF="ON"),
        }
    )
    configurations.append(
        {
            "compiled_configuration_id": "fork-pprof-off-frame-pointers",
            "source_sha": "fork-sha",
            "c_compiler_identity": "cc (Ubuntu) 13",
            "linker_identity": "GNU ld 2.42",
            "cmake_cache": cache_with(
                MI_PPROF="OFF", CMAKE_C_FLAGS_RELEASE="-O3 -fno-omit-frame-pointer"
            ),
        }
    )
    return configurations


def selftest() -> int:
    source_dir = Path("/src")
    build_dir = Path("/build/upstream-baseline")

    upstream_args = cmake_arguments("upstream-baseline", source_dir, build_dir, "/usr/bin/cc")
    if any(arg.startswith("-DMI_PPROF=") for arg in upstream_args):
        raise AssertionError(
            "upstream-baseline must not carry -DMI_PPROF: upstream has no such option"
        )

    fp_args = cmake_arguments("fork-pprof-off-frame-pointers", source_dir, build_dir, "/usr/bin/cc")
    if "-DCMAKE_C_FLAGS_RELEASE=-O3 -fno-omit-frame-pointer" not in fp_args:
        raise AssertionError(
            "fork-pprof-off-frame-pointers must force the frame-pointer flag by hand"
        )
    off_args = cmake_arguments("fork-pprof-off", source_dir, build_dir, "/usr/bin/cc")
    if any("-fno-omit-frame-pointer" in arg for arg in off_args):
        raise AssertionError("fork-pprof-off must not carry the frame-pointer flag")

    cache_text = "# a comment\n//another comment\n\nMI_PPROF:BOOL=ON\nMI_OPT_ARCH:INTERNAL=OFF\n"
    cache = parse_cmake_cache(cache_text)
    if cache != {"MI_PPROF": "ON", "MI_OPT_ARCH": "OFF"}:
        raise AssertionError(
            "CMakeCache.txt parsing dropped, kept a comment, or mis-parsed an entry"
        )

    projected = project_cache({**cache, "MI_DHAT": "ON"}, "upstream-baseline")
    if projected["MI_PPROF"] is not None or projected["MI_DHAT"] is not None:
        raise AssertionError("upstream-baseline projection must null MI_PPROF and MI_DHAT")

    consistent = _selftest_configurations()
    check_equivalence(consistent)

    drifted = [dict(entry) for entry in consistent]
    for entry in drifted:
        if entry["compiled_configuration_id"] == "fork-pprof-on":
            entry_cache = cast(dict[str, object], entry["cmake_cache"])
            entry["cmake_cache"] = {**entry_cache, "MI_OPT_ARCH": "ON"}
    try:
        check_equivalence(drifted)
    except EquivalenceError:
        pass
    else:
        raise AssertionError("an MI_OPT_ARCH drift between fork configurations was accepted")

    expected_probe: dict[str, object] = {
        "configuration_id": "fork-pprof-on",
        "allocator_id": "mimalloc-pprof",
        "allocator_version": "deadbeef",
        "source_sha": "deadbeef",
        "library_sha256": "a" * 64,
        "executable_sha256": "b" * 64,
        "pprof_compiled": True,
    }
    good_probe: dict[str, object] = {**expected_probe, "pprof_enabled": False}
    validate_identity_probe(good_probe, expected_probe)
    try:
        validate_identity_probe({**good_probe, "pprof_enabled": True}, expected_probe)
    except BuildError:
        pass
    else:
        raise AssertionError("a probe reporting pprof_enabled=True was accepted")
    try:
        validate_identity_probe({**good_probe, "extra": "unexpected"}, expected_probe)
    except BuildError:
        pass
    else:
        raise AssertionError("a probe with an extra key was accepted")

    records = allocators.read_lockfile(allocators.default_lockfile())
    upstream = next(r for r in records if r["id"] == "upstream-mimalloc")
    upstream_source = allocators.require_mapping(upstream["source"], "upstream-mimalloc.source")
    upstream_commit = upstream_source.get("commit")
    if upstream_commit != UPSTREAM_COMMIT:
        raise AssertionError("lockfile upstream-mimalloc commit drifted from UPSTREAM_COMMIT")

    print(
        "PASS pprof-tax configuration builder selftest: cmake args, cache parsing, "
        "equivalence, identity probe"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lockfile", type=Path, default=allocators.default_lockfile())
    parser.add_argument(
        "--build-root", type=Path, help="download/extract/build root for the pprof-tax matrix"
    )
    parser.add_argument(
        "--jobs", type=int, default=1, help="parallel jobs forwarded to `cmake --build`"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="run offline pure-function checks, no build"
    )
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.jobs < 1:
        parser.error("--jobs must be at least one")
    if args.build_root is None:
        parser.error("--build-root is required unless --selftest is given")
    manifest = build_pprof_tax_manifest(args.lockfile, args.build_root, args.jobs)
    output = args.build_root.resolve() / "pprof-tax" / "pprof-tax-manifest.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    configurations = cast(list[object], manifest["compiled_configurations"])
    print(f"PASS built {len(configurations)} pprof-tax configurations; wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
