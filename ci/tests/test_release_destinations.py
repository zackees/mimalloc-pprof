"""Failure injection for the issue-frozen release destination worker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci import release
from ci import release_destinations as rd


class FakeDestination:
    def __init__(self) -> None:
        self.tag: str | None = None
        self.draft: bool | None = None
        self.assets: dict[str, str] = {}
        self.crate: str | None = None
        self.frozen: dict[str, object] | None = None
        self.events: list[str] = []
        self.upload_failures = 0
        self.ambiguous_writes: set[str] = set()
        self.freeze_visible = True
        self.release_target_is_tag = False

    def maybe_ambiguous(self, name: str) -> None:
        if name in self.ambiguous_writes:
            self.ambiguous_writes.remove(name)
            raise rd.TransientGitHubError("response lost after successful write")

    def tag_sha(self, tag: str) -> str | None:
        return self.tag

    def release(self, tag: str) -> rd.ReleaseState | None:
        return (
            rd.ReleaseState(
                tag if self.release_target_is_tag else self.tag or "",
                self.draft,
                self.assets.copy(),
            )
            if self.draft is not None
            else None
        )

    def crate_checksum(self, version: str) -> str | None:
        return self.crate

    def freeze(self, record: dict[str, object]) -> None:
        self.frozen = record
        self.events.append("freeze")

    def read_freeze(self) -> dict[str, object] | None:
        return self.frozen if self.freeze_visible else None

    def create_tag(self, tag: str, sha: str) -> None:
        self.tag = sha
        self.events.append("tag")
        self.maybe_ambiguous("tag")

    def create_draft(self, tag: str, sha: str) -> None:
        self.draft = True
        self.events.append("draft")
        self.maybe_ambiguous("draft")

    def upload_asset(self, tag: str, name: str, path: Path) -> None:
        if self.upload_failures:
            self.upload_failures -= 1
            raise rd.TransientGitHubError("HTTP 503")
        self.assets[name] = rd.file_sha256(path)
        self.events.append(f"asset:{name}")
        self.maybe_ambiguous(f"asset:{name}")

    def publish_crate(self, path: Path) -> None:
        self.crate = rd.file_sha256(path)
        self.events.append("crate")

    def finalize(self, tag: str) -> None:
        self.draft = False
        self.events.append("finalize")
        self.maybe_ambiguous("finalize")


class DestinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        self.value = release.directive(444, "1.0.1", "a" * 40)
        for name in self.value["assets"]:
            (self.dist / name).write_bytes(name.encode())
        self.crate = self.dist / "package.crate"
        self.crate.write_bytes(b"packed crate")
        self.info: dict[str, object] = {
            "artifacts": [
                {"name": name, "sha256": rd.file_sha256(self.dist / name)}
                for name in self.value["assets"]
            ],
            **self.value,
        }
        self.backend = FakeDestination()

    def run_worker(self, frozen: dict[str, object] | None = None) -> None:
        with patch.object(release, "verify_info"):
            rd.execute(
                self.backend,
                self.value,
                self.info,
                self.dist,
                self.crate,
                frozen,
                log=lambda _: None,
                sleep=lambda _: None,
            )

    def test_freeze_precedes_writes_and_partial_resume(self) -> None:
        self.backend.upload_failures = 10
        with self.assertRaises(rd.TransientGitHubError):
            self.run_worker()
        self.assertEqual(self.backend.events[:3], ["freeze", "tag", "draft"])
        self.assertIsNotNone(self.backend.frozen)
        self.run_worker(self.backend.frozen)
        self.assertEqual(self.backend.events.count("tag"), 1)
        self.assertEqual(self.backend.events.count("crate"), 1)
        self.assertFalse(self.backend.draft)

    def test_transient_retry_then_success(self) -> None:
        self.backend.upload_failures = 9
        self.run_worker()
        self.assertEqual(len(self.backend.assets), 5)

    def test_permanent_error_fails_fast(self) -> None:
        self.backend.tag = "b" * 40
        with self.assertRaisesRegex(release.ReleaseError, "immutable tag"):
            self.run_worker()
        self.assertEqual(self.backend.events, [])
        self.backend.tag = None
        self.backend.crate = "0" * 64
        with self.assertRaisesRegex(release.ReleaseError, "crates.io checksum"):
            self.run_worker()
        self.assertEqual(self.backend.events, [])

    def test_crate_success_then_github_resume(self) -> None:
        self.backend.crate = rd.file_sha256(self.crate)
        self.run_worker()
        self.assertNotIn("crate", self.backend.events)
        self.assertIn("finalize", self.backend.events)

    def test_freeze_must_be_visible_before_first_destination_write(self) -> None:
        self.backend.freeze_visible = False
        with self.assertRaisesRegex(release.ReleaseError, "freeze readback"):
            self.run_worker()
        self.assertEqual(self.backend.events, ["freeze"])

    def test_release_targeting_resolved_tag_can_resume(self) -> None:
        self.run_worker()
        self.backend.release_target_is_tag = True
        self.run_worker(self.backend.frozen)
        self.assertEqual(self.backend.events.count("tag"), 1)
        self.backend.tag = "b" * 40
        with self.assertRaisesRegex(release.ReleaseError, "immutable tag"):
            self.run_worker(self.backend.frozen)

    def test_lost_success_responses_are_verified_without_duplicate_writes(self) -> None:
        self.backend.ambiguous_writes = {
            "tag",
            "draft",
            f"asset:{self.value['assets'][0]}",
            "finalize",
        }
        self.run_worker()
        self.assertEqual(self.backend.events.count("tag"), 1)
        self.assertEqual(self.backend.events.count("draft"), 1)
        self.assertEqual(self.backend.events.count("finalize"), 1)
        self.assertEqual(self.backend.events.count(f"asset:{self.value['assets'][0]}"), 1)

    def test_annotated_tag_resolves_to_commit(self) -> None:
        ref = '{"object":{"type":"tag","sha":"' + "b" * 40 + '"}}'
        tag_obj = '{"object":{"type":"commit","sha":"' + "a" * 40 + '"}}'
        with (
            patch.object(rd.ReadOnlyDestination, "_gh_optional", return_value=ref),
            patch.object(rd.ReadOnlyDestination, "_gh_required", return_value=tag_obj),
        ):
            self.assertEqual(rd.ReadOnlyDestination().tag_sha("v1.0.1"), "a" * 40)


if __name__ == "__main__":
    unittest.main()
