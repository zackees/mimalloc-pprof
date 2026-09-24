"""Hash and host refusal checks for the packaged-library smoke gate."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release import ReleaseError
from smoke_release_archive import verify_and_smoke


class _ArchiveArtifact(TypedDict):
    name: str
    bytes: int
    sha256: str
    validated: dict[str, str]


class _ArchiveInfo(TypedDict):
    candidate_sha: str
    artifacts: list[_ArchiveArtifact]


class SmokeArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        self.sha = "a" * 40
        self.binary = b"sample library bytes"
        self.name = "mimalloc-pprof-windows-x64-gnu-v1.0.1.zip"
        with zipfile.ZipFile(self.dist / self.name, "w") as archive:
            archive.writestr("bin/mimalloc.dll", self.binary)
            archive.writestr("bin/libgcc_s_seh-1.dll", b"runtime")
        raw = (self.dist / self.name).read_bytes()
        self.info: _ArchiveInfo = {
            "candidate_sha": self.sha,
            "artifacts": [
                {
                    "name": self.name,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "validated": {
                        "binary": "bin/mimalloc.dll",
                        "binary_sha256": hashlib.sha256(self.binary).hexdigest(),
                    },
                }
            ],
        }
        self.write_info()

    def write_info(self) -> None:
        (self.dist / "info.json").write_text(json.dumps(self.info), encoding="utf-8")

    def test_refuses_wrong_host(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "cannot run"):
            verify_and_smoke(self.dist, "macos-arm64", self.sha)

    def test_refuses_wrong_candidate(self) -> None:
        with (
            patch("smoke_release_archive.sys.platform", "win32"),
            patch("smoke_release_archive.platform.machine", return_value="AMD64"),
            self.assertRaisesRegex(ReleaseError, "candidate SHA"),
        ):
            verify_and_smoke(self.dist, "windows-x64-gnu", "b" * 40)

    def test_refuses_archive_and_library_hash_mismatch(self) -> None:
        with (
            patch("smoke_release_archive.sys.platform", "win32"),
            patch("smoke_release_archive.platform.machine", return_value="AMD64"),
        ):
            self.info["artifacts"][0]["sha256"] = "0" * 64
            self.write_info()
            with self.assertRaisesRegex(ReleaseError, "archive differs"):
                verify_and_smoke(self.dist, "windows-x64-gnu", self.sha)
            raw = (self.dist / self.name).read_bytes()
            self.info["artifacts"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
            self.info["artifacts"][0]["validated"]["binary_sha256"] = "0" * 64
            self.write_info()
            with self.assertRaisesRegex(ReleaseError, "library differs"):
                verify_and_smoke(self.dist, "windows-x64-gnu", self.sha)
