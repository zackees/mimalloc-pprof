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

    def test_bundle_member_safety(self) -> None:
        from tempfile import TemporaryDirectory

        asset, shared_name, static_name, bundle_shared, bundle_static = self.CASES[0]
        unsafe = [
            (".", tarfile.REGTYPE),
            ("./.", tarfile.DIRTYPE),
            ("../escape", tarfile.DIRTYPE),
            ("./../escape", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("./sub/../escape", tarfile.REGTYPE),
            ("./sub//escape", tarfile.REGTYPE),
            ("./sub\\escape", tarfile.REGTYPE),
            ("C:/absolute", tarfile.REGTYPE),
            ("./link", tarfile.SYMTYPE),
            ("./hardlink", tarfile.LNKTYPE),
            ("./fifo", tarfile.FIFOTYPE),
            ("./device", tarfile.CHRTYPE),
            ("./block", tarfile.BLKTYPE),
        ]
        for root_name in (".", "./"):
            with self.subTest(root_name=root_name), TemporaryDirectory() as temp:
                root = tarfile.TarInfo(root_name)
                root.type = tarfile.DIRTYPE
                safe_link = tarfile.TarInfo("./libmimalloc.3.dylib")
                safe_link.type = tarfile.SYMTYPE
                safe_link.linkname = bundle_shared
                self.check_case(
                    Path(temp),
                    asset,
                    shared_name,
                    static_name,
                    bundle_shared,
                    bundle_static,
                    extra_rows=[root, safe_link],
                )
        with TemporaryDirectory() as temp:
            self.check_case(
                Path(temp),
                asset,
                shared_name,
                static_name,
                bundle_shared,
                bundle_static,
                extra_rows=[root, root],
                expected_error="duplicate test bundle member",
            )
        for name, kind in unsafe:
            with self.subTest(name=name, kind=kind), TemporaryDirectory() as temp:
                row = tarfile.TarInfo(name)
                row.type = kind
                self.check_case(
                    Path(temp),
                    asset,
                    shared_name,
                    static_name,
                    bundle_shared,
                    bundle_static,
                    extra_rows=[row],
                    expected_error="unsafe test bundle member",
                )
        unsafe_links = [
            [("./escape", "../outside")],
            [("./dangling", "missing")],
            [("./cycle-a", "cycle-b"), ("./cycle-b", "cycle-a")],
        ]
        for definitions in unsafe_links:
            with self.subTest(definitions=definitions), TemporaryDirectory() as temp:
                links: list[tarfile.TarInfo] = []
                for name, target in definitions:
                    row = tarfile.TarInfo(name)
                    row.type = tarfile.SYMTYPE
                    row.linkname = target
                    links.append(row)
                self.check_case(
                    Path(temp),
                    asset,
                    shared_name,
                    static_name,
                    bundle_shared,
                    bundle_static,
                    extra_rows=links,
                    expected_error="unsafe test bundle member",
                )

    def check_case(
        self,
        tmp_path: Path,
        asset: str,
        shared_name: str,
        static_name: str,
        bundle_shared: str,
        bundle_static: str,
        extra_rows: list[tarfile.TarInfo] | None = None,
        expected_error: str | None = None,
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
                for row in extra_rows or []:
                    output.addfile(row)
                for name, data in contents.items():
                    row = tarfile.TarInfo(name)
                    row.size = len(data)
                    output.addfile(row, io.BytesIO(data))

        write_bundle()
        if expected_error is not None:
            with self.assertRaisesRegex(ReleaseError, expected_error):
                verify(archive, bundle, asset)
            return
        verify(archive, bundle, asset)
        contents[bundle_shared] = b"different shared bytes"
        write_bundle()
        with self.assertRaisesRegex(ReleaseError, "differs"):
            verify(archive, bundle, asset)
