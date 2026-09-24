"""Live GitHub/crates.io adapter for the issue-frozen release state machine.

Only auto-release.yml invokes ``--real``, after the exact-SHA full and shipped
archive gates. This module has no write side effects on import.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import tomllib
from ci import release
from ci import release_destinations as destinations


class LiveDestination(destinations.ReadOnlyDestination):
    def __init__(self, issue: int, crate: Path) -> None:
        self.issue = issue
        self.crate = crate
        self._crate_metadata: dict[str, object] | None = None

    def validate_crate(self, path: Path) -> None:
        if path != self.crate:
            raise release.ReleaseError("crate path changed before preflight")
        self._crate_metadata = crate_publish_metadata(path)

    @staticmethod
    def _github(*args: str) -> str:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout
        message = result.stderr.strip()
        if re.search(
            r"HTTP (?:429|5[0-9][0-9])\b|(?:connection|timeout|EOF|broken pipe|EPIPE)",
            message,
            re.I,
        ):
            raise destinations.TransientGitHubError(message)
        raise release.ReleaseError(f"GitHub write failed: {message}")

    def freeze(self, record: dict[str, object]) -> None:
        body = (
            f"{release.FREEZE_MARKER}\n```json\n"
            f"{json.dumps(record, indent=2, sort_keys=True)}\n```\n"
            "Immutable release identity: resume this issue, tag, candidate and bytes only."
        )
        self._github(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{release.REPO}/issues/{self.issue}/comments",
            "-f",
            f"body={body}",
        )

    def read_freeze(self) -> dict[str, object] | None:
        return release.frozen_identity(self.issue)

    def create_tag(self, tag: str, sha: str) -> None:
        self._github(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{release.REPO}/git/refs",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={sha}",
        )

    def create_draft(self, tag: str, sha: str) -> None:
        self._github(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{release.REPO}/releases",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"target_commitish={sha}",
            "-f",
            f"name=mimalloc-pprof {tag}",
            "-F",
            "draft=true",
            "-F",
            "prerelease=false",
            "-F",
            "generate_release_notes=true",
        )

    def upload_asset(self, tag: str, name: str, path: Path) -> None:
        if path.name != name:
            raise release.ReleaseError("asset path and frozen name differ")
        self._github("gh", "release", "upload", tag, str(path), "--repo", release.REPO)

    def publish_crate(self, path: Path) -> None:
        if path != self.crate:
            raise release.ReleaseError("crate path changed after preflight")
        archive = path.read_bytes()
        frozen = self.read_freeze()
        if frozen is None or frozen.get("crate_sha256") != hashlib.sha256(archive).hexdigest():
            raise release.ReleaseError("crate bytes differ from authoritative issue freeze")
        metadata = self._crate_metadata
        if metadata is None:
            raise release.ReleaseError("crate publish metadata was not validated in preflight")
        encoded = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode()
        body = struct.pack("<I", len(encoded)) + encoded + struct.pack("<I", len(archive)) + archive
        token = os.environ["CARGO_REGISTRY_TOKEN"]
        request = urllib.request.Request(
            "https://crates.io/api/v1/crates/new",
            data=body,
            method="PUT",
            headers={
                "Authorization": token,
                "Content-Type": "application/octet-stream",
                "Accept": "application/json",
                "User-Agent": "mimalloc-pprof issue-frozen release worker",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                if response.status < 200 or response.status >= 300:
                    raise release.ReleaseError(f"crates.io upload returned HTTP {response.status}")
                outcome = json.load(response)
                if outcome.get("errors"):
                    raise release.ReleaseError("crates.io rejected publish metadata or archive")
        except urllib.error.HTTPError as error:
            message = f"crates.io upload returned HTTP {error.code}"
            if error.code == 429 or error.code >= 500:
                raise destinations.AmbiguousCratePublishError(message) from error
            raise release.ReleaseError(message) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            raise destinations.AmbiguousCratePublishError(
                f"crates.io upload transport failed: {reason}"
            ) from error

    def finalize(self, tag: str) -> None:
        def release_id_shape(value: dict[str, object]) -> bool:
            return isinstance(value.get("id"), int)

        raw = self._gh_required(
            f"repos/{release.REPO}/releases/tags/{tag}", validate=release_id_shape
        )
        release_id = int(json.loads(raw)["id"])
        self._github(
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{release.REPO}/releases/{release_id}",
            "-F",
            "draft=false",
        )


def parse_cargo_metadata_stdout(stdout: str) -> dict[str, Any]:
    """Extract Cargo metadata from Soldr output without mistaking telemetry for it."""
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        start = stdout.find("{", offset)
        if start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(stdout[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        offset = start + consumed
        if isinstance(value, dict):
            document = cast(dict[str, Any], value)
            if isinstance(document.get("packages"), list) and isinstance(
                document.get("workspace_members"), list
            ):
                return document
    raise release.ReleaseError("Cargo metadata has no valid metadata JSON document")


def crate_publish_metadata(path: Path) -> dict[str, object]:
    """Build the Registry Web API Publish JSON from Cargo's resolved package data.

    Cargo metadata is an input, not the wire shape. The explicit mapping below is
    the Publish API schema from the Cargo Book, and rejects unsupported sources.
    """
    result = subprocess.run(
        ["soldr", "cargo", "metadata", "--no-deps", "--format-version", "1", "--locked"],
        cwd=release.ROOT / "rust",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise release.ReleaseError(f"Cargo metadata failed: {result.stderr.strip()}")
    # Soldr may prepend JSON timing telemetry before Cargo's JSON document.
    document = parse_cargo_metadata_stdout(result.stdout)
    packages = [row for row in document["packages"] if row.get("name") == "mimalloc-pprof"]
    if len(packages) != 1:
        raise release.ReleaseError("Cargo metadata has no unique mimalloc-pprof package")
    package = cast(dict[str, Any], packages[0])
    name, version = str(package["name"]), str(package["version"])
    if path.name != f"{name}-{version}.crate":
        raise release.ReleaseError("packaged crate version differs from Cargo metadata")
    with tarfile.open(path, "r:gz") as archive:

        def member_bytes(member_name: str) -> bytes:
            try:
                member = archive.getmember(f"{name}-{version}/{member_name}")
            except KeyError as error:
                raise release.ReleaseError(f"packaged crate lacks {member_name}") from error
            if not member.isfile():
                raise release.ReleaseError(f"packaged crate {member_name} is not a file")
            stream = archive.extractfile(member)
            if stream is None:
                raise release.ReleaseError(f"packaged crate cannot read {member_name}")
            return stream.read()

        manifest = tomllib.loads(member_bytes("Cargo.toml").decode())
        packaged_package = cast(dict[str, Any], manifest.get("package", {}))
        readme_file = packaged_package.get("readme")
        readme = member_bytes(str(readme_file)).decode() if readme_file else None
    if packaged_package.get("name") != name or packaged_package.get("version") != version:
        raise release.ReleaseError("packaged crate manifest identity differs from Cargo metadata")
    package_fields: tuple[tuple[str, str, object], ...] = (
        ("authors", "authors", []),
        ("description", "description", None),
        ("documentation", "documentation", None),
        ("homepage", "homepage", None),
        ("keywords", "keywords", []),
        ("categories", "categories", []),
        ("license", "license", None),
        ("license_file", "license-file", None),
        ("repository", "repository", None),
        ("links", "links", None),
        ("rust_version", "rust-version", None),
        ("readme", "readme", None),
    )
    for cargo_key, manifest_key, default in package_fields:
        if package.get(cargo_key) != packaged_package.get(manifest_key, default):
            raise release.ReleaseError(f"packaged crate {manifest_key} differs from Cargo metadata")
    if package["features"] != manifest.get("features", {}):
        raise release.ReleaseError("packaged crate features differ from Cargo metadata")
    if "target" in manifest:
        raise release.ReleaseError(
            "target-specific crate dependencies need explicit publish mapping"
        )
    packaged_deps: dict[tuple[str, str], Any] = {}
    for section, kind in (
        ("dependencies", "normal"),
        ("build-dependencies", "build"),
        ("dev-dependencies", "dev"),
    ):
        section_deps = cast(dict[str, Any], manifest.get(section, {}))
        for dependency_name, dependency_spec in section_deps.items():
            packaged_deps[(kind, dependency_name)] = dependency_spec
    cargo_dep_keys = {
        (str(row.get("kind") or "normal"), str(row["rename"] or row["name"]))
        for row in package["dependencies"]
    }
    if set(packaged_deps) != cargo_dep_keys:
        raise release.ReleaseError("packaged crate dependencies differ from Cargo metadata")
    deps: list[dict[str, object]] = []
    for raw in package["dependencies"]:
        dependency = cast(dict[str, Any], raw)
        if dependency.get("source") != "registry+https://github.com/rust-lang/crates.io-index":
            raise release.ReleaseError("publish metadata has unsupported dependency source")
        kind = str(dependency["kind"] or "normal")
        alias = str(dependency["rename"] or dependency["name"])
        spec: Any = packaged_deps[(kind, alias)]
        if isinstance(spec, str):
            spec = {"version": spec}
        if not isinstance(spec, dict):
            raise release.ReleaseError("packaged dependency has unsupported specification")
        spec = cast(dict[str, Any], spec)
        requirement = str(spec.get("version", ""))
        if (
            dependency["req"] not in (requirement, f"^{requirement}")
            or bool(spec.get("optional", False)) != dependency["optional"]
            or bool(spec.get("default-features", True)) != dependency["uses_default_features"]
            or spec.get("features", []) != dependency["features"]
            or spec.get("package", alias) != dependency["name"]
        ):
            raise release.ReleaseError("packaged dependency differs from Cargo metadata")
        deps.append(
            {
                "name": dependency["name"],
                "version_req": dependency["req"],
                "features": dependency["features"],
                "optional": dependency["optional"],
                "default_features": dependency["uses_default_features"],
                "target": dependency["target"],
                "kind": kind,
                "registry": None,
                "explicit_name_in_toml": dependency["rename"],
            }
        )
    return {
        "name": name,
        "vers": version,
        "deps": deps,
        "features": manifest.get("features", {}),
        "authors": packaged_package.get("authors", []),
        "description": packaged_package.get("description"),
        "documentation": packaged_package.get("documentation"),
        "homepage": packaged_package.get("homepage"),
        "readme": readme,
        "readme_file": readme_file,
        "keywords": packaged_package.get("keywords", []),
        "categories": packaged_package.get("categories", []),
        "license": packaged_package.get("license"),
        "license_file": packaged_package.get("license-file"),
        "repository": packaged_package.get("repository"),
        "badges": manifest.get("badges", {}),
        "links": packaged_package.get("links"),
        "rust_version": packaged_package.get("rust-version"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume one frozen release publication")
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--crate", type=Path, required=True)
    parser.add_argument("--real", action="store_true", required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise release.ReleaseError("live publisher requires the release Actions worker")
        if not os.environ.get("GH_TOKEN") or not os.environ.get("CARGO_REGISTRY_TOKEN"):
            raise release.ReleaseError("release worker lacks GitHub or crates.io credential")
        value = release.directive(args.issue, release.source_version(), args.candidate_sha)
        frozen = release.frozen_identity(args.issue)
        release.validate_candidate(
            value, frozen=frozen, allow_release_outputs=True, require_issue_ready=True
        )
        records = release.issue_directives(args.issue)
        if not records:
            raise release.ReleaseError("issue has no release directive")
        release.require_history(records, value, frozen)
        info = json.loads((args.dist / "info.json").read_text())
        # Validate the Registry Publish JSON against the exact packaged archive
        # before execute() can freeze the issue or create the immutable tag.
        metadata = crate_publish_metadata(args.crate)
        if metadata["name"] != "mimalloc-pprof" or metadata["vers"] != value["version"]:
            raise release.ReleaseError("packaged crate metadata differs from release directive")
        destinations.execute(
            LiveDestination(args.issue, args.crate),
            value,
            info,
            args.dist,
            args.crate,
            frozen,
            log=lambda line: print(line, flush=True),
        )
        return 0
    except (release.ReleaseError, destinations.TransientGitHubError, OSError, ValueError) as error:
        print(f"release publication: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
