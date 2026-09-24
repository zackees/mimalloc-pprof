"""Issue-frozen release destination planner and resumable publisher."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, cast

from ci import release


class TransientGitHubError(Exception):
    """A retryable transport or GitHub server failure (not a conflict)."""


class AmbiguousCratePublishError(release.ReleaseError):
    """The registry may have accepted the crate before the response was lost."""


MAX_CRATE_BYTES = 10_000_000  # crates.io's compressed .crate upload limit


class ReadableDestination(Protocol):
    def tag_sha(self, tag: str) -> str | None: ...
    def release(self, tag: str) -> ReleaseState | None: ...
    def crate_checksum(self, version: str) -> str | None: ...


class Destination(ReadableDestination, Protocol):
    def validate_crate(self, path: Path) -> None: ...
    def freeze(self, record: dict[str, object]) -> None: ...
    def read_freeze(self) -> dict[str, object] | None: ...
    def create_tag(self, tag: str, sha: str) -> None: ...
    def create_draft(self, tag: str, sha: str) -> None: ...
    def upload_asset(self, tag: str, name: str, path: Path) -> None: ...
    def publish_crate(self, path: Path) -> None: ...
    def finalize(self, tag: str) -> None: ...


@dataclass(frozen=True)
class ReleaseState:
    target_sha: str
    draft: bool
    assets: dict[str, str]


@dataclass(frozen=True)
class Plan:
    freeze: dict[str, object]
    missing_tag: bool
    missing_release: bool
    missing_assets: tuple[str, ...]
    missing_crate: bool
    finalize: bool


class ReadOnlyDestination:
    """Live destination reads for dry preflight; write methods are absent."""

    def tag_sha(self, tag: str) -> str | None:
        def object_shape(value: dict[str, object]) -> bool:
            raw_object = value.get("object")
            if not isinstance(raw_object, dict):
                return False
            obj = cast(dict[str, object], raw_object)
            return isinstance(obj.get("type"), str) and isinstance(obj.get("sha"), str)

        raw = self._gh_optional(f"repos/{release.REPO}/git/ref/tags/{tag}", validate=object_shape)
        if raw is None:
            return None
        data = json.loads(raw)
        obj = data["object"]
        for _ in range(10):
            if obj["type"] == "commit":
                return str(obj["sha"])
            if obj["type"] != "tag":
                raise release.ReleaseError("release tag does not resolve to a commit")
            obj = json.loads(
                self._gh_required(
                    f"repos/{release.REPO}/git/tags/{obj['sha']}", validate=object_shape
                )
            )["object"]
        raise release.ReleaseError("release tag indirection exceeds ten levels")

    def release(self, tag: str) -> ReleaseState | None:
        def asset_shape(value: object) -> bool:
            if not isinstance(value, dict):
                return False
            asset = cast(dict[str, object], value)
            return isinstance(asset.get("name"), str) and isinstance(asset.get("digest"), str)

        def release_shape(value: dict[str, object]) -> bool:
            return (
                isinstance(value.get("target_commitish"), str)
                and isinstance(value.get("draft"), bool)
                and isinstance(value.get("assets"), list)
                and all(asset_shape(item) for item in cast(list[object], value.get("assets")))
            )

        raw = self._gh_optional(f"repos/{release.REPO}/releases/tags/{tag}", validate=release_shape)
        if raw is None:
            return None
        data = json.loads(raw)
        assets = {
            str(item["name"]): str(item["digest"]).removeprefix("sha256:")
            for item in data["assets"]
        }
        return ReleaseState(str(data["target_commitish"]), bool(data["draft"]), assets)

    @staticmethod
    def _gh_optional(
        endpoint: str,
        *,
        validate: Callable[[dict[str, object]], bool] = lambda _: True,
    ) -> str | None:
        for attempt in range(10):
            result = subprocess.run(
                ["gh", "api", endpoint], capture_output=True, text=True, check=False
            )
            if result.returncode == 0:
                try:
                    value = json.loads(result.stdout)
                    if not isinstance(value, dict) or not validate(cast(dict[str, object], value)):
                        raise json.JSONDecodeError(
                            "GitHub object response is not an object", result.stdout, 0
                        )
                    return result.stdout
                except json.JSONDecodeError as error:
                    if attempt == 9:
                        raise release.ReleaseError(
                            f"GitHub read returned malformed JSON after 10 attempts: {endpoint}"
                        ) from error
                    time.sleep(min(2**attempt, 30))
                    continue
            status_match = re.search(r"\bHTTP ([0-9]{3})\b", result.stderr)
            status = int(status_match.group(1)) if status_match else None
            if status == 404:
                return None
            retryable = status == 429 or (status is not None and status >= 500)
            if status is None:
                retryable = bool(
                    re.search(
                        r"(?:connection|timeout|EOF|broken pipe|EPIPE)",
                        result.stderr,
                        re.I,
                    )
                )
            if not retryable:
                raise release.ReleaseError(f"GitHub read failed: {result.stderr.strip()}")
            if attempt == 9:
                raise release.ReleaseError(
                    f"GitHub read failed after 10 attempts: {result.stderr.strip()}"
                )
            time.sleep(min(2**attempt, 30))
        raise AssertionError("unreachable GitHub retry loop")

    @classmethod
    def _gh_required(
        cls,
        endpoint: str,
        *,
        validate: Callable[[dict[str, object]], bool] = lambda _: True,
    ) -> str:
        raw = cls._gh_optional(endpoint, validate=validate)
        if raw is None:
            raise release.ReleaseError("release tag object disappeared during verification")
        return raw

    def crate_checksum(self, version: str) -> str | None:
        url = f"https://crates.io/api/v1/crates/mimalloc-pprof/{version}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "mimalloc-pprof dry preflight"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise release.ReleaseError(f"crates.io returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise release.ReleaseError(f"crates.io read failed: {error}") from error
        if data.get("version", {}).get("yanked"):
            raise release.ReleaseError("crates.io version is yanked")
        return str(data["version"]["checksum"])


def dry_preflight_main() -> int:
    parser = argparse.ArgumentParser(description="Read-only release destination preflight")
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--crate", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = release.directive(args.issue, release.source_version(), args.candidate_sha)
        records = release.issue_directives(args.issue)
        frozen = release.frozen_identity(args.issue)
        if not records:
            raise release.ReleaseError("issue has no release directive")
        release.require_history(records, value, frozen)
        info = json.loads((args.dist / "info.json").read_text())
        plan = preflight(ReadOnlyDestination(), value, info, args.dist, args.crate, frozen)
        print(
            json.dumps(
                {
                    "dry_preflight": "passed",
                    "missing_tag": plan.missing_tag,
                    "missing_release": plan.missing_release,
                    "missing_assets": plan.missing_assets,
                    "missing_crate": plan.missing_crate,
                    "freeze": plan.freeze,
                },
                indent=2,
            )
        )
        return 0
    except (release.ReleaseError, OSError, json.JSONDecodeError) as error:
        print(f"release dry preflight: {error}", file=sys.stderr)
        return 1


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight(
    destination: ReadableDestination,
    directive: dict[str, object],
    info: dict[str, object],
    dist: Path,
    crate: Path,
    frozen: dict[str, object] | None,
) -> Plan:
    """Inspect every destination and byte before any write, including a freeze."""
    release.verify_info(dist, directive, info)
    version = str(directive["version"])
    if crate.name != f"mimalloc-pprof-{version}.crate" or crate.is_symlink():
        raise release.ReleaseError("packaged crate name or file type differs from directive")
    crate_size = crate.stat().st_size
    if crate_size <= 0 or crate_size > MAX_CRATE_BYTES:
        raise release.ReleaseError("packaged crate exceeds crates.io 10 MB limit or is empty")
    crate_hash = file_sha256(crate)
    record = release.freeze_record(directive, info, crate_hash)
    if frozen is not None and frozen != record:
        raise release.ReleaseError("issue freeze differs from exact packaged bytes")
    tag, sha = (str(directive[key]) for key in ("tag", "candidate_sha"))
    existing_tag = destination.tag_sha(tag)
    if existing_tag is not None and existing_tag != sha:
        raise release.ReleaseError("immutable tag points to another candidate")
    existing_release = destination.release(tag)
    if existing_release is not None and not (
        existing_release.target_sha == sha
        or (existing_release.target_sha == tag and existing_tag == sha)
    ):
        raise release.ReleaseError("GitHub Release targets another candidate")
    expected = record["asset_sha256"]
    assert isinstance(expected, dict)
    assets: dict[str, str] = existing_release.assets if existing_release else {}
    for name, digest in assets.items():
        if name not in expected or expected[name] != digest:
            raise release.ReleaseError(f"GitHub asset conflict: {name}")
    existing_crate = destination.crate_checksum(version)
    if existing_crate is not None and existing_crate != crate_hash:
        raise release.ReleaseError("crates.io checksum conflicts with packaged crate")
    declared_assets = cast(list[str], directive["assets"])
    missing = tuple(name for name in declared_assets if name not in assets)
    if (
        existing_release is not None
        and not existing_release.draft
        and (missing or existing_crate is None)
    ):
        raise release.ReleaseError("published GitHub Release is incomplete")
    return Plan(
        record,
        existing_tag is None,
        existing_release is None,
        missing,
        existing_crate is None,
        existing_release is None or existing_release.draft,
    )


def github_retry(
    operation: Callable[[], None],
    *,
    log: Callable[[str], None],
    completed: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Retry only explicitly transient GitHub failures, at most ten attempts."""
    for attempt in range(1, 11):
        try:
            operation()
            return
        except release.ReleaseError:
            # A second attempt may report 422 because the first request committed
            # while its response was lost. Reconcile exact state before failing.
            if completed is not None and completed():
                log("github: verified write after conflict response")
                return
            raise
        except TransientGitHubError as error:
            log(f"github transient attempt={attempt}/10: {error}")
            # The server may have committed the write before the response failed.
            if completed is not None and completed():
                log("github: verified write after ambiguous response")
                return
            if attempt == 10:
                raise
            sleep(min(2 ** (attempt - 1), 30))


