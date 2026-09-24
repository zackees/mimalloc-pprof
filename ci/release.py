#!/usr/bin/env python3
"""Issue-driven, SHA-pinned release entry point for mimalloc-pprof (#444).

The real publisher remains disabled in auto-release.yml until the destination
state machine is implemented. This front door only records an attempt and
dispatches a non-publishing worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import struct
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
REPO = "zackees/mimalloc-pprof"
MARKER = "<!-- fleet-release-attempt/v1 -->"
FREEZE_MARKER = "<!-- fleet-release-freeze/v1 -->"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
MAX_ASSET_BYTES = 100_000_000
ASSET_TEMPLATES = (
    "mimalloc-pprof-c-{tag}.zip",
    "mimalloc-pprof-macos-arm64-{tag}.tar.gz",
    "mimalloc-pprof-macos-x86_64-{tag}.tar.gz",
    "mimalloc-pprof-windows-x64-gnu-{tag}.zip",
    "mimalloc-pprof-windows-x64-msvc-{tag}.zip",
)
TARGETS = {
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
    "windows-x64-gnu": "x86_64-pc-windows-gnu",
    "windows-x64-msvc": "x86_64-pc-windows-msvc",
}


class ReleaseError(ValueError):
    """A candidate or release artifact is unsafe to proceed with."""


def command(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ReleaseError(f"{' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def source_version() -> str:
    def field(section: str, name: str) -> str:
        match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]+)"\s*$', section, re.MULTILINE)
        if not match:
            raise ReleaseError(f"missing {name} in Cargo package section")
        return match.group(1)

    manifest = (ROOT / "rust/mimalloc-pprof/Cargo.toml").read_text()
    package = re.search(r"(?ms)^\[package\]\s*\n(.*?)(?=^\[|\Z)", manifest)
    if not package:
        raise ReleaseError("Cargo.toml has no [package] section")
    version = field(package.group(1), "version")
    lock = (ROOT / "rust/Cargo.lock").read_text()
    sections = re.findall(r"(?ms)^\[\[package\]\]\s*\n(.*?)(?=^\[\[package\]\]|\Z)", lock)
    locked = [
        field(section, "version")
        for section in sections
        if field(section, "name") == "mimalloc-pprof"
    ]
    if locked != [version]:
        raise ReleaseError(
            f"Cargo.lock mimalloc-pprof version {locked} differs from Cargo.toml {version}"
        )
    return version


def directive(issue: int, version: str, candidate_sha: str) -> dict[str, Any]:
    if issue < 1 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ReleaseError("issue and semver version are required")
    if not SHA_RE.fullmatch(candidate_sha):
        raise ReleaseError("candidate_sha must be a lowercase full 40-character SHA")
    tag = f"v{version}"
    return {
        "schema": "fleet-release-attempt/v1",
        "repository": REPO,
        "issue": issue,
        "version": version,
        "tag": tag,
        "candidate_sha": candidate_sha,
        "mode": "full",
        "destinations": ["crates.io", "github-release"],
        "assets": [template.format(tag=tag) for template in ASSET_TEMPLATES],
    }


def require_same_directive(existing: dict[str, Any], desired: dict[str, Any]) -> None:
    if existing != desired:
        raise ReleaseError("release directive is already bound to different identity or scope")


def comment_body(value: dict[str, Any], state: str, run_url: str = "") -> str:
    now = datetime.now(timezone.utc).isoformat()
    link = f"; run: {run_url}" if run_url else ""
    return f"{MARKER}\n```json\n{json.dumps(value, indent=2, sort_keys=True)}\n```\nState: {state}; UTC: {now}{link}"


def parse_directive(body: str) -> dict[str, Any] | None:
    if MARKER not in body:
        return None
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    if not match:
        raise ReleaseError("release comment has no JSON directive")
    value: object = json.loads(match.group(1))
    if not isinstance(value, dict):
        raise ReleaseError("unrecognized release directive")
    parsed = cast(dict[str, Any], value)
    if parsed.get("schema") != "fleet-release-attempt/v1":
        raise ReleaseError("unrecognized release directive")
    return parsed


def issue_comments(issue: int) -> list[str]:
    raw = command(
        "gh", "api", "--paginate", "--slurp", f"repos/{REPO}/issues/{issue}/comments?per_page=100"
    )
    pages = cast(list[list[dict[str, Any]]], json.loads(raw))
    trusted = {"OWNER", "MEMBER", "COLLABORATOR"}
    return [
        str(row["body"])
        for page in pages
        for row in page
        if row.get("author_association") in trusted
        or row.get("user", {}).get("login") == "github-actions[bot]"
    ]


def issue_directives(issue: int) -> list[dict[str, Any]]:
    return [parsed for body in issue_comments(issue) if (parsed := parse_directive(body))]


def frozen_identity(issue: int) -> dict[str, Any] | None:
    records: list[dict[str, Any]] = []
    for body in issue_comments(issue):
        if FREEZE_MARKER in body:
            match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
            if not match:
                raise ReleaseError("release freeze has no JSON")
            record: object = json.loads(match.group(1))
            if not isinstance(record, dict):
                raise ReleaseError("invalid release freeze")
            typed_record = cast(dict[str, Any], record)
            if typed_record.get("schema") != "fleet-release-freeze/v1":
                raise ReleaseError("invalid release freeze")
            records.append(typed_record)
    if records and any(record != records[0] for record in records):
        raise ReleaseError("conflicting frozen release identities")
    return records[0] if records else None


def require_history(
    records: list[dict[str, Any]], desired: dict[str, Any], frozen: dict[str, Any] | None
) -> None:
    for record in records:
        if {key: item for key, item in record.items() if key != "candidate_sha"} != {
            key: item for key, item in desired.items() if key != "candidate_sha"
        }:
            raise ReleaseError("release scope changed across issue directives")
    if frozen is not None:
        if frozen.get("directive") != desired:
            raise ReleaseError("release identity is frozen at a different candidate")
        if not records or records[-1] != desired:
            raise ReleaseError("issue retargeted after release identity froze")


def freeze_record(value: dict[str, Any], info: dict[str, Any], crate_sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", crate_sha256):
        raise ReleaseError("freeze requires packaged crate SHA-256")
    if {key: info.get(key) for key in value} != value:
        raise ReleaseError("info.json does not match release directive")
    artifacts: object = info.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseError("freeze requires all declared assets")
    rows = cast(list[dict[str, Any]], artifacts)
    if [row.get("name") for row in rows] != value["assets"]:
        raise ReleaseError("freeze requires all declared assets")
    hashes = {str(row["name"]): row.get("sha256") for row in rows}
    if any(
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in hashes.values()
    ):
        raise ReleaseError("freeze requires valid asset SHA-256 hashes")
    return {
        "schema": "fleet-release-freeze/v1",
        "directive": value,
        "info_sha256": hashlib.sha256(
            json.dumps(info, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "asset_sha256": hashes,
        "crate_sha256": crate_sha256,
    }


def require_frozen_info(
    frozen: dict[str, Any], value: dict[str, Any], info: dict[str, Any]
) -> None:
    if frozen != freeze_record(value, info, frozen.get("crate_sha256", "")):
        raise ReleaseError("release assets differ from frozen info.json")


def verify_existing_release_assets(value: dict[str, Any], frozen: dict[str, Any]) -> None:
    """Reject conflicting destination bytes while allowing absent outputs on resume."""
    raw = command("gh", "api", f"repos/{REPO}/releases?per_page=100")
    releases: list[dict[str, Any]] = json.loads(raw)
    matches = [row for row in releases if row.get("tag_name") == value["tag"]]
    if len(matches) > 1:
        raise ReleaseError("duplicate GitHub Releases for tag")
    if not matches:
        return
    release = matches[0]
    if release.get("target_commitish") not in (value["candidate_sha"], value["tag"]):
        raise ReleaseError("existing GitHub Release has conflicting identity")
    declared = frozen["asset_sha256"]
    for asset in release.get("assets", []):
        name = asset.get("name")
        if name not in declared:
            raise ReleaseError(f"unexpected existing release asset {name}")
        if asset.get("digest") != f"sha256:{declared[name]}":
            raise ReleaseError(f"existing release asset {name} differs from frozen hash")


def verify_existing_crate(value: dict[str, Any], frozen: dict[str, Any]) -> None:
    import urllib.error
    import urllib.request

    url = f"https://crates.io/api/v1/crates/mimalloc-pprof/{value['version']}"
    request = urllib.request.Request(url, headers={"User-Agent": "mimalloc-pprof release resume"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise ReleaseError(f"crates.io version check returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ReleaseError(f"crates.io version check failed: {error}") from error
    if result.get("version", {}).get("checksum") != frozen.get("crate_sha256"):
        raise ReleaseError("existing crates.io package differs from frozen crate hash")


def recorded_merge_sha(body: str) -> str:
    matches = re.findall(
        r"(?im)^- Candidate merge SHA:\s*(?:`|\*\*)?([0-9a-f]{40})(?:`|\*\*)?\s*$", body
    )
    if len(matches) != 1:
        raise ReleaseError("release issue must record one full Candidate merge SHA")
    return matches[0]


def recorded_version_bump_pr(body: str) -> int:
    matches = re.findall(r"(?im)^- Version-bump PR:\s*#([1-9][0-9]*)\s*$", body)
    if len(matches) != 1:
        raise ReleaseError("release issue must record one Version-bump PR number")
    return int(matches[0])


def recorded_version_bump_sha(body: str) -> str:
    matches = re.findall(
        r"(?im)^- Version-bump merge SHA:\s*(?:`|\*\*)?([0-9a-f]{40})(?:`|\*\*)?\s*$", body
    )
    if len(matches) != 1:
        raise ReleaseError("release issue must record one full Version-bump merge SHA")
    return matches[0]


def recorded_candidate_pr(body: str) -> int:
    matches = re.findall(r"(?im)^- Candidate PR:\s*#([1-9][0-9]*)\s*$", body)
    if len(matches) != 1:
        raise ReleaseError("release issue must record one Candidate PR number")
    return int(matches[0])


def merged_pr_sha(number: int) -> str:
    pr = json.loads(
        command(
            "gh",
            "pr",
            "view",
            str(number),
            "-R",
            REPO,
            "--json",
            "baseRefName,mergedAt,mergeCommit",
        )
    )
    sha = pr.get("mergeCommit", {}).get("oid")
    if (
        pr.get("baseRefName") != "main"
        or not pr.get("mergedAt")
        or not isinstance(sha, str)
        or not SHA_RE.fullmatch(sha)
    ):
        raise ReleaseError(f"PR #{number} is not a reviewed merged commit on main")
    return sha


def is_ancestor(older: str, newer: str) -> bool:
    return (
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", older, newer),
            cwd=ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def validate_candidate(
    value: dict[str, Any],
    *,
    require_registry_free: bool = True,
    frozen: dict[str, Any] | None = None,
) -> None:
    expected = directive(value["issue"], value["version"], value["candidate_sha"])
    require_same_directive(expected, value)
    issue = json.loads(
        command(
            "gh", "issue", "view", str(value["issue"]), "-R", REPO, "--json", "title,state,body"
        )
    )
    if issue.get("state") != "OPEN" or f"v{value['version']}" not in issue.get("title", ""):
        raise ReleaseError("release issue is closed or targets a different version")
    body = issue.get("body", "")
    bump = recorded_version_bump_sha(body)
    if merged_pr_sha(recorded_version_bump_pr(body)) != bump:
        raise ReleaseError("version bump differs from its recorded PR merge")
    if recorded_merge_sha(body) != value["candidate_sha"]:
        raise ReleaseError("candidate differs from issue control")
    if merged_pr_sha(recorded_candidate_pr(body)) != value["candidate_sha"]:
        raise ReleaseError("candidate differs from its recorded PR merge")
    if source_version() != value["version"]:
        raise ReleaseError("source Cargo version differs from the release directive")
    head = command("git", "rev-parse", "HEAD")
    if head != value["candidate_sha"]:
        raise ReleaseError("checkout HEAD differs from release candidate SHA")
    command("git", "fetch", "origin", "main")
    if not is_ancestor(bump, head) or not is_ancestor(head, "origin/main"):
        raise ReleaseError("candidate must descend from the bump and be merged into main")
    parents = command("git", "rev-list", "--parents", "-n", "1", bump).split()
    if len(parents) not in (2, 3) or parents[0] != bump:
        raise ReleaseError("version bump is not a merge result")
    previous = command("git", "show", f"{parents[1]}:rust/mimalloc-pprof/Cargo.toml")
    previous_version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', previous)
    if not previous_version or previous_version.group(1) == value["version"]:
        raise ReleaseError("recorded bump did not change the version from its first parent")
    bumped = command("git", "show", f"{bump}:rust/mimalloc-pprof/Cargo.toml")
    bumped_version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', bumped)
    if not bumped_version or bumped_version.group(1) != value["version"]:
        raise ReleaseError("recorded bump has the wrong version")
    if command("git", "status", "--porcelain"):
        raise ReleaseError("release candidate checkout must be clean")
    existing_tag = command("git", "ls-remote", "--tags", "origin", f"refs/tags/{value['tag']}")
    if existing_tag:
        if frozen is None:
            raise ReleaseError(f"tag {value['tag']} already exists without frozen identity")
        tag_sha = command("git", "ls-remote", "--tags", "origin", f"refs/tags/{value['tag']}^{{}}")
        resolved = tag_sha.split()[0] if tag_sha else existing_tag.split()[0]
        if resolved != head:
            raise ReleaseError(f"tag {value['tag']} points to a different commit")
    if frozen is not None and frozen.get("directive") != value:
        raise ReleaseError("frozen release identity differs from directive")
    if require_registry_free and frozen is None:
        # gh api cannot query crates.io. urllib returns HTTP 404 for an unused version.
        import urllib.error
        import urllib.request

        url = f"https://crates.io/api/v1/crates/mimalloc-pprof/{value['version']}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "mimalloc-pprof release preflight"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status == 200:
                    raise ReleaseError(f"crates.io version {value['version']} already exists")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise ReleaseError(f"crates.io version check returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise ReleaseError(f"crates.io version check failed: {error}") from error


def archive_members(path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    entries: list[tuple[str, bool, bytes]] = []
    links: dict[str, str] = {}
    try:
        if path.name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                total = 0
                for row in archive.infolist():
                    total += row.file_size
                    if (
                        row.file_size > MAX_ASSET_BYTES
                        or total > MAX_ASSET_BYTES
                        or (row.external_attr >> 16) & 0o170000 == 0o120000
                    ):
                        raise ReleaseError(f"invalid archive member {row.filename}")
                    entries.append(
                        (row.filename, row.is_dir(), archive.read(row) if not row.is_dir() else b"")
                    )
        else:
            with tarfile.open(path, "r:gz") as archive:
                total = 0
                for row in archive.getmembers():
                    if row.isdir():
                        entries.append((row.name, True, b""))
                    elif row.issym():
                        if (
                            row.linkname.startswith("/")
                            or "\\" in row.linkname
                            or re.match(r"^[A-Za-z]:", row.linkname)
                        ):
                            raise ReleaseError(f"unsafe archive symlink {row.name}")
                        entries.append((row.name, False, f"SYMLINK:{row.linkname}".encode()))
                        links[row.name.removeprefix("./")] = row.linkname
                    elif row.isfile():
                        stream = archive.extractfile(row)
                        total += row.size
                        if stream is None or row.size > MAX_ASSET_BYTES or total > MAX_ASSET_BYTES:
                            raise ReleaseError(f"invalid archive member {row.name}")
                        entries.append((row.name, False, stream.read()))
                    else:
                        raise ReleaseError(f"unsupported archive member {row.name}")
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as error:
        raise ReleaseError(f"malformed archive {path.name}: {error}") from error
    for raw, is_directory, data in entries:
        name = raw.removeprefix("./").rstrip("/")
        if not name and raw in (".", "./") and is_directory:
            continue
        if (
            name.startswith("/")
            or "\\" in name
            or re.match(r"^[A-Za-z]:", name)
            or any(part in ("", ".", "..") for part in name.split("/"))
        ):
            raise ReleaseError(f"unsafe archive member {raw}")
        if is_directory:
            continue
        if name in members:
            raise ReleaseError(f"duplicate archive member {name}")
        members[name] = data
    for name in links:
        seen: set[str] = set()
        current = name
        while current in links:
            if current in seen:
                raise ReleaseError(f"cyclic archive symlink {name}")
            seen.add(current)
            current = posixpath.normpath(posixpath.join(posixpath.dirname(current), links[current]))
            if current.startswith("../") or current == ".." or current not in members:
                raise ReleaseError(f"unresolved archive symlink {name}")
    return members


def inspect_archive(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    members = archive_members(path)
    if path.name.startswith("mimalloc-pprof-c-"):
        expected = {
            name: (ROOT / "rust/mimalloc-pprof/vendor" / name).read_bytes()
            for name in (
                "mimalloc-pprof-amalgamated.c",
                "mimalloc-pprof-amalgamated.h",
                "mimalloc.h",
                "mimalloc-stats.h",
                "README.md",
            )
        }
        if members != expected:
            raise ReleaseError("C archive contents differ from candidate vendor sources")
        return {
            "target": "source",
            "provenance_commit": value["candidate_sha"],
            "members": sorted(members),
        }
    asset = next((key for key in TARGETS if f"-{key}-" in path.name), None)
    if asset is None:
        raise ReleaseError(f"unknown target archive {path.name}")
    target = TARGETS[asset]
    provenance = members.get("PROVENANCE.txt", b"").decode("utf-8", errors="replace")
    if (
        not re.search(rf"(?m)^commit:\s+{value['candidate_sha']}$", provenance)
        or not re.search(rf"(?m)^target:\s+{re.escape(target)}$", provenance)
        or not provenance.startswith(f"mimalloc-pprof {value['candidate_sha']} -- {asset}\n")
    ):
        raise ReleaseError(f"{path.name} has missing or mismatched provenance")
    dylibs = [
        name
        for name, data in members.items()
        if re.fullmatch(r"lib/libmimalloc\.3(?:\.[0-9]+)*\.dylib", name)
        and not data.startswith(b"SYMLINK:")
    ]
    if asset.startswith("macos") and len(dylibs) != 1:
        raise ReleaseError(f"{path.name} must contain one versioned mimalloc dylib")
    binary_name = dylibs[0] if asset.startswith("macos") else "bin/mimalloc.dll"
    binary = members.get(binary_name)
    if binary is None:
        raise ReleaseError(f"{path.name} lacks {binary_name}")
    if asset.startswith("macos"):
        cpu = 0x0100000C if asset == "macos-arm64" else 0x01000007
        if (
            len(binary) < 8
            or binary[:4] != b"\xcf\xfa\xed\xfe"
            or struct.unpack("<I", binary[4:8])[0] != cpu
        ):
            raise ReleaseError(f"{path.name} has wrong Mach-O target")
    else:
        if len(binary) < 0x40 or binary[:2] != b"MZ":
            raise ReleaseError(f"{path.name} has invalid PE binary")
        offset = struct.unpack("<I", binary[0x3C:0x40])[0]
        if binary[offset : offset + 6] != b"PE\0\0\x64\x86":
            raise ReleaseError(f"{path.name} has wrong PE target")
        if "bin/mimalloc-redirect.dll" not in members:
            raise ReleaseError(f"{path.name} lacks redirect DLL")
        if asset == "windows-x64-gnu" and "bin/libgcc_s_seh-1.dll" not in members:
            raise ReleaseError(f"{path.name} lacks GNU runtime DLL")
        if asset == "windows-x64-msvc" and "bin/libgcc_s_seh-1.dll" in members:
            raise ReleaseError(f"{path.name} contains GNU runtime in MSVC archive")
    return {
        "target": target,
        "provenance_commit": value["candidate_sha"],
        "binary": binary_name,
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "members": sorted(members),
    }


def inspect_artifacts(directory: Path, value: dict[str, Any]) -> dict[str, Any]:
    actual = {
        path.name for path in directory.iterdir() if path.is_file() and path.name != "info.json"
    }
    expected = set(value["assets"])
    if actual != expected:
        raise ReleaseError(
            f"asset set mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    artifacts: list[dict[str, Any]] = []
    for name in value["assets"]:
        path = directory / name
        if path.is_symlink():
            raise ReleaseError(f"asset {name} is a symlink")
        size = path.stat().st_size
        if size == 0 or size > MAX_ASSET_BYTES:
            raise ReleaseError(f"asset {name} has unacceptable size {size}")
        artifacts.append(
            {
                "name": name,
                "bytes": size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "validated": inspect_archive(path, value),
            }
        )
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**value, "directive_sha256": digest, "artifacts": artifacts}


def verify_info(directory: Path, value: dict[str, Any], info: dict[str, Any]) -> None:
    if info != inspect_artifacts(directory, value):
        raise ReleaseError("info.json identity, size, or SHA-256 differs from release artifacts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("start", "resume", "status", "worker-check", "record-outcome"):
        entry = commands.add_parser(name)
        entry.add_argument("--issue", type=int, default=444)
        entry.add_argument("--candidate-sha")
        if name in ("start", "resume"):
            entry.add_argument(
                "--dry", action="store_true", help="print a plan without issue writes or dispatch"
            )
        if name == "record-outcome":
            entry.add_argument("--run-url", required=True)
            entry.add_argument(
                "--state", choices=("dry-passed", "blocked", "real-passed"), required=True
            )
            entry.add_argument("--results", required=True)
    for operation in ("preflight-artifacts", "verify-artifacts"):
        artifact_command = commands.add_parser(operation)
        artifact_command.add_argument("--issue", type=int, default=444)
        artifact_command.add_argument("--candidate-sha", required=True)
        artifact_command.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "status":
            for body in issue_comments(args.issue):
                if parse_directive(body):
                    print(body.split("State: ", 1)[-1])
            return 0
        candidate = args.candidate_sha or command("git", "rev-parse", "HEAD")
        value = directive(args.issue, source_version(), candidate)
        if args.operation in ("preflight-artifacts", "verify-artifacts"):
            existing = issue_directives(args.issue)
            if not existing:
                raise ReleaseError("issue has no release directive")
            frozen = frozen_identity(args.issue)
            require_history(existing, value, frozen)
            if args.operation == "preflight-artifacts":
                info = inspect_artifacts(args.dist, value)
            else:
                info = json.loads((args.dist / "info.json").read_text())
            if frozen is not None:
                require_frozen_info(frozen, value, info)
            if args.operation == "preflight-artifacts":
                (args.dist / "info.json").write_text(
                    json.dumps(info, indent=2, sort_keys=True) + "\n"
                )
            verify_info(args.dist, value, info)
            print(json.dumps(info, indent=2))
            return 0
        existing = issue_directives(args.issue)
        frozen = frozen_identity(args.issue)
        if args.operation == "start" and existing:
            raise ReleaseError("attempt already exists; use resume")
        if args.operation in ("resume", "worker-check", "record-outcome"):
            if not existing:
                raise ReleaseError("attempt issue has no directive; use start")
            require_history(existing, value, frozen)
        if args.operation == "record-outcome":
            state = f"{args.state}; jobs: {args.results}"
            command(
                "gh",
                "issue",
                "comment",
                str(args.issue),
                "-R",
                REPO,
                "--body",
                comment_body(value, state, args.run_url),
            )
            print(state)
            return 0
        validate_candidate(value, frozen=frozen)
        if frozen is not None:
            verify_existing_release_assets(value, frozen)
            verify_existing_crate(value, frozen)
        if args.operation == "worker-check":
            print(f"validated release issue #{args.issue} at {candidate}")
            return 0
        if args.dry:
            print(
                json.dumps(
                    {"action": args.operation, "dispatch": False, "directive": value}, indent=2
                )
            )
            return 0
        command(
            "gh",
            "issue",
            "comment",
            str(args.issue),
            "-R",
            REPO,
            "--body",
            comment_body(value, "dry-worker-dispatching"),
        )
        command(
            "gh",
            "workflow",
            "run",
            "auto-release.yml",
            "-R",
            REPO,
            "--ref",
            "main",
            "-F",
            "dry_run=true",
            "-f",
            f"issue_number={args.issue}",
            "-f",
            f"candidate_sha={candidate}",
        )
        print(
            f"dispatched non-publishing release worker for {candidate}; issue https://github.com/{REPO}/issues/{args.issue}"
        )
        return 0
    except (ReleaseError, OSError, json.JSONDecodeError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
