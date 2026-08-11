#!/usr/bin/env python3
"""Prepare the pinned native allocator sources used by ``benchmark-suite``.

This intentionally contains only deterministic source acquisition and native-library
build work.  Timed workloads belong in the benchmark child process, never here.
Archives are checksummed before they are opened, and extraction rejects every link
and every path which could escape the requested destination.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

EXPECTED_IDS = ("tcmalloc", "jemalloc", "upstream-mimalloc", "mimalloc-pprof")
SHA256_LENGTH = 64
GIT_SHA_LENGTH = 40
CHUNK_SIZE = 1024 * 1024
ENVIRONMENT_ALLOWLIST = (
    "AR",
    "CARGO_HOME",
    "CC",
    "CFLAGS",
    "CXX",
    "CXXFLAGS",
    "HOME",
    "LD",
    "MAKEFLAGS",
    "PATH",
    "RANLIB",
    "RUSTUP_HOME",
    "SOURCE_DATE_EPOCH",
    "TEMP",
    "TMP",
    "TMPDIR",
)
ADAPTER_SYMBOLS = (
    "bench_allocator_id",
    "bench_allocator_version",
    "bench_alloc",
    "bench_calloc",
    "bench_realloc",
    "bench_aligned_alloc",
    "bench_free",
    "bench_usable_size",
)
COMPETITOR_SYMBOLS = {
    "tcmalloc": "TCMallocInternalMalloc",
    "jemalloc": "je_malloc",
    "upstream-mimalloc": "mi_malloc",
    "mimalloc-pprof": "mi_malloc",
}


class LockfileError(ValueError):
    """The checked-in source contract is malformed or no longer immutable."""


class ArchiveError(RuntimeError):
    """An archive did not match its locked bytes or is unsafe to extract."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_lockfile() -> Path:
    return repository_root() / "rust" / "benchmark-suite" / "allocators" / "allocator-lock.json"


def source_patch_directory() -> Path:
    return default_lockfile().parent / "patches"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while True:
            block = input_file.read(CHUNK_SIZE)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def is_hex_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise LockfileError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LockfileError(f"{label} must be a non-empty string")
    return value


def require_string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise LockfileError(f"{label} must be a non-empty list of non-empty strings")
    items = cast(list[object], value)
    return [require_string(item, f"{label} item") for item in items]