def execute(
    destination: Destination,
    directive: dict[str, object],
    info: dict[str, object],
    dist: Path,
    crate: Path,
    frozen: dict[str, object] | None,
    *,
    log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Freeze, transfer from one runner, verify all bytes, then publish the draft."""
    plan = preflight(destination, directive, info, dist, crate, frozen)
    destination.validate_crate(crate)
    tag, sha = str(directive["tag"]), str(directive["candidate_sha"])
    expected_assets = cast(dict[str, str], plan.freeze["asset_sha256"])

    def tag_done() -> bool:
        observed = destination.tag_sha(tag)
        if observed is not None and observed != sha:
            raise release.ReleaseError("immutable tag points to another candidate")
        return observed == sha

    def release_done(*, draft: bool) -> bool:
        observed = destination.release(tag)
        if observed is None:
            return False
        if not (
            observed.target_sha == sha
            or (observed.target_sha == tag and destination.tag_sha(tag) == sha)
        ):
            raise release.ReleaseError("GitHub Release targets another candidate")
        return observed.draft == draft

    def asset_done(name: str) -> bool:
        observed = destination.release(tag)
        if observed is None:
            return False
        if not (
            observed.target_sha == sha
            or (observed.target_sha == tag and destination.tag_sha(tag) == sha)
        ):
            raise release.ReleaseError("GitHub Release targets another candidate")
        digest = observed.assets.get(name)
        if digest is not None and digest != expected_assets[name]:
            raise release.ReleaseError(f"GitHub asset conflict: {name}")
        return digest == expected_assets[name]

    if frozen is None:
        github_retry(
            lambda: destination.freeze(plan.freeze),
            log=log,
            completed=lambda: destination.read_freeze() == plan.freeze,
            sleep=sleep,
        )
    authoritative_freeze = destination.read_freeze()
    if authoritative_freeze != plan.freeze:
        raise release.ReleaseError("issue freeze readback differs from packaged release identity")
    log("issue: verified frozen directive, info.json, asset hashes, and crate hash")
    if plan.missing_tag:
        github_retry(
            lambda: destination.create_tag(tag, sha),
            log=log,
            completed=tag_done,
            sleep=sleep,
        )
        log("github: tag created")
    if plan.missing_release:
        github_retry(
            lambda: destination.create_draft(tag, sha),
            log=log,
            completed=lambda: release_done(draft=True),
            sleep=sleep,
        )
        log("github: draft created")
    # The two network destinations transfer concurrently. Workers buffer their logs;
    # the job prints each destination's complete transcript in a fixed order.
    github_lines: list[str] = []
    crate_lines: list[str] = []

    def transfer_assets() -> None:
        for name in plan.missing_assets:
            github_retry(
                lambda name=name: destination.upload_asset(tag, name, dist / name),
                log=github_lines.append,
                completed=lambda name=name: asset_done(name),
                sleep=sleep,
            )
            github_lines.append(f"github: asset verified {name}")

    def transfer_crate() -> None:
        if plan.missing_crate:
            try:
                destination.publish_crate(crate)
            except AmbiguousCratePublishError:
                # Cargo may lose its response after crates.io accepted the upload.
                # A matching registry checksum is the only safe success signal.
                for attempt in range(10):
                    if (
                        destination.crate_checksum(str(directive["version"]))
                        == plan.freeze["crate_sha256"]
                    ):
                        crate_lines.append("crates.io: accepted despite ambiguous response")
                        break
                    if attempt == 9:
                        raise
                    sleep(min(2**attempt, 30))
            crate_lines.append("crates.io: publish attempted")

    log("release: GitHub assets and crates.io transfers started on one worker")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        github_transfer = pool.submit(transfer_assets)
        crate_transfer = pool.submit(transfer_crate)
        failures: list[Exception] = []
        for label, future, lines in (
            ("github", github_transfer, github_lines),
            ("crates.io", crate_transfer, crate_lines),
        ):
            try:
                future.result()
            except Exception as error:
                log(f"{label}: transfer failed: {error}")
                failures.append(error)
            for line in lines:
                log(line)
        if failures:
            raise failures[0]
    # Re-read authoritative destinations after potentially ambiguous writes.
    complete = preflight(destination, directive, info, dist, crate, plan.freeze)
    for attempt in range(9):
        if not complete.missing_crate:
            break
        sleep(min(2**attempt, 30))
        complete = preflight(destination, directive, info, dist, crate, plan.freeze)
    if (
        complete.missing_tag
        or complete.missing_release
        or complete.missing_assets
        or complete.missing_crate
    ):
        raise release.ReleaseError("destination verification incomplete; resume with same freeze")
    if complete.finalize:
        github_retry(
            lambda: destination.finalize(tag),
            log=log,
            completed=lambda: release_done(draft=False),
            sleep=sleep,
        )
    final = preflight(destination, directive, info, dist, crate, plan.freeze)
    if (
        final.missing_tag
        or final.missing_release
        or final.missing_assets
        or final.missing_crate
        or final.finalize
    ):
        raise release.ReleaseError("final destination verification incomplete")
    log("release: complete")


if __name__ == "__main__":
    raise SystemExit(dry_preflight_main())
