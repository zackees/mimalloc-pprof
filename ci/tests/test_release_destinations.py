"""Failure injection for the issue-frozen release destination worker."""

from __future__ import annotations

import subprocess
import tempfile
import threading
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
        self.crate_error_after_write = False
        self.crate_publish_error: Exception | None = None
        self.freeze_failures = 0
        self.crate_validation_error: Exception | None = None

    def validate_crate(self, path: Path) -> None:
        if self.crate_validation_error is not None:
            raise self.crate_validation_error

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
        if self.freeze_failures:
            self.freeze_failures -= 1
            raise rd.TransientGitHubError("HTTP 503")
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
        if self.crate_publish_error is not None:
            raise self.crate_publish_error
        self.crate = rd.file_sha256(path)
        self.events.append("crate")
        if self.crate_error_after_write:
            self.crate_error_after_write = False
            raise rd.AmbiguousCratePublishError("response lost after registry accepted crate")

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
        self.crate = self.dist / "mimalloc-pprof-1.0.1.crate"
        self.crate.write_bytes(b"packed crate")
        self.info: dict[str, object] = {
            "artifacts": [
                {"name": name, "sha256": rd.file_sha256(self.dist / name)}
                for name in self.value["assets"]
            ],
            **self.value,
        }
        self.backend = FakeDestination()
        self.sleeps: list[float] = []

    def test_live_read_retries_empty_success_before_parsing(self) -> None:
        responses = [
            subprocess.CompletedProcess(["gh"], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(["gh"], 0, stdout="{}", stderr=""),
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout='{"object":{"type":"commit","sha":"' + "a" * 40 + '"}}',
                stderr="",
            ),
        ]
        with (
            patch.object(rd.subprocess, "run", side_effect=responses),
            patch.object(rd.time, "sleep") as sleep,
        ):
            observed = rd.ReadOnlyDestination().tag_sha("v1.0.1")
        self.assertEqual(observed, "a" * 40)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_live_read_classifies_http_status_before_message_text(self) -> None:
        permanent = subprocess.CompletedProcess(
            ["gh"], 1, stdout="", stderr="HTTP 403: connection timeout is forbidden"
        )
        with (
            patch.object(rd.subprocess, "run", return_value=permanent) as run,
            patch.object(rd.time, "sleep") as sleep,
            self.assertRaisesRegex(release.ReleaseError, "HTTP 403"),
        ):
            rd.ReadOnlyDestination().tag_sha("v1.0.1")
        run.assert_called_once()
        sleep.assert_not_called()

        transient = [
            subprocess.CompletedProcess(
                ["gh"], 1, stdout="", stderr="HTTP 500: upstream reported HTTP 404"
            ),
            subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="HTTP 404"),
        ]
        with (
            patch.object(rd.subprocess, "run", side_effect=transient),
            patch.object(rd.time, "sleep") as sleep,
        ):
            self.assertIsNone(rd.ReadOnlyDestination().tag_sha("v1.0.1"))
        sleep.assert_called_once_with(1)

        for stderr in ("HTTP 429: rate limited", "connection reset by peer"):
            with self.subTest(stderr=stderr):
                responses = [
                    subprocess.CompletedProcess(["gh"], 1, stdout="", stderr=stderr),
                    subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="HTTP 404"),
                ]
                with (
                    patch.object(rd.subprocess, "run", side_effect=responses),
                    patch.object(rd.time, "sleep") as sleep,
                ):
                    self.assertIsNone(rd.ReadOnlyDestination().tag_sha("v1.0.1"))
                sleep.assert_called_once_with(1)

    def test_live_read_rejects_unsupported_ref_type_without_retry(self) -> None:
        unsupported = subprocess.CompletedProcess(
            ["gh"],
            0,
            stdout='{"object":{"type":"blob","sha":"' + "a" * 40 + '"}}',
            stderr="",
        )
        with (
            patch.object(rd.subprocess, "run", return_value=unsupported) as run,
            patch.object(rd.time, "sleep") as sleep,
            self.assertRaisesRegex(release.ReleaseError, "does not resolve to a commit"),
        ):
            rd.ReadOnlyDestination().tag_sha("v1.0.1")
        run.assert_called_once()
        sleep.assert_not_called()

    def test_live_read_reports_persistent_malformed_success_with_context(self) -> None:
        malformed = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
        with (
            patch.object(rd.subprocess, "run", return_value=malformed),
            patch.object(rd.time, "sleep") as sleep,
            self.assertRaisesRegex(
                release.ReleaseError, "malformed JSON after 10 attempts.*git/ref/tags"
            ),
        ):
            rd.ReadOnlyDestination().tag_sha("v1.0.1")
        self.assertEqual(sleep.call_count, 9)

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
                sleep=self.sleeps.append,
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

    def test_issue_freeze_retries_transient_failures_before_tag(self) -> None:
        self.backend.freeze_failures = 9
        self.run_worker()
        self.assertEqual(self.backend.events[0], "freeze")
        self.assertEqual(self.backend.freeze_failures, 0)

    def test_permanent_error_fails_fast(self) -> None:
        self.backend.tag = "b" * 40
        with self.assertRaisesRegex(release.ReleaseError, "immutable tag"):
            self.run_worker()
        self.assertEqual(self.backend.events, [])

    def test_crate_metadata_failure_precedes_every_write(self) -> None:
        self.backend.crate_validation_error = release.ReleaseError("metadata mismatch")
        with self.assertRaisesRegex(release.ReleaseError, "metadata mismatch"):
            self.run_worker()
        self.assertEqual(self.backend.events, [])
        self.assertIsNone(self.backend.frozen)
        self.backend.tag = None
        self.backend.crate = "0" * 64
        with self.assertRaisesRegex(release.ReleaseError, "crates.io checksum"):
            self.run_worker()
        self.assertEqual(self.backend.events, [])

    def test_permanent_crate_rejection_does_not_poll_or_back_off(self) -> None:
        self.backend.crate_publish_error = release.ReleaseError(
            "crates.io upload returned HTTP 403"
        )
        with self.assertRaisesRegex(release.ReleaseError, "HTTP 403"):
            self.run_worker()
        self.assertEqual(self.sleeps, [])
        self.assertIsNone(self.backend.crate)

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

    def test_oversized_crate_fails_before_freeze(self) -> None:
        with self.crate.open("wb") as handle:
            handle.truncate(rd.MAX_CRATE_BYTES + 1)
        with self.assertRaisesRegex(release.ReleaseError, "10 MB"):
            self.run_worker()
        self.assertEqual(self.backend.events, [])

    def test_ambiguous_registry_acceptance_reconciles_before_finalize(self) -> None:
        self.backend.crate_error_after_write = True
        self.run_worker()
        self.assertEqual(self.backend.events.count("crate"), 1)
        self.assertFalse(self.backend.draft)

    def test_assets_and_crate_transfer_overlap(self) -> None:
        barrier = threading.Barrier(2, timeout=2)
        original_upload = self.backend.upload_asset
        original_crate = self.backend.publish_crate
        first_upload = True

        def upload(tag: str, name: str, path: Path) -> None:
            nonlocal first_upload
            if first_upload:
                first_upload = False
                barrier.wait()
            original_upload(tag, name, path)

        def publish(path: Path) -> None:
            barrier.wait()
            original_crate(path)

        self.backend.upload_asset = upload  # type: ignore[method-assign]
        self.backend.publish_crate = publish  # type: ignore[method-assign]
        self.run_worker()
        self.assertFalse(self.backend.draft)


if __name__ == "__main__":
    unittest.main()