def require_commands(value: object, label: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise LockfileError(f"{label} must be a non-empty command list")
    commands: list[list[str]] = []
    for index, command in enumerate(cast(list[object], value)):
        commands.append(require_string_list(command, f"{label}[{index}]"))
    return commands


def validate_source(allocator_id: str, pin: str, source: Mapping[str, object]) -> None:
    kind = require_string(source.get("kind"), f"{allocator_id}.source.kind")
    repository = require_string(
        source.get("canonical_repository"), f"{allocator_id}.source.canonical_repository"
    )
    parsed = urllib.parse.urlparse(repository)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise LockfileError(
            f"{allocator_id}.source.canonical_repository must be a canonical HTTPS URL"
        )
    commit = require_string(source.get("commit"), f"{allocator_id}.source.commit")

    if kind == "checkout":
        if (
            allocator_id != "mimalloc-pprof"
            or pin != "workflow-source"
            or commit != "workflow-source"
        ):
            raise LockfileError("only mimalloc-pprof may use the workflow-source checkout pin")
        if "archive_url" in source or "archive_sha256" in source:
            raise LockfileError("workflow-source checkout must not carry a stale archive")
        return

    if kind != "archive":
        raise LockfileError(f"{allocator_id}.source.kind must be archive or checkout")
    if not is_hex_digest(commit, GIT_SHA_LENGTH):
        raise LockfileError(
            f"{allocator_id}.source.commit must be a full lowercase 40-character SHA"
        )
    archive_url = require_string(source.get("archive_url"), f"{allocator_id}.source.archive_url")
    parsed_archive = urllib.parse.urlparse(archive_url)
    if parsed_archive.scheme != "https" or not parsed_archive.netloc or parsed_archive.query:
        raise LockfileError(
            f"{allocator_id}.source.archive_url must be an immutable HTTPS archive URL"
        )
    archive_sha = source.get("archive_sha256")
    if not is_hex_digest(archive_sha, SHA256_LENGTH):
        raise LockfileError(f"{allocator_id}.source.archive_sha256 must be a lowercase SHA-256")
    if commit[:8] not in archive_url:
        raise LockfileError(f"{allocator_id}.source.archive_url must name its source commit")
    if allocator_id == "jemalloc" and pin != f"5.3.1@{commit}":
        raise LockfileError("jemalloc must record release 5.3.1 and its peeled commit")
    if allocator_id == "upstream-mimalloc" and pin != "dev3@bcee5a88":
        raise LockfileError("upstream-mimalloc must remain at dev3@bcee5a88")
    if allocator_id == "tcmalloc" and pin != commit:
        raise LockfileError("tcmalloc pin must equal its immutable commit")


def source_patch_records(
    allocator_id: str, patches: Mapping[str, object]
) -> list[Mapping[str, object]]:
    source = patches.get("source")
    if not isinstance(source, list):
        raise LockfileError(f"{allocator_id}.patches.source must be a list of patch records")
    validated: list[Mapping[str, object]] = []
    for index, patch in enumerate(cast(list[object], source)):
        label = f"{allocator_id}.patches.source[{index}]"
        record = require_mapping(patch, label)
        filename = require_string(record.get("file"), f"{label}.file")
        if (
            filename != PurePosixPath(filename).name
            or filename != PureWindowsPath(filename).name
            or not filename.endswith(".patch")
        ):
            raise LockfileError(f"{label}.file must be one .patch filename, not a path")
        expected_sha256 = record.get("sha256")
        if not is_hex_digest(expected_sha256, SHA256_LENGTH):
            raise LockfileError(f"{label}.sha256 must be a lowercase SHA-256")
        patch_path = source_patch_directory() / filename
        try:
            patch_path.resolve(strict=True).relative_to(
                source_patch_directory().resolve(strict=True)
            )
        except (OSError, ValueError) as error:
            raise LockfileError(
                f"{label}.file is not a checked-in source patch: {filename}"
            ) from error
        if patch_path.is_symlink() or not patch_path.is_file():
            raise LockfileError(f"{label}.file is not a regular file: {filename}")
        actual_sha256 = sha256_file(patch_path)
        if actual_sha256 != expected_sha256:
            raise LockfileError(
                f"{label}.sha256 mismatch for {filename}: expected {expected_sha256}, got {actual_sha256}"
            )
        validated.append(record)
    return validated


def validate_allocator(record: object) -> Mapping[str, object]:
    allocator = require_mapping(record, "allocator")
    allocator_id = require_string(allocator.get("id"), "allocator.id")
    pin = require_string(allocator.get("pin"), f"{allocator_id}.pin")
    source = require_mapping(allocator.get("source"), f"{allocator_id}.source")
    build = require_mapping(allocator.get("build"), f"{allocator_id}.build")
    validate_source(allocator_id, pin, source)
    require_string(build.get("system"), f"{allocator_id}.build.system")
    require_commands(build.get("commands"), f"{allocator_id}.build.commands")
    require_string_list(build.get("flags"), f"{allocator_id}.build.flags")
    if allocator_id == "tcmalloc":
        if build.get("required_tool_version") != "bazel 9.1.0":
            raise LockfileError("tcmalloc.required_tool_version must be bazel 9.1.0")
        if not is_hex_digest(build.get("generated_lock_sha256"), SHA256_LENGTH):
            raise LockfileError("tcmalloc.generated_lock_sha256 must be a lowercase SHA-256")
    elif "required_tool_version" in build or "generated_lock_sha256" in build:
        raise LockfileError(f"{allocator_id} must not carry Bazel-specific generated-lock metadata")
    library = require_string(allocator.get("expected_static_library"), allocator_id)
    if (
        not library.startswith("lib")
        or not library.endswith((".a", ".lo"))
        or "/" in library
        or "\\" in library
    ):
        raise LockfileError(
            f"{allocator_id}.expected_static_library must be one .a or Bazel .lo archive filename"
        )
    require_string(allocator.get("adapter_kind"), f"{allocator_id}.adapter_kind")
    require_string(allocator.get("license"), f"{allocator_id}.license")
    patches = require_mapping(allocator.get("patches"), f"{allocator_id}.patches")
    source_patches = source_patch_records(allocator_id, patches)
    build_patches = patches.get("build")
    if build_patches != []:
        raise LockfileError(f"{allocator_id}.patches.build must be an empty list")
    if source.get("kind") == "checkout" and source_patches:
        raise LockfileError("workflow-source checkout may not be patched by the allocator builder")
    return allocator


def read_lockfile(path: Path) -> list[Mapping[str, object]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LockfileError(f"cannot read lockfile {path}: {error}") from error
    lockfile = require_mapping(parsed, "lockfile")
    if lockfile.get("schema_version") != 1:
        raise LockfileError("lockfile.schema_version must be 1")
    if lockfile.get("target") != "x86_64-unknown-linux-gnu":
        raise LockfileError("lockfile.target must be x86_64-unknown-linux-gnu")
    records = lockfile.get("allocators")
    if not isinstance(records, list):
        raise LockfileError("lockfile.allocators must be a list")
    validated = [validate_allocator(record) for record in cast(list[object], records)]
    ids = tuple(require_string(record.get("id"), "allocator.id") for record in validated)
    if ids != EXPECTED_IDS:
        raise LockfileError(f"lockfile allocator IDs must be exactly {EXPECTED_IDS}, got {ids}")
    return validated


def validate_archive_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = member.name
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise ArchiveError(f"unsafe archive path: {name!r}")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise ArchiveError(f"links and special files are forbidden in benchmark archives: {name!r}")
    if not (member.isdir() or member.isfile()):
        raise ArchiveError(f"unsupported archive member: {name!r}")
    return path


def descendant(destination: Path, relative: PurePosixPath) -> Path:
    root = destination.resolve()
    candidate = root.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ArchiveError(f"archive member escapes destination: {relative}") from error
    return candidate


def extract_archive(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ArchiveError(f"refusing to extract into existing destination: {destination}")
    destination.mkdir(parents=True)
    try:
        with tarfile.open(archive, mode="r:*") as bundle:
            members = bundle.getmembers()
            safe_members = [(member, validate_archive_member(member)) for member in members]
            for member, relative in safe_members:
                output = descendant(destination, relative)
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                input_file = bundle.extractfile(member)
                if input_file is None:
                    raise ArchiveError(f"unable to read archive file: {member.name!r}")
                with input_file, output.open("xb") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=CHUNK_SIZE)
                # Preserve only the executable/non-executable distinction. Build
                # archives commonly contain invoked scripts, while all other mode
                # bits are normalized for a deterministic and non-privileged tree.
                output.chmod(0o755 if member.mode & 0o111 else 0o644)
    except ArchiveError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise ArchiveError(f"cannot safely extract {archive}: {error}") from error


def download_archive(url: str, expected_sha256: str, destination: Path) -> None:
    if destination.exists():
        if sha256_file(destination) == expected_sha256:
            return
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "mimalloc-pprof-benchmark-lock/1"}
        )
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            temporary.open("wb") as output,
        ):
            while True:
                block = response.read(CHUNK_SIZE)
                if not block:
                    break
                output.write(block)
        actual_sha256 = sha256_file(temporary)
        if actual_sha256 != expected_sha256:
            raise ArchiveError(
                f"archive checksum mismatch for {url}: expected {expected_sha256}, got {actual_sha256}"
            )
        temporary.replace(destination)
    except (OSError, urllib.error.URLError) as error:
        raise ArchiveError(f"cannot download {url}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def archive_filename(record: Mapping[str, object]) -> str:
    allocator_id = require_string(record.get("id"), "allocator.id")
    source = require_mapping(record.get("source"), f"{allocator_id}.source")
    commit = require_string(source.get("commit"), f"{allocator_id}.source.commit")
    return f"{allocator_id}-{commit}.tar.gz"


def first_source_directory(destination: Path) -> Path:
    entries = sorted(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise ArchiveError(f"archive must contain exactly one source directory: {destination}")
    return entries[0]


def apply_source_patches(
    record: Mapping[str, object], source_dir: Path, logs: Path
) -> list[dict[str, str]]:
    allocator_id = require_string(record.get("id"), "allocator.id")
    patches = require_mapping(record.get("patches"), f"{allocator_id}.patches")
    applied: list[dict[str, str]] = []
    patch_log = logs / f"{allocator_id}-patches.log"
    patch_records = source_patch_records(allocator_id, patches)
    if not patch_records:
        return applied
    with patch_log.open("w", encoding="utf-8") as log:
        for patch in patch_records:
            filename = require_string(patch.get("file"), f"{allocator_id}.patch.file")
            expected_sha256 = require_string(patch.get("sha256"), f"{allocator_id}.patch.sha256")
            patch_path = source_patch_directory() / filename
            command = ["patch", "--batch", "--forward", "--strip=1", "--input", str(patch_path)]
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            try:
                subprocess.run(
                    command,
                    cwd=source_dir,
                    env=command_environment(),
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            except FileNotFoundError as error:
                raise ArchiveError(
                    "source patches require the standard `patch` executable"
                ) from error
            except subprocess.CalledProcessError as error:
                raise ArchiveError(
                    f"{allocator_id} source patch {filename} did not apply; see {patch_log}"
                ) from error
            applied.append({"file": filename, "sha256": expected_sha256})
    return applied


def prepare_sources(
    records: Iterable[Mapping[str, object]], build_root: Path, logs: Path
) -> tuple[dict[str, Path], dict[str, list[dict[str, str]]], dict[str, str]]:
    sources: dict[str, Path] = {}
    applied_patches: dict[str, list[dict[str, str]]] = {}
    source_tree_sha256s: dict[str, str] = {}
    archive_root = build_root / "archives"
    source_root = build_root / "sources"
    for record in records:
        allocator_id = require_string(record.get("id"), "allocator.id")
        source = require_mapping(record.get("source"), f"{allocator_id}.source")
        if source.get("kind") == "checkout":
            ensure_checkout_engine_matches_head()
            sources[allocator_id] = repository_root()
            applied_patches[allocator_id] = []
            source_tree_sha256s[allocator_id] = checkout_tree_sha256()
            continue
        archive = archive_root / archive_filename(record)
        download_archive(
            require_string(source.get("archive_url"), f"{allocator_id}.source.archive_url"),
            require_string(source.get("archive_sha256"), f"{allocator_id}.source.archive_sha256"),
            archive,
        )
        unpacked = source_root / allocator_id
        if unpacked.exists():
            shutil.rmtree(unpacked)
        extract_archive(archive, unpacked)
        source_dir = first_source_directory(unpacked)
        sources[allocator_id] = source_dir
        applied_patches[allocator_id] = apply_source_patches(record, source_dir, logs)
        source_tree_sha256s[allocator_id] = tree_sha256(source_dir)
    return sources, applied_patches, source_tree_sha256s


def command_environment() -> dict[str, str]:
    environment = {name: os.environ[name] for name in ENVIRONMENT_ALLOWLIST if name in os.environ}
    # The prescribed soldr Docker harness installs verified CMake/Ninja syslibs
    # into its persistent soldr volume rather than the base image. Make those
    # exact binaries visible to the native recipes and retain the resulting PATH
    # in provenance.
    soldr_syslib = Path("/root/.soldr/bin/syslib")
    try:
        is_syslib = soldr_syslib.is_dir()
    except PermissionError:
        is_syslib = False
    if is_syslib:
        syslib_bins = sorted(
            (path for path in soldr_syslib.glob("*/*/*/package/bin") if path.is_dir()),
            reverse=True,
        )
        if syslib_bins:
            inherited = environment.get("PATH", "")
            environment["PATH"] = os.pathsep.join([*(str(path) for path in syslib_bins), inherited])
    return environment


def benchmark_runtime_environment() -> dict[str, str]:
    environment = command_environment()
    environment.update({"MIMALLOC_PROF": "0", "MIMALLOC_MEMORY_EVENTS": "0"})
    return environment


def expand_command(
    command: Sequence[str], source_dir: Path, build_dir: Path, jobs: int
) -> list[str]:
    values = {"source_dir": str(source_dir), "build_dir": str(build_dir), "jobs": str(jobs)}
    try:
        return [part.format(**values) for part in command]
    except KeyError as error:
        raise LockfileError(f"unsupported build-command placeholder: {error}") from error


def tool_version(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            [*command, "--version"],
            env=command_environment(),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError:
        return "unavailable"
    output = (result.stdout or result.stderr).splitlines()
    if result.returncode != 0:
        return f"exit {result.returncode}"
    return output[0] if output else f"exit {result.returncode}"


def split_tool_command(value: str) -> list[str]:
    try:
        command = shlex.split(value, posix=os.name != "nt")
    except ValueError as error:
        raise ArchiveError(f"cannot parse tool command {value!r}: {error}") from error
    if not command:
        raise ArchiveError("tool command must not be empty")
    return command


def checked_tool_version(command: Sequence[str]) -> str:
    version = tool_version(command)
    if version == "unavailable" or version.startswith("exit "):
        raise ArchiveError(f"cannot identify build tool {list(command)!r}: {version}")
    return version


def _tool_version_satisfies(actual: str, required: str) -> bool:
    """Check that actual tool version meets or exceeds the required version.

    Both strings are expected in the form 'toolname major.minor.patch'.
    Only major and minor are compared; patch is advisory.
    """
    if actual == required:
        return True
    try:
        actual_parts = actual.rsplit(" ", 1)
        required_parts = required.rsplit(" ", 1)
        if len(actual_parts) != 2 or len(required_parts) != 2:
            return actual == required
        if actual_parts[0] != required_parts[0]:
            return False
        actual_ver = tuple(int(p) for p in actual_parts[1].split("."))
        required_ver = tuple(int(p) for p in required_parts[1].split("."))
        return actual_ver[:2] >= required_ver[:2]
    except (ValueError, IndexError):
        return actual == required


def compiler_identity(value: str) -> str:
    command = split_tool_command(value)
    effective = checked_tool_version(command)
    if len(command) == 1:
        return f"{shlex.join(command)} :: {effective}"
    wrapper = checked_tool_version(command[:1])
    compiler = checked_tool_version(command[-1:])
    return (
        f"command={shlex.join(command)}; effective={effective}; "
        f"wrapper={wrapper}; compiler={compiler}"
    )


def checkout_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root(),
        env=command_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if not is_hex_digest(commit, GIT_SHA_LENGTH):
        raise ArchiveError(f"workflow source did not resolve to a full Git SHA: {commit!r}")
    return commit


def ensure_checkout_engine_matches_head() -> None:
    command = [
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        "CMakeLists.txt",
        "cmake",
        "include",
        "src",
    ]
    result = subprocess.run(
        command,
        cwd=repository_root(),
        env=command_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    dirty = result.stdout.strip()
    if dirty:
        raise ArchiveError(
            "workflow allocator C-engine inputs must match HEAD before benchmarking:\n" + dirty
        )


def checkout_tree_sha256() -> str:
    result = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=repository_root(),
        env=command_environment(),
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def source_commit(record: Mapping[str, object], workflow_commit: str) -> str:
    allocator_id = require_string(record.get("id"), "allocator.id")
    source = require_mapping(record.get("source"), f"{allocator_id}.source")
    commit = require_string(source.get("commit"), f"{allocator_id}.source.commit")
    return workflow_commit if commit == "workflow-source" else commit


def adapter_version(record: Mapping[str, object], workflow_commit: str) -> str:
    pin = require_string(record.get("pin"), "allocator.pin")
    return workflow_commit if pin == "workflow-source" else pin


def bazel_execution_root(source_dir: Path, build_dir: Path) -> Path:
    result = subprocess.run(
        ["bazel", f"--output_user_root={build_dir / 'bazel'}", "info", "execution_root"],
        cwd=source_dir,
        env=command_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    root = Path(result.stdout.strip()).resolve()
    if not root.is_dir():
        raise ArchiveError(f"Bazel execution root does not exist: {root}")
    return root


def adapter_include_directories(allocator_id: str, source_dir: Path, build_dir: Path) -> list[Path]:
    if allocator_id == "tcmalloc":
        directories = [
            source_dir,
            bazel_execution_root(source_dir, build_dir) / "external" / "abseil-cpp+",
        ]
    elif allocator_id == "jemalloc":
        directories = [source_dir / "include"]
    else:
        directories = [source_dir / "include"]
    missing = [path for path in directories if not path.is_dir()]
    if missing:
        raise ArchiveError(f"{allocator_id} adapter include directories are missing: {missing}")
    return directories


def tcmalloc_link_inputs(
    source_dir: Path, build_dir: Path, primary_library: Path, logs: Path
) -> tuple[list[tuple[str, Path | str]], list[list[str]]]:
    query_file = (
        repository_root() / "rust" / "benchmark-suite" / "native" / "tcmalloc_link_inputs.cquery"
    ).resolve()
    command = [
        "bazel",
        f"--output_user_root={build_dir / 'bazel'}",
        "cquery",
        "--lockfile_mode=error",
        "--dynamic_mode=off",
        "-c",
        "opt",
        "--copt=-fno-omit-frame-pointer",
        "//tcmalloc:tcmalloc",
        "--output=starlark",
        f"--starlark:file={query_file}",
    ]
    result = subprocess.run(
        command,
        cwd=source_dir,
        env=command_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    query_log = logs / "tcmalloc-link-query.log"
    query_log.write_text(
        "$ " + " ".join(command) + "\n" + result.stderr,
        encoding="utf-8",
    )
    execution_root = bazel_execution_root(source_dir, build_dir)
    unresolved_archives: list[tuple[str, str, str]] = []
    flags: list[tuple[str, str]] = []
    owners: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0] in ("archive", "always"):
            kind, value, owner = fields
            unresolved_archives.append((kind, value, owner))
            owners.append(owner)
        elif len(fields) == 2 and fields[0] == "flag" and fields[1] == "-pthread":
            flags.append((fields[0], fields[1]))
        else:
            raise ArchiveError(f"unsupported TCMalloc cquery link input: {line!r}")
    unique_owners = list(dict.fromkeys(owners))
    if not unique_owners:
        raise ArchiveError("TCMalloc CcInfo closure did not expose static-library owners")
    closure_command = [
        "bazel",
        f"--output_user_root={build_dir / 'bazel'}",
        "build",
        "--lockfile_mode=error",
        "--dynamic_mode=off",
        "-c",
        "opt",
        "--copt=-fno-omit-frame-pointer",
        *unique_owners,
    ]
    with query_log.open("a", encoding="utf-8") as log:
        log.write("$ " + " ".join(closure_command) + "\n")
        log.flush()
        subprocess.run(
            closure_command,
            cwd=source_dir,
            env=command_environment(),
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    inputs: list[tuple[str, Path | str]] = []
    archives: set[Path] = set()
    for kind, value, _owner in unresolved_archives:
        candidate = (execution_root / value).resolve()
        if not candidate.is_file():
            raise ArchiveError(f"TCMalloc linker input does not exist: {candidate}")
        if candidate.read_bytes()[:8] not in (b"!<arch>\n", b"!<thin>\n"):
            raise ArchiveError(f"TCMalloc linker input is not a static archive: {candidate}")
        inputs.append((kind, candidate))
        archives.add(candidate)
    inputs.extend(flags)
    if primary_library.resolve() not in archives:
        raise ArchiveError("TCMalloc CcInfo closure omitted the locked primary libtcmalloc.lo")
    if len(archives) < 2:
        raise ArchiveError("TCMalloc link closure did not include its transitive static libraries")
    return inputs, [command, closure_command]


def write_link_manifest(
    allocator_id: str,
    source_dir: Path,
    build_dir: Path,
    primary_library: Path,
    build_root: Path,
    logs: Path,
) -> tuple[Path, list[list[str]], list[dict[str, object]]]:
    query_commands: list[list[str]] = []
    if allocator_id == "tcmalloc":
        inputs, queries = tcmalloc_link_inputs(source_dir, build_dir, primary_library, logs)
        query_commands.extend(queries)
    else:
        inputs = [("archive", primary_library.resolve())]
    manifest_dir = build_root / "link-manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / f"{allocator_id}.txt"
    lines: list[str] = []
    provenance: list[dict[str, object]] = []
    for kind, value in inputs:
        if isinstance(value, Path):
            lines.append(f"{kind}\t{value}")
            provenance.append(
                {
                    "kind": kind,
                    "path": str(value),
                    "sha256": sha256_file(value),
                }
            )
        else:
            lines.append(f"{kind}\t{value}")
            provenance.append({"kind": kind, "value": value})
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest.resolve(), query_commands, provenance


def find_primary_library(record: Mapping[str, object], source_dir: Path, build_dir: Path) -> Path:
    allocator_id = require_string(record.get("id"), "allocator.id")
    expected = require_string(record.get("expected_static_library"), allocator_id)
    build = require_mapping(record.get("build"), f"{allocator_id}.build")
    system = require_string(build.get("system"), f"{allocator_id}.build.system")
    if system == "cmake-ninja":
        candidates = sorted(build_dir.rglob(expected))
    elif system == "autoconf-make":
        candidates = [source_dir / "lib" / expected]
    elif system == "bazel":
        candidates = [source_dir / "bazel-bin" / "tcmalloc" / expected]
    else:
        raise ArchiveError(f"{allocator_id} has unsupported build system {system!r}")
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        raise ArchiveError(
            f"{allocator_id} expected exactly one recipe-local {expected}, found {existing}"
        )
    return existing[0].resolve()


def run_text(command: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=command_environment(),
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.stdout


def defined_symbols(binary: Path) -> set[str]:
    # Release linkers may localize allocator symbols (mimalloc aliases its
    # override and mi_* entry points at the same address). Inspect all defined
    # ELF symbols so localization cannot hide either the intended allocator or
    # an accidental second competitor.
    output = run_text(["nm", "--defined-only", str(binary)], cwd=binary.parent)
    symbols: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if fields:
            symbols.add(fields[-1])
    return symbols


def dynamic_dependencies(binary: Path) -> list[str]:
    output = run_text(["readelf", "-d", str(binary)], cwd=binary.parent)
    return [line.strip() for line in output.splitlines() if "(NEEDED)" in line]


def validate_symbol_identity(
    allocator_id: str, symbols: set[str], needed: Sequence[str]
) -> dict[str, object]:
    missing_adapter = sorted(set(ADAPTER_SYMBOLS) - symbols)
    if missing_adapter:
        raise ArchiveError(f"{allocator_id} child is missing adapter symbols: {missing_adapter}")
    expected_family = COMPETITOR_SYMBOLS[allocator_id]
    if expected_family not in symbols:
        raise ArchiveError(
            f"{allocator_id} child does not define its expected allocator symbol {expected_family}"
        )
    forbidden = {
        symbol
        for _family_id, symbol in COMPETITOR_SYMBOLS.items()
        if symbol != expected_family and symbol in symbols
    }
    if forbidden:
        raise ArchiveError(f"{allocator_id} child contains a second allocator: {sorted(forbidden)}")
    competitor_needed = [
        line
        for line in needed
        if any(name in line.lower() for name in ("tcmalloc", "jemalloc", "mimalloc"))
    ]
    if competitor_needed:
        raise ArchiveError(
            f"{allocator_id} child dynamically loads a competitor allocator: {competitor_needed}"
        )
    return {
        "expected_allocator_symbol": expected_family,
        "forbidden_allocator_symbols_absent": sorted(
            set(COMPETITOR_SYMBOLS.values()) - {expected_family}
        ),
        "needed_libraries": needed,
    }


def validate_link_identity(allocator_id: str, binary: Path) -> dict[str, object]:
    return validate_symbol_identity(
        allocator_id, defined_symbols(binary), dynamic_dependencies(binary)
    )


def build_child(
    record: Mapping[str, object],
    source_dir: Path,
    build_dir: Path,
    library: Path,
    build_root: Path,
    logs: Path,
    workflow_commit: str,
) -> tuple[Path, list[list[str]], list[dict[str, object]], dict[str, object]]:
    allocator_id = require_string(record.get("id"), "allocator.id")
    link_manifest, query_commands, link_inputs = write_link_manifest(
        allocator_id, source_dir, build_dir, library, build_root, logs
    )
    target_dir = (build_root / "cargo-target" / allocator_id).resolve()
    command = [
        "soldr",
        "cargo",
        "build",
        "--manifest-path",
        str(repository_root() / "rust" / "Cargo.toml"),
        "-p",
        "benchmark-suite",
        "--bin",
        "benchmark-child",
        "--release",
        "--locked",
    ]
    environment = command_environment()
    environment.update(
        {
            "CARGO_TARGET_DIR": str(target_dir),
            "BENCH_ALLOCATOR_ID": allocator_id,
            "BENCH_ALLOCATOR_VERSION": adapter_version(record, workflow_commit),
            "BENCH_ALLOCATOR_SOURCE_SHA": source_commit(record, workflow_commit),
            "BENCH_ALLOCATOR_LIBRARY": str(library.resolve()),
            "BENCH_ALLOCATOR_LIBRARY_SHA256": sha256_file(library),
            "BENCH_ALLOCATOR_INCLUDE_DIRS": os.pathsep.join(
                str(path.resolve())
                for path in adapter_include_directories(allocator_id, source_dir, build_dir)
            ),
            "BENCH_ALLOCATOR_LINK_MANIFEST": str(link_manifest),
        }
    )
    with (logs / f"{allocator_id}-child.log").open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        subprocess.run(
            command,
            cwd=repository_root() / "rust",
            env=environment,
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    built = target_dir / "release" / "benchmark-child"
    if not built.is_file():
        raise ArchiveError(f"Cargo did not produce {allocator_id} benchmark child at {built}")
    output_dir = build_root / "children" / allocator_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "benchmark-child"
    shutil.copyfile(built, output)
    output.chmod(0o755)
    link_identity = validate_link_identity(allocator_id, output)
    return output, [*query_commands, command], link_inputs, link_identity


def run_adapter_smoke(
    allocator_id: str,
    version: str,
    source_sha: str,
    library_sha256: str,
    binary: Path,
) -> dict[str, object]:
    environment = benchmark_runtime_environment()
    environment["BENCH_CHILD_BINARY_SHA256"] = sha256_file(binary)
    result = subprocess.run(
        [str(binary), "--adapter-smoke"],
        cwd=binary.parent,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ArchiveError(f"{allocator_id} adapter smoke did not emit JSON: {error}") from error
    smoke = require_mapping(parsed, f"{allocator_id} adapter smoke")
    expected = {
        "allocator_id": allocator_id,
        "allocator_version": version,
        "source_sha": source_sha,
        "library_sha256": library_sha256,
        "child_binary_sha256": sha256_file(binary),
    }
    for field, value in expected.items():
        if smoke.get(field) != value:
            raise ArchiveError(
                f"{allocator_id} adapter smoke {field} mismatch: "
                f"expected {value!r}, got {smoke.get(field)!r}"
            )
    checksum = smoke.get("checksum")
    usable_size = smoke.get("usable_size")
    if not isinstance(checksum, int) or checksum <= 0:
        raise ArchiveError(f"{allocator_id} adapter smoke checksum is invalid: {checksum!r}")
    if not isinstance(usable_size, int) or usable_size < 128:
        raise ArchiveError(f"{allocator_id} adapter smoke usable size is invalid: {usable_size!r}")
    return dict(smoke)


def mimalloc_option_comparison(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    by_id = {require_string(record.get("id"), "allocator.id"): record for record in records}
    upstream = require_mapping(by_id["upstream-mimalloc"].get("build"), "upstream build")
    fork = require_mapping(by_id["mimalloc-pprof"].get("build"), "fork build")
    upstream_flags = set(require_string_list(upstream.get("flags"), "upstream flags"))
    fork_flags = set(require_string_list(fork.get("flags"), "fork flags"))
    common = {
        "build_type": "Release",
        "MI_BUILD_STATIC": "ON",
        "MI_BUILD_SHARED": "OFF",
        "MI_BUILD_TESTS": "OFF",
        "MI_OPT_ARCH": "OFF",
        "MI_OPT_SIMD": "ON",
        "optimization": "-O3",
        "frame_pointers": "-fno-omit-frame-pointer",
    }
    expected_common_flags = {
        "Release",
        "MI_BUILD_STATIC=ON",
        "MI_BUILD_SHARED=OFF",
        "MI_BUILD_TESTS=OFF",
        "MI_OPT_ARCH=OFF",
        "MI_OPT_SIMD=ON",
        "-O3",
        "-fno-omit-frame-pointer",
    }
    if not expected_common_flags.issubset(upstream_flags & fork_flags):
        raise ArchiveError("upstream/fork mimalloc common build options are not equivalent")
    upstream_only = upstream_flags - fork_flags
    fork_only = fork_flags - upstream_flags
    if upstream_only != {"MI_PPROF=OFF"} or fork_only != {"MI_PPROF=ON"}:
        raise ArchiveError(
            "upstream/fork mimalloc options differ outside the intended MI_PPROF field"
        )
    return {
        "equivalent_fields": common,
        "intentional_difference": {
            "field": "MI_PPROF",
            "upstream-mimalloc": "OFF",
            "mimalloc-pprof": "ON",
        },
        "runtime_disabled_state": {
            "MIMALLOC_PROF": "0",
            "MIMALLOC_MEMORY_EVENTS": "0",
        },
    }


def build_records(
    records: Iterable[Mapping[str, object]], build_root: Path, jobs: int
) -> dict[str, object]:
    producer_started = time.perf_counter()
    records = list(records)
    logs = build_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    sources, applied_patches, source_tree_sha256s = prepare_sources(records, build_root, logs)
    workflow_commit = checkout_commit()
    builds: list[dict[str, object]] = []
    for record in records:
        allocator_id = require_string(record.get("id"), "allocator.id")
        source_dir = sources[allocator_id]
        build_dir = build_root / "build" / allocator_id
        build_dir.mkdir(parents=True, exist_ok=True)
        build = require_mapping(record.get("build"), f"{allocator_id}.build")
        commands = require_commands(build.get("commands"), f"{allocator_id}.build.commands")
        resolved = [expand_command(command, source_dir, build_dir, jobs) for command in commands]
        expected_generated_lock = build.get("generated_lock_sha256")
        generated_lock_verified = expected_generated_lock is None
        required_tool = build.get("required_tool_version")
        if required_tool is not None:
            actual_tool = checked_tool_version([resolved[0][0]])
            if not _tool_version_satisfies(actual_tool, required_tool):
                raise ArchiveError(f"{allocator_id} requires {required_tool}, got {actual_tool}")
        with (logs / f"{allocator_id}.log").open("w", encoding="utf-8") as log:
            for command in resolved:
                if "build" in command and not generated_lock_verified:
                    raise ArchiveError(
                        f"{allocator_id} generated module lock was not verified before build"
                    )
                log.write("$ " + " ".join(command) + "\n")
                log.flush()
                subprocess.run(
                    command,
                    cwd=source_dir,
                    env=command_environment(),
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                if "--lockfile_mode=update" in command:
                    generated_lock = source_dir / "MODULE.bazel.lock"
                    actual_lock_sha256 = sha256_file(generated_lock)
                    if actual_lock_sha256 != expected_generated_lock:
                        raise ArchiveError(
                            f"{allocator_id} generated module lock mismatch: expected "
                            f"{expected_generated_lock}, got {actual_lock_sha256}"
                        )
                    generated_lock_verified = True
        library = find_primary_library(record, source_dir, build_dir)
        source_sha = source_commit(record, workflow_commit)
        version = adapter_version(record, workflow_commit)
        child, child_commands, link_inputs, link_identity = build_child(
            record,
            source_dir,
            build_dir,
            library,
            build_root,
            logs,
            workflow_commit,
        )
        library_sha256 = sha256_file(library)
        child_sha256 = sha256_file(child)
        smoke = run_adapter_smoke(
            allocator_id,
            version,
            source_sha,
            library_sha256,
            child,
        )
        builds.append(
            {
                "id": allocator_id,
                "version": version,
                "source_sha": source_sha,
                "canonical_repository": require_mapping(
                    record.get("source"), f"{allocator_id}.source"
                ).get("canonical_repository"),
                "source_archive_url": require_mapping(
                    record.get("source"), f"{allocator_id}.source"
                ).get("archive_url"),
                "source_archive_sha256": require_mapping(
                    record.get("source"), f"{allocator_id}.source"
                ).get("archive_sha256"),
                "source_directory": str(source_dir),
                "source_tree_sha256": source_tree_sha256s[allocator_id],
                "library": str(library),
                "library_sha256": library_sha256,
                "child_binary": str(child),
                "child_binary_sha256": child_sha256,
                "commands": [*resolved, *child_commands],
                "build_flags": require_string_list(
                    build.get("flags"), f"{allocator_id}.build.flags"
                ),
                "toolchain": {
                    "compiler": compiler_identity(
                        command_environment().get(
                            "CXX" if allocator_id == "tcmalloc" else "CC",
                            "c++" if allocator_id == "tcmalloc" else "cc",
                        )
                    ),
                    "linker": compiler_identity(command_environment().get("CC", "cc")),
                },
                "source_patches": applied_patches[allocator_id],
                "link_inputs": link_inputs,
                "link_identity": link_identity,
                "adapter_smoke": smoke,
            }
        )
    binary_hashes = [cast(str, build["child_binary_sha256"]) for build in builds]
    if len(set(binary_hashes)) != len(EXPECTED_IDS):
        raise ArchiveError("the four allocator child executable hashes are not distinct")
    build_elapsed_seconds = time.perf_counter() - producer_started
    if not (0.0 <= build_elapsed_seconds < float("inf")):
        raise ArchiveError("producer build elapsed time is not finite and nonnegative")
    return {
        "schema_version": 1,
        "build_elapsed_seconds": build_elapsed_seconds,
        "lockfile_sha256": sha256_file(default_lockfile()),
        "environment": command_environment(),
        "tool_versions": {
            "cc": compiler_identity(command_environment().get("CC", "cc")),
            "cxx": compiler_identity(command_environment().get("CXX", "c++")),
            "cmake": checked_tool_version(["cmake"]),
            "ninja": checked_tool_version(["ninja"]),
            "bazel": checked_tool_version(["bazel"]),
            "make": checked_tool_version(["make"]),
        },
        "mimalloc_option_comparison": mimalloc_option_comparison(records),
        "allocators": builds,
    }


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ArchiveError(f"source tree unexpectedly contains a symlink: {relative}")
        if path.is_dir():
            digest.update(f"D {relative}\n".encode())
        elif path.is_file():
            executable = "X" if path.stat().st_mode & stat.S_IXUSR else "-"
            digest.update(f"F{executable} {relative} {sha256_file(path)}\n".encode())
        else:
            raise ArchiveError(f"source tree has unsupported entry: {relative}")
    return digest.hexdigest()


def selftest() -> int:
    records = read_lockfile(default_lockfile())
    if len(records) != len(EXPECTED_IDS):
        raise AssertionError("valid lockfile did not return every allocator")
    with tempfile.TemporaryDirectory(prefix="mi-benchmark-allocator-selftest-") as temporary:
        root = Path(temporary)
        good = root / "good.tar.gz"
        with tarfile.open(good, "w:gz") as bundle:
            payload = b"benchmark source\n"
            info = tarfile.TarInfo("source/README")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
            script = b"#!/bin/sh\nexit 0\n"
            info = tarfile.TarInfo("source/configure")
            info.size = len(script)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(script))
        extraction = root / "extract"
        extract_archive(good, extraction)
        if (extraction / "source" / "README").read_bytes() != b"benchmark source\n":
            raise AssertionError("safe archive extraction changed its contents")
        if os.name != "nt" and not os.access(extraction / "source" / "configure", os.X_OK):
            raise AssertionError("safe archive extraction removed a build script's executable bit")

        try:
            download_archive(good.as_uri(), "0" * SHA256_LENGTH, root / "never-extract.tar.gz")
        except ArchiveError:
            pass
        else:
            raise AssertionError("corrupt archive checksum was accepted")
        if (root / "never-extract.tar.gz").exists():
            raise AssertionError("checksum mismatch was retained")

        traversal = root / "traversal.tar"
        with tarfile.open(traversal, "w") as bundle:
            info = tarfile.TarInfo("../outside")
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))
        try:
            extract_archive(traversal, root / "traversal-out")
        except ArchiveError:
            pass
        else:
            raise AssertionError("path traversal archive was accepted")

        symlink = root / "symlink.tar"
        with tarfile.open(symlink, "w") as bundle:
            link = tarfile.TarInfo("source/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "README"
            bundle.addfile(link)
        try:
            extract_archive(symlink, root / "symlink-out")
        except ArchiveError:
            pass
        else:
            raise AssertionError("symlink archive was accepted")

        for invalid_commit in ("main", "c316de3"):
            try:
                validate_source(
                    "tcmalloc",
                    invalid_commit,
                    {
                        "kind": "archive",
                        "canonical_repository": "https://github.com/google/tcmalloc",
                        "commit": invalid_commit,
                        "archive_url": "https://github.com/google/tcmalloc/archive/main.tar.gz",
                        "archive_sha256": "0" * SHA256_LENGTH,
                    },
                )
            except LockfileError:
                pass
            else:
                raise AssertionError(f"invalid lockfile commit was accepted: {invalid_commit}")
        valid_symbols = {*ADAPTER_SYMBOLS, COMPETITOR_SYMBOLS["tcmalloc"]}
        validate_symbol_identity("tcmalloc", valid_symbols, ["libc.so.6"])
        try:
            validate_symbol_identity(
                "tcmalloc",
                {*valid_symbols, COMPETITOR_SYMBOLS["jemalloc"]},
                ["libc.so.6"],
            )
        except ArchiveError:
            pass
        else:
            raise AssertionError("a child with two allocator symbol families was accepted")
        try:
            validate_symbol_identity("jemalloc", {*ADAPTER_SYMBOLS}, ["libc.so.6"])
        except ArchiveError:
            pass
        else:
            raise AssertionError("a child missing its allocator symbol family was accepted")
    print(
        "PASS allocator builder selftest: lock, checksum, traversal, mode, source, and link identity"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lockfile", type=Path, default=default_lockfile())
    parser.add_argument(
        "--selftest", action="store_true", help="run offline lock and archive safety tests"
    )
    parser.add_argument(
        "--build-root", type=Path, help="download/extract/build root; enables native builds"
    )
    parser.add_argument(
        "--jobs", type=int, default=1, help="parallel jobs forwarded to native build tools"
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least one")
    if args.selftest:
        return selftest()
    records = read_lockfile(args.lockfile)
    if args.build_root is None:
        print(f"PASS {args.lockfile}: validated {len(records)} immutable allocator records")
        return 0
    provenance = build_records(records, args.build_root.resolve(), args.jobs)
    output = args.build_root / "allocator-provenance.json"
    output.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS built {len(records)} allocators; wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
