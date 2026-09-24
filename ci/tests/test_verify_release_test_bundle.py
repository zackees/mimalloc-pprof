"""The native release gate rejects a test bundle with different library bytes."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release import ReleaseError
from verify_release_test_bundle import verify


class ReleaseTestBundleTests(unittest.TestCase):
    CASES: ClassVar[list[tuple[str, str, str, str, str]]] = [
        (
            "macos-arm64",
            "lib/libmimalloc.3.0.dylib",
            "lib/mimalloc-3.0/libmimalloc.a",
            "libmimalloc.3.0.dylib",
            "libmimalloc.a",
        ),
        (
            "windows-x64-gnu",
            "bin/mimalloc.dll",
            "lib/mimalloc-3.0/libmimalloc.a",
            "mimalloc.dll",
            "libmimalloc.a",
        ),
        (
            "windows-x64-msvc",
            "bin/mimalloc.dll",
            "lib/mimalloc-3.0/mimalloc.lib",
            "mimalloc.dll",
            "mimalloc.lib",
        ),
    ]

    def test_exact_release_libraries(self) -> None:
        from tempfile import TemporaryDirectory

        for asset, shared_name, static_name, bundle_shared, bundle_static in self.CASES:
            with self.subTest(asset=asset), TemporaryDirectory() as temp:
                self.check_case(
                    Path(temp), asset, shared_name, static_name, bundle_shared, bundle_static
                )

    def check_case(
        self,
        tmp_path: Path,
        asset: str,
        shared_name: str,
        static_name: str,
        bundle_shared: str,
        bundle_static: str,
    ) -> None:
        shipped = {shared_name: b"shared bytes", static_name: b"static bytes"}
        archive = tmp_path / ("asset.tar.gz" if asset.startswith("macos") else "asset.zip")
        if asset.startswith("macos"):
            with tarfile.open(archive, "w:gz") as output:
                for name, data in shipped.items():
                    row = tarfile.TarInfo(name)
                    row.size = len(data)
                    output.addfile(row, io.BytesIO(data))
        else:
            with zipfile.ZipFile(archive, "w") as output:
                for name, data in shipped.items():
                    output.writestr(name, data)

        bundle = tmp_path / "bundle.tar.gz"
        contents = {
            "tests.json": json.dumps(
                {
                    "tests": [{"name": "test-stress-dynamic"}]
                    + [{"name": f"test-{n}"} for n in range(20)]
                }
            ).encode(),
            bundle_shared: shipped[shared_name],
            bundle_static: shipped[static_name],
        }

        def write_bundle() -> None:
            with tarfile.open(bundle, "w:gz") as output:
                for name, data in contents.items():
                    row = tarfile.TarInfo(name)
                    row.size = len(data)
                    output.addfile(row, io.BytesIO(data))

        write_bundle()
        verify(archive, bundle, asset)
        contents[bundle_shared] = b"different shared bytes"
        write_bundle()
        with self.assertRaisesRegex(ReleaseError, "differs"):
            verify(archive, bundle, asset)
