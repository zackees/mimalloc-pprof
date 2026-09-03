from __future__ import annotations

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

import build_benchmark_allocators as builder

BUN_ID = "bun-mimalloc"
BUN_COMMIT = "b20b60d959093b1bc0e24306ec72ccacb3e46fb9"


def locked_records() -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        json.loads(builder.default_lockfile().read_text(encoding="utf-8"))["allocators"],
    )


def bun_record() -> dict[str, Any]:
    return next(record for record in locked_records() if record["id"] == BUN_ID)


class BunAllocatorRowTests(unittest.TestCase):
    def test_lockfile_carries_the_bun_row_in_the_expected_position(self) -> None:
        records = builder.read_lockfile(builder.default_lockfile())
        ids = [record["id"] for record in records]
        self.assertEqual(list(builder.EXPECTED_IDS), ids)
        self.assertEqual(
            ["tcmalloc", "jemalloc", "upstream-mimalloc", BUN_ID, "mimalloc-pprof"], ids
        )

    def test_bun_row_is_an_immutable_archive_built_by_the_shared_recipe(self) -> None:
        record = bun_record()
        source = record["source"]
        self.assertEqual("archive", source["kind"])
        self.assertEqual("https://github.com/oven-sh/mimalloc", source["canonical_repository"])
        self.assertEqual(BUN_COMMIT, source["commit"])
        self.assertIn(BUN_COMMIT, source["archive_url"])
        self.assertTrue(builder.is_hex_digest(source["archive_sha256"], builder.SHA256_LENGTH))
        self.assertEqual("cmake-ninja", record["build"]["system"])
        self.assertEqual("mimalloc-c-api", record["adapter_kind"])
        self.assertEqual("libmimalloc.a", record["expected_static_library"])
        self.assertEqual({"source": [], "build": []}, record["patches"])

    def test_bun_and_upstream_share_every_build_flag(self) -> None:
        """The Bun row exists to be compared, so its recipe must be the upstream
        recipe minus the MI_PPROF option Bun's tree does not have."""

        records = locked_records()
        by_id = {record["id"]: record for record in records}
        upstream_flags = set(by_id["upstream-mimalloc"]["build"]["flags"])
        bun_flags = set(by_id[BUN_ID]["build"]["flags"])
        self.assertEqual({"MI_PPROF=OFF"}, upstream_flags - bun_flags)
        self.assertEqual(set(), bun_flags - upstream_flags)
        comparison = builder.mimalloc_option_comparison(records)
        difference = cast(dict[str, str], comparison["intentional_difference"])
        self.assertEqual("not-applicable", difference[BUN_ID])

    def test_a_bun_row_with_an_extra_build_flag_is_rejected(self) -> None:
        records = locked_records()
        for record in records:
            if record["id"] == BUN_ID:
                record["build"]["flags"].append("MI_SECURE=ON")
        with self.assertRaises(builder.ArchiveError):
            builder.mimalloc_option_comparison(records)

    def test_a_floating_bun_pin_is_rejected(self) -> None:
        record = bun_record()
        with self.assertRaises(builder.LockfileError):
            builder.validate_source(BUN_ID, "bun-dev3-v2@deadbeef", record["source"])

    def test_a_mis_hashed_bun_archive_fails_the_build_instead_of_being_skipped(self) -> None:
        """Positive control for issue #325's acceptance criterion: flip the
        locked digest and the download must raise rather than silently accept
        or skip the row. The archive is served from a local file:// URL so the
        control needs no network."""

        payload = b"not the real oven-sh/mimalloc source\n"
        with tempfile.TemporaryDirectory(prefix="bun-mis-hash-") as temporary:
            root = Path(temporary)
            archive = root / f"{BUN_ID}-{BUN_COMMIT}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("mimalloc/readme.md")
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
            honest_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            mis_hash = ("0" if honest_digest[0] != "0" else "1") + honest_digest[1:]
            destination = root / "downloaded.tar.gz"

            with self.assertRaises(builder.ArchiveError) as raised:
                builder.download_archive(archive.as_uri(), mis_hash, destination)
            self.assertIn("checksum mismatch", str(raised.exception))
            self.assertFalse(destination.exists(), "a mis-hashed archive must not be left on disk")

            # The same call succeeds against the honest digest, which proves the
            # control failed for the hash and not for an unrelated reason.
            builder.download_archive(archive.as_uri(), honest_digest, destination)
            self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
