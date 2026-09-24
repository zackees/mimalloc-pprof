"""Exact-byte registry upload contract; no test contacts a registry."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import struct
import subprocess
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import cast
from unittest.mock import patch

from ci import release, release_destinations, release_live


class FakeResponse:
    status = 200

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, *_: object) -> bytes:
        return b'{"warnings":{}}'


class LiveUploadTests(unittest.TestCase):
    def test_publish_metadata_skips_soldr_json_telemetry(self) -> None:
        package: dict[str, object] = {
            "name": "mimalloc-pprof",
            "version": "1.0.1",
            "authors": [],
            "description": None,
            "documentation": None,
            "homepage": None,
            "keywords": [],
            "categories": [],
            "license": None,
            "license_file": None,
            "repository": None,
            "links": None,
            "rust_version": None,
            "readme": None,
            "features": {"default": []},
            "dependencies": [],
        }
        metadata: dict[str, object] = {
            "packages": [package],
            "workspace_members": ["mimalloc-pprof 1.0.1"],
            "version": 1,
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"elapsed_seconds":0.2,"command":"cargo metadata"}\n'
                "soldr: cache telemetry\n"
                f"{json.dumps(metadata)}\n"
            ),
            stderr="",
        )
        manifest = (
            b'[package]\nname = "mimalloc-pprof"\nversion = "1.0.1"\n'
            b'edition = "2021"\n\n[features]\ndefault = []\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            crate = Path(temporary) / "mimalloc-pprof-1.0.1.crate"
            with tarfile.open(crate, "w:gz") as archive:
                member = tarfile.TarInfo("mimalloc-pprof-1.0.1/Cargo.toml")
                member.size = len(manifest)
                archive.addfile(member, io.BytesIO(manifest))
            with patch.object(release_live.subprocess, "run", return_value=completed):
                publish = release_live.crate_publish_metadata(crate)

        self.assertEqual(publish["name"], "mimalloc-pprof")
        self.assertEqual(publish["vers"], "1.0.1")

    def test_metadata_parser_rejects_unrelated_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "no valid metadata JSON document"):
            release_live.parse_cargo_metadata_stdout('{"elapsed_seconds":0.2}\n')

    def test_permanent_registry_http_error_is_not_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            crate = Path(temporary) / "mimalloc-pprof-1.0.1.crate"
            archive = b"exact frozen archive bytes"
            crate.write_bytes(archive)
            destination = release_live.LiveDestination(444, crate)
            frozen = {"crate_sha256": hashlib.sha256(archive).hexdigest()}
            metadata: dict[str, object] = {
                "name": "mimalloc-pprof",
                "vers": "1.0.1",
                "deps": [],
            }
            error = urllib.error.HTTPError(
                "https://crates.io/api/v1/crates/new",
                403,
                "forbidden",
                Message(),
                None,
            )
            with (
                patch.object(destination, "read_freeze", return_value=frozen),
                patch.object(release_live, "crate_publish_metadata", return_value=metadata),
                patch.object(release_live.urllib.request, "urlopen", side_effect=error),
                patch.dict(os.environ, {"CARGO_REGISTRY_TOKEN": "test-only-token"}),
                self.assertRaises(release.ReleaseError) as raised,
            ):
                destination.validate_crate(crate)
                destination.publish_crate(crate)
            self.assertNotIsInstance(
                raised.exception, release_destinations.AmbiguousCratePublishError
            )

    def test_put_sends_the_frozen_archive_as_the_exact_registry_body_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            crate = Path(temporary) / "mimalloc-pprof-1.0.1.crate"
            archive = b"exact frozen archive bytes"
            crate.write_bytes(archive)
            destination = release_live.LiveDestination(444, crate)
            frozen = {"crate_sha256": hashlib.sha256(archive).hexdigest()}
            metadata: dict[str, object] = {"name": "mimalloc-pprof", "vers": "1.0.1", "deps": []}
            captured: list[urllib.request.Request] = []

            def open_request(request: urllib.request.Request, *, timeout: int) -> FakeResponse:
                captured.append(request)
                self.assertEqual(timeout, 180)
                return FakeResponse()

            with (
                patch.object(destination, "read_freeze", return_value=frozen),
                patch.object(release_live, "crate_publish_metadata", return_value=metadata),
                patch.object(release_live.urllib.request, "urlopen", side_effect=open_request),
                patch.dict(os.environ, {"CARGO_REGISTRY_TOKEN": "test-only-token"}),
            ):
                destination.validate_crate(crate)
                destination.publish_crate(crate)
            self.assertEqual(len(captured), 1)
            request = captured[0]
            self.assertEqual(request.get_method(), "PUT")
            self.assertEqual(request.full_url, "https://crates.io/api/v1/crates/new")
            body = request.data
            assert isinstance(body, bytes)
            json_length = struct.unpack_from("<I", body, 0)[0]
            self.assertEqual(json.loads(body[4 : 4 + json_length]), metadata)
            archive_offset = 4 + json_length
            self.assertEqual(struct.unpack_from("<I", body, archive_offset)[0], len(archive))
            self.assertEqual(body[archive_offset + 4 :], archive)

    def test_changed_archive_never_reaches_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            crate = Path(temporary) / "mimalloc-pprof-1.0.1.crate"
            crate.write_bytes(b"changed")
            destination = release_live.LiveDestination(444, crate)
            with (
                patch.object(destination, "read_freeze", return_value={"crate_sha256": "0" * 64}),
                patch.object(
                    release_live,
                    "crate_publish_metadata",
                    return_value={"name": "mimalloc-pprof", "vers": "1.0.1", "deps": []},
                ),
                patch.object(release_live.urllib.request, "urlopen") as upload,
                self.assertRaisesRegex(ValueError, "authoritative issue freeze"),
            ):
                destination.validate_crate(crate)
                destination.publish_crate(crate)
            upload.assert_not_called()

    @unittest.skipUnless(
        os.environ.get("RELEASE_WIRE_PROBE") == "1", "manual local Cargo wire probe"
    )
    def test_publish_metadata_shape_matches_cargo_local_registry_wire(self) -> None:
        """Capture Cargo's real PUT to localhost; no crates.io request is made."""

        class Registry(http.server.BaseHTTPRequestHandler):
            payload: bytes | None = None

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def do_GET(self) -> None:
                _, port = cast(tuple[str, int], self.server.server_address)
                if self.path == "/index/config.json":
                    self._send_json(
                        {
                            "dl": f"http://127.0.0.1:{port}/api/v1/crates",
                            "api": f"http://127.0.0.1:{port}",
                        }
                    )
                    return
                if self.path.endswith("/wi/re/wire-probe") and Registry.payload is not None:
                    payload = Registry.payload
                    json_size = struct.unpack_from("<I", payload, 0)[0]
                    row = json.loads(payload[4 : 4 + json_size])
                    crate_offset = 4 + json_size
                    crate = payload[crate_offset + 4 :]
                    entry = {
                        "name": row["name"],
                        "vers": row["vers"],
                        "deps": row["deps"],
                        "cksum": hashlib.sha256(crate).hexdigest(),
                        "features": row["features"],
                        "yanked": False,
                    }
                    self._send_bytes((json.dumps(entry) + "\n").encode())
                    return
                self.send_error(404)

            def do_PUT(self) -> None:
                if self.path != "/api/v1/crates/new":
                    self.send_error(404)
                    return
                Registry.payload = self.rfile.read(int(self.headers["Content-Length"]))
                self._send_json({"warnings": {}})

            def _send_json(self, value: dict[str, object]) -> None:
                self._send_bytes(json.dumps(value).encode())

            def _send_bytes(self, value: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "src").mkdir()
                (root / "src/lib.rs").write_text("pub fn probe() {}\n")
                (root / "README.md").write_text("# Wire probe\n")
                (root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.1"\n')
                (root / "Cargo.toml").write_text(
                    '[package]\nname = "wire-probe"\nversion = "0.1.0"\nedition = "2021"\nlicense = "MIT"\ndescription = "local wire probe"\nreadme = "README.md"\n[features]\ndefault = []\nprobe = []\n'
                )
                port = server.server_address[1]
                result = subprocess.run(
                    [
                        "soldr",
                        "cargo",
                        "publish",
                        "--registry",
                        "capture",
                        "--allow-dirty",
                        "--no-verify",
                        "--config",
                        f'registries.capture.index="sparse+http://127.0.0.1:{port}/index/"',
                    ],
                    cwd=root,
                    env={**os.environ, "CARGO_REGISTRIES_CAPTURE_TOKEN": "local-test-token"},
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIsNotNone(Registry.payload)
                assert Registry.payload is not None
                json_size = struct.unpack_from("<I", Registry.payload, 0)[0]
                cargo_wire = json.loads(Registry.payload[4 : 4 + json_size])
                expected_keys = {
                    "name",
                    "vers",
                    "deps",
                    "features",
                    "authors",
                    "description",
                    "documentation",
                    "homepage",
                    "readme",
                    "readme_file",
                    "keywords",
                    "categories",
                    "license",
                    "license_file",
                    "repository",
                    "badges",
                    "links",
                    "rust_version",
                }
                self.assertEqual(set(cargo_wire), expected_keys)
                self.assertEqual(cargo_wire["readme"], "# Wire probe\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
