"""Release attempt identity and preflight regressions for issue #444."""

from __future__ import annotations

import json
import stat
import struct
import tarfile
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from ci import release

SHA = "a" * 40
BUMP = "b" * 40
PARENT = "c" * 40


class ReleaseFrontdoorTests(unittest.TestCase):
    def test_github_json_retries_empty_and_schema_invalid_success(self) -> None:
        sleeps: list[float] = []
        with patch.object(
            release,
            "command",
            side_effect=["", "[]", '{"state":"OPEN"}'],
        ):
            value = release.github_json(
                "gh",
                "issue",
                "view",
                validate=lambda result: release.json_object_with_string_fields(result, ("state",)),
                sleep=sleeps.append,
            )
        self.assertEqual(value, {"state": "OPEN"})
        self.assertEqual(sleeps, [1, 2])

    def test_github_json_bounds_malformed_success_retries(self) -> None:
        sleeps: list[float] = []
        with (
            patch.object(release, "command", return_value=""),
            self.assertRaisesRegex(release.ReleaseError, "after 10 attempts"),
        ):
            release.github_json(
                "gh",
                "issue",
                "view",
                validate=lambda result: isinstance(result, dict),
                sleep=sleeps.append,
            )
        self.assertEqual(sleeps, [1, 2, 4, 8, 16, 30, 30, 30, 30])

    def test_github_json_does_not_retry_command_failure(self) -> None:
        sleeps: list[float] = []
        with (
            patch.object(release, "command", side_effect=release.ReleaseError("auth failed")),
            self.assertRaisesRegex(release.ReleaseError, "auth failed"),
        ):
            release.github_json(
                "gh",
                "issue",
                "view",
                validate=lambda result: isinstance(result, dict),
                sleep=sleeps.append,
            )
        self.assertEqual(sleeps, [])

    def test_release_list_shape_validates_fields_used_by_preflight(self) -> None:
        self.assertTrue(
            release.release_list_shape(
                [{"tag_name": "v1.0.1", "assets": [{"name": "asset", "digest": "sha256:a"}]}]
            )
        )
        self.assertFalse(release.release_list_shape([{"tag_name": "v1.0.1"}]))
        self.assertFalse(release.release_list_shape([{"tag_name": "v1.0.1", "assets": [None]}]))

    def test_issue_comment_read_retries_empty_success_response(self) -> None:
        sleeps: list[float] = []
        response = json.dumps(
            [{"body": "trusted", "author_association": "OWNER", "login": "owner"}]
        )
        with patch.object(release, "command", side_effect=["", response]):
            self.assertEqual(release.issue_comments(444, sleep=sleeps.append), ["trusted"])
        self.assertEqual(sleeps, [1])

    def test_issue_comment_read_accepts_paginated_json_lines_from_gh(self) -> None:
        response = "\n".join(
            json.dumps(page)
            for page in (
                [{"body": "owner\nbody", "author_association": "OWNER", "login": "owner"}],
                [
                    {
                        "body": "automation",
                        "author_association": "NONE",
                        "login": "github-actions[bot]",
                    },
                    {"body": "untrusted", "author_association": "NONE", "login": "outsider"},
                ],
            )
        )
        with patch.object(release, "command", return_value=response) as run:
            self.assertEqual(release.issue_comments(444), ["owner\nbody", "automation"])
        self.assertIn("| @json", run.call_args.args[-1])

    def test_issue_comment_read_accepts_valid_empty_page(self) -> None:
        with patch.object(release, "command", return_value="[]"):
            self.assertEqual(release.issue_comments(444), [])

    def test_issue_comment_read_bounds_malformed_response_retries(self) -> None:
        sleeps: list[float] = []
        with (
            patch.object(release, "command", return_value=""),
            self.assertRaisesRegex(release.ReleaseError, "after 10 attempts"),
        ):
            release.issue_comments(444, sleep=sleeps.append)
        self.assertEqual(sleeps, [1, 2, 4, 8, 16, 30, 30, 30, 30])
        self.assertEqual(release.json_lines_shape(""), "empty")
        self.assertEqual(release.json_lines_shape("<html>"), "ndjson/0-of-1-lines/6-bytes")
        self.assertEqual(
            release.json_lines_shape('{"body":"ok"}\nnot-json'),
            "ndjson/1-of-2-lines/22-bytes",
        )

    def test_issue_comment_read_retries_malformed_rows(self) -> None:
        sleeps: list[float] = []
        response = json.dumps(
            [{"body": "trusted", "author_association": "OWNER", "login": "owner"}]
        )
        with patch.object(release, "command", side_effect=["null", response]):
            self.assertEqual(release.issue_comments(444, sleep=sleeps.append), ["trusted"])
        self.assertEqual(sleeps, [1])

    def test_issue_comment_read_does_not_retry_command_failure(self) -> None:
        sleeps: list[float] = []
        with (
            patch.object(release, "command", side_effect=release.ReleaseError("auth failed")),
            self.assertRaisesRegex(release.ReleaseError, "auth failed"),
        ):
            release.issue_comments(444, sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_source_version_requires_matching_lockfile(self) -> None:
        self.assertEqual(release.source_version(), "1.0.1")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crate = root / "rust/mimalloc-pprof"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").write_text(
                '[package]\nname = "mimalloc-pprof"\nversion = "1.0.1"\n'
            )
            (root / "rust/Cargo.lock").write_text(
                '[[package]]\nname = "mimalloc-pprof"\nversion = "1.0.0"\n'
            )
            with patch.object(release, "ROOT", root), self.assertRaises(release.ReleaseError):
                release.source_version()

    def test_directive_is_exact_and_full(self) -> None:
        directive = release.directive(444, "1.0.1", SHA)
        self.assertEqual(directive["tag"], "v1.0.1")
        self.assertEqual(directive["candidate_sha"], SHA)
        self.assertEqual(directive["mode"], "full")
        self.assertEqual(len(directive["assets"]), 5)
        with self.assertRaises(release.ReleaseError):
            release.directive(444, "1.0.1", "a" * 7)

    def test_existing_directive_cannot_be_retargeted(self) -> None:
        original = release.directive(444, "1.0.1", SHA)
        release.require_same_directive(original, original)
        with self.assertRaises(release.ReleaseError):
            release.require_same_directive(original, release.directive(444, "1.0.1", "b" * 40))

    def test_artifact_preflight_rejects_missing_oversize_and_hash_mismatch(self) -> None:
        directive = release.directive(444, "1.0.1", SHA)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in directive["assets"]:
                path = directory / name
                if name.startswith("mimalloc-pprof-c-"):
                    with zipfile.ZipFile(path, "w") as archive:
                        for member in (
                            "mimalloc-pprof-amalgamated.c",
                            "mimalloc-pprof-amalgamated.h",
                            "mimalloc.h",
                            "mimalloc-stats.h",
                            "README.md",
                        ):
                            archive.write(
                                release.ROOT / "rust/mimalloc-pprof/vendor" / member, member
                            )
                    continue
                asset = next(key for key in release.TARGETS if f"-{key}-" in name)
                provenance = f"mimalloc-pprof {SHA} -- {asset}\n\ncommit:   {SHA}\ntarget:   {release.TARGETS[asset]}\n".encode()
                if asset.startswith("macos"):
                    cpu = 0x0100000C if asset == "macos-arm64" else 0x01000007
                    members = {
                        "PROVENANCE.txt": provenance,
                        "lib/libmimalloc.3.1.dylib": b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu),
                    }
                    with tarfile.open(path, "w:gz") as archive:
                        for member, data in members.items():
                            entry = tarfile.TarInfo(member)
                            entry.size = len(data)
                            archive.addfile(entry, BytesIO(data))
                else:
                    pe = bytearray(0x86)
                    pe[:2] = b"MZ"
                    pe[0x3C:0x40] = struct.pack("<I", 0x80)
                    pe[0x80:0x86] = b"PE\0\0\x64\x86"
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("PROVENANCE.txt", provenance)
                        archive.writestr("bin/mimalloc.dll", pe)
                        archive.writestr("bin/mimalloc-redirect.dll", pe)
                        if asset == "windows-x64-gnu":
                            archive.writestr("bin/libgcc_s_seh-1.dll", pe)
            info = release.inspect_artifacts(directory, directive)
            self.assertEqual(info["candidate_sha"], SHA)
            self.assertEqual(len(info["artifacts"]), 5)
            self.assertEqual(info["artifacts"][1]["validated"]["target"], "aarch64-apple-darwin")
            release.verify_info(directory, directive, info)
            info["artifacts"][0]["sha256"] = "0" * 64
            with self.assertRaises(release.ReleaseError):
                release.verify_info(directory, directive, info)
            with (
                patch.object(release, "MAX_ASSET_BYTES", 1),
                self.assertRaises(release.ReleaseError),
            ):
                release.inspect_artifacts(directory, directive)
            (directory / directive["assets"][0]).unlink()
            with self.assertRaises(release.ReleaseError):
                release.inspect_artifacts(directory, directive)

    def test_archives_reject_wrong_target_and_provenance(self) -> None:
        value = release.directive(444, "1.0.1", SHA)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / value["assets"][1]
            with tarfile.open(path, "w:gz") as archive:
                for name, data in {
                    "PROVENANCE.txt": f"mimalloc-pprof {'b' * 40} -- macos-arm64\ncommit:   {'b' * 40}\ntarget:   aarch64-apple-darwin\n".encode(),
                    "lib/libmimalloc.3.1.dylib": b"\xcf\xfa\xed\xfe"
                    + struct.pack("<I", 0x0100000C),
                }.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    archive.addfile(entry, BytesIO(data))
            with self.assertRaisesRegex(release.ReleaseError, "provenance"):
                release.inspect_archive(path, value)
            with tarfile.open(path, "w:gz") as archive:
                members = {
                    "PROVENANCE.txt": f"mimalloc-pprof {SHA} -- macos-arm64\ncommit:   {SHA}\ntarget:   aarch64-apple-darwin\n".encode(),
                    "lib/libmimalloc.3.1.dylib": b"\xcf\xfa\xed\xfe"
                    + struct.pack("<I", 0x01000007),
                }
                for name, data in members.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    archive.addfile(entry, BytesIO(data))
            with self.assertRaisesRegex(release.ReleaseError, "Mach-O target"):
                release.inspect_archive(path, value)

    def test_archive_member_paths_and_symlink_targets_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rooted_tar = root / "rooted.tar.gz"
            with tarfile.open(rooted_tar, "w:gz") as archive:
                root_entry = tarfile.TarInfo(".")
                root_entry.type = tarfile.DIRTYPE
                archive.addfile(root_entry)
                data = b"safe"
                file_entry = tarfile.TarInfo("./safe.txt")
                file_entry.size = len(data)
                archive.addfile(file_entry, BytesIO(data))
            self.assertEqual(release.archive_members(rooted_tar), {"safe.txt": b"safe"})
            with tarfile.open(rooted_tar, "w:gz") as archive:
                root_file = tarfile.TarInfo(".")
                root_file.size = 1
                archive.addfile(root_file, BytesIO(b"x"))
            with self.assertRaisesRegex(release.ReleaseError, "unsafe archive member"):
                release.archive_members(rooted_tar)
            zip_path = root / "bad.zip"
            for member in ("C:/escape", "dir\\escape", "../escape"):
                with zipfile.ZipFile(zip_path, "w") as archive:
                    archive.writestr(member, b"x")
                with self.assertRaisesRegex(release.ReleaseError, "unsafe archive member"):
                    release.archive_members(zip_path)
            tar_path = root / "bad.tar.gz"
            for target in ("../../escape", "C:/escape", "\\escape", "missing"):
                with tarfile.open(tar_path, "w:gz") as archive:
                    entry = tarfile.TarInfo("lib/link.dylib")
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = target
                    archive.addfile(entry)
                with self.assertRaises(release.ReleaseError):
                    release.archive_members(tar_path)
            with tarfile.open(tar_path, "w:gz") as archive:
                data = b"mach-o"
                real = tarfile.TarInfo("lib/real.dylib")
                real.size = len(data)
                archive.addfile(real, BytesIO(data))
                link = tarfile.TarInfo("lib/link.dylib")
                link.type = tarfile.SYMTYPE
                link.linkname = "real.dylib"
                archive.addfile(link)
            self.assertIn("lib/link.dylib", release.archive_members(tar_path))

    def test_zip_member_types_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "asset.zip"
            for label, mode in (
                ("symlink", stat.S_IFLNK),
                ("character device", stat.S_IFCHR),
                ("block device", stat.S_IFBLK),
                ("fifo", stat.S_IFIFO),
                ("socket", stat.S_IFSOCK),
                ("unknown", 0o150000),
            ):
                with self.subTest(label=label):
                    member = zipfile.ZipInfo("bin/unsafe")
                    member.create_system = 3
                    member.external_attr = (mode | 0o644) << 16
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(member, b"payload")
                    with self.assertRaisesRegex(release.ReleaseError, "invalid archive member"):
                        release.archive_members(path)
            for name in ("bin/duplicate", "bin/duplicate/"):
                with self.subTest(duplicate=name):
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(name, b"" if name.endswith("/") else b"first")
                        archive.writestr(name, b"" if name.endswith("/") else b"second")
                    with self.assertRaisesRegex(release.ReleaseError, "duplicate archive member"):
                        release.archive_members(path)
            native = Path(temporary) / "native"
            native.write_bytes(b"native")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("bin/plain", b"plain")
                archive.write(native, "bin/native")
                unix_file = zipfile.ZipInfo("bin/unix")
                unix_file.create_system = 3
                unix_file.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(unix_file, b"unix")
                unix_dir = zipfile.ZipInfo("bin/dir/")
                unix_dir.create_system = 3
                unix_dir.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(unix_dir, b"")
                dos_file = zipfile.ZipInfo("bin/dos")
                dos_file.create_system = 0
                dos_file.external_attr = 0x20
                archive.writestr(dos_file, b"dos")
            self.assertEqual(
                release.archive_members(path),
                {
                    "bin/plain": b"plain",
                    "bin/native": b"native",
                    "bin/unix": b"unix",
                    "bin/dos": b"dos",
                },
            )

    def test_candidate_requires_recorded_version_bump_merge(self) -> None:
        self.assertEqual(release.recorded_merge_sha(f"- Candidate merge SHA: **{SHA}**"), SHA)
        self.assertEqual(release.recorded_version_bump_pr("- Version-bump PR: #999"), 999)
        for body in (
            "- Candidate merge SHA: **pending**",
            "",
            f"- Candidate merge SHA: **{SHA}**\n- Candidate merge SHA: **{SHA}**",
        ):
            with self.assertRaises(release.ReleaseError):
                release.recorded_merge_sha(body)
        with self.assertRaises(release.ReleaseError):
            release.recorded_version_bump_pr("- Version-bump PR: pending")

    def test_candidate_accepts_later_reviewed_merge_and_rejects_other_pr(self) -> None:
        value = release.directive(444, "1.0.1", SHA)
        responses = {
            "issue": json.dumps(
                {
                    "state": "OPEN",
                    "title": "release v1.0.1",
                    "body": f"- Candidate merge SHA: **{SHA}**\n- Candidate PR: #1000\n- Version-bump merge SHA: **{BUMP}**\n- Version-bump PR: #999",
                }
            ),
            "HEAD": SHA,
            "parents": f"{BUMP} {PARENT}",
            "previous": '[package]\nversion = "1.0.0"\n',
            "bumped": '[package]\nversion = "1.0.1"\n',
        }

        def fake_command(*args: str) -> str:
            if args[:3] == ("gh", "issue", "view"):
                return responses["issue"]
            if args[:3] == ("gh", "pr", "view"):
                oid = BUMP if args[3] == "999" else SHA
                return json.dumps(
                    {"baseRefName": "main", "mergedAt": "now", "mergeCommit": {"oid": oid}}
                )
            if args[:3] == ("git", "rev-parse", "HEAD"):
                return responses["HEAD"]
            if args[:3] == ("git", "rev-list", "--parents"):
                return responses["parents"]
            if args[:2] == ("git", "show"):
                return responses["previous"] if args[2].startswith(PARENT) else responses["bumped"]
            return ""

        with (
            patch.object(release, "command", side_effect=fake_command),
            patch.object(release, "source_version", return_value="1.0.1"),
            patch.object(release.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            release.validate_candidate(value, require_registry_free=False)
            with self.assertRaisesRegex(release.ReleaseError, "ready-to-publish"):
                release.validate_candidate(
                    value, require_registry_free=False, require_issue_ready=True
                )
            issue_record = json.loads(responses["issue"])
            issue_record["body"] = "- State: **ready-to-publish**.\n" + issue_record["body"]
            responses["issue"] = json.dumps(issue_record)
            release.validate_candidate(value, require_registry_free=False, require_issue_ready=True)
            responses["issue"] = responses["issue"].replace("#1000", "#999")
            with self.assertRaisesRegex(release.ReleaseError, "candidate differs"):
                release.validate_candidate(value, require_registry_free=False)

    def test_prewrite_retarget_and_postwrite_freeze(self) -> None:
        old = release.directive(444, "1.0.1", BUMP)
        new = release.directive(444, "1.0.1", SHA)
        release.require_history([old, new], new, None)
        info = {**new, "artifacts": [{"name": name, "sha256": "d" * 64} for name in new["assets"]]}
        frozen = release.freeze_record(new, info, "f" * 64)
        release.require_history([old, new], new, frozen)
        release.require_frozen_info(frozen, new, info)
        with self.assertRaisesRegex(release.ReleaseError, "frozen"):
            release.require_history([old, new], old, frozen)
        info["artifacts"][0]["sha256"] = "e" * 64
        with self.assertRaisesRegex(release.ReleaseError, "frozen"):
            release.require_frozen_info(frozen, new, info)

    def test_partial_github_assets_resume_and_conflict(self) -> None:
        value = release.directive(444, "1.0.1", SHA)
        info = {
            **value,
            "artifacts": [{"name": name, "sha256": "d" * 64} for name in value["assets"]],
        }
        frozen = release.freeze_record(value, info, "f" * 64)
        existing: dict[str, object] = {
            "tag_name": value["tag"],
            "target_commitish": SHA,
            "assets": [{"name": value["assets"][0], "digest": "sha256:" + "d" * 64}],
        }
        with patch.object(release, "command", return_value=json.dumps([existing])):
            release.verify_existing_release_assets(value, frozen)
            existing["assets"] = [{"name": value["assets"][0], "digest": "sha256:" + "e" * 64}]
            with (
                patch.object(release, "command", return_value=json.dumps([existing])),
                self.assertRaisesRegex(release.ReleaseError, "frozen hash"),
            ):
                release.verify_existing_release_assets(value, frozen)

    def test_existing_tag_requires_frozen_same_sha(self) -> None:
        value = release.directive(444, "1.0.1", SHA)
        issue = json.dumps(
            {
                "state": "OPEN",
                "title": "release v1.0.1",
                "body": f"- Candidate merge SHA: **{SHA}**\n- Candidate PR: #1000\n- Version-bump merge SHA: **{BUMP}**\n- Version-bump PR: #999",
            }
        )
        tag_target = SHA

        def fake_command(*args: str) -> str:
            if args[:3] == ("gh", "issue", "view"):
                return issue
            if args[:3] == ("gh", "pr", "view"):
                oid = BUMP if args[3] == "999" else SHA
                return json.dumps(
                    {"baseRefName": "main", "mergedAt": "now", "mergeCommit": {"oid": oid}}
                )
            if args[:3] == ("git", "rev-parse", "HEAD"):
                return SHA
            if args[:3] == ("git", "rev-list", "--parents"):
                return f"{BUMP} {PARENT}"
            if args[:2] == ("git", "show"):
                return (
                    '[package]\nversion = "1.0.0"\n'
                    if args[2].startswith(PARENT)
                    else '[package]\nversion = "1.0.1"\n'
                )
            if args[:3] == ("git", "ls-remote", "--tags"):
                return f"{tag_target}\t{args[-1]}"
            return ""

        with (
            patch.object(release, "command", side_effect=fake_command),
            patch.object(release, "source_version", return_value="1.0.1"),
            patch.object(release.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            with self.assertRaisesRegex(release.ReleaseError, "without frozen"):
                release.validate_candidate(value, require_registry_free=False)
            release.validate_candidate(
                value, require_registry_free=False, frozen={"directive": value}
            )
            tag_target = PARENT
            with self.assertRaisesRegex(release.ReleaseError, "different commit"):
                release.validate_candidate(
                    value, require_registry_free=False, frozen={"directive": value}
                )

    def test_comment_round_trip_and_dry_run_has_no_worker_dispatch(self) -> None:
        directive = release.directive(444, "1.0.1", SHA)
        body = release.comment_body(directive, "ready-preflight")
        self.assertEqual(release.parse_directive(body), directive)
        self.assertIsNone(release.parse_directive("ordinary comment"))
        with (
            patch(
                "sys.argv",
                ["release.py", "start", "--issue", "444", "--candidate-sha", SHA, "--dry"],
            ),
            patch.object(release, "source_version", return_value="1.0.1"),
            patch.object(release, "issue_directives", return_value=[]),
            patch.object(release, "frozen_identity", return_value=None),
            patch.object(release, "validate_candidate"),
            patch.object(release, "command") as run,
        ):
            self.assertEqual(release.main(), 0)
            run.assert_not_called()

    def test_issue_directive_ignores_untrusted_comments(self) -> None:
        value = release.directive(444, "1.0.1", SHA)
        body = release.comment_body(value, "ready")
        comments = json.dumps(
            [
                {"body": body, "author_association": "NONE", "login": "outsider"},
                {"body": body, "author_association": "OWNER", "login": "zackees"},
            ]
        )
        with patch.object(release, "command", return_value=comments):
            self.assertEqual(release.issue_directives(444), [value])

    def test_workflow_has_issue_gate_before_every_build(self) -> None:
        import yaml

        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / ".github/workflows/auto-release.yml").read_text()
        )
        jobs = workflow["jobs"]
        self.assertIn("validate-attempt", jobs)
        for job in ("build-and-package", "build-binaries"):
            self.assertIn("validate-attempt", jobs[job]["needs"])
        self.assertNotIn("push", workflow.get("on", workflow.get(True, {})))


if __name__ == "__main__":
    unittest.main()
