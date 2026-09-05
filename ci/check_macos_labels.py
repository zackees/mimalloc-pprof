#!/usr/bin/env python3
"""Every Darwin-only test must carry the `macos` ctest label (#339).

The selective macOS lane in `macos-bundles.yml` executes `run_test_bundle.py --label
macos` inside the Recovery guest, so a test that only means something on macOS but
forgot its `LABELS macos` would build, ship in the bundle, and never run anywhere --
the "gate that verifies nothing" shape docs/ci-gates.md exists to prevent. This check
runs on the bundle manifest (`tests.json`, the same file the guest replays) and makes
the omission red on the Linux build job, long before a guest boots.

The convention is by name: a test called `test-osx-*` is Darwin-only. Anything else
may carry the label too (a portable test worth re-running on macOS, e.g. the TLS-slot
probe), but is not required to.

    python3 ci/check_macos_labels.py bundle-dir/tests.json [...]
    python3 ci/check_macos_labels.py --selftest
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

MACOS_LABEL = "macos"
DARWIN_ONLY_PREFIX = "test-osx-"


def _entries(manifest: Mapping[str, object]) -> list[tuple[str, list[str]]]:
    """(name, labels) for every well-formed entry; raises if there is no `tests` array."""
    tests = manifest.get("tests")
    if not isinstance(tests, list):
        raise ValueError("manifest has no `tests` array")
    out: list[tuple[str, list[str]]] = []
    for raw in cast("list[object]", tests):
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, object]", raw)
        name = entry.get("name")
        labels = entry.get("labels")
        if not isinstance(name, str):
            continue
        clean = (
            [x for x in cast("list[object]", labels) if isinstance(x, str)]
            if isinstance(labels, list)
            else []
        )
        out.append((name, clean))
    return out


def unlabeled(manifest: Mapping[str, object]) -> list[str]:
    """Names of `test-osx-*` entries that do not carry the `macos` label."""
    return sorted(
        name
        for name, labels in _entries(manifest)
        if name.startswith(DARWIN_ONLY_PREFIX) and MACOS_LABEL not in labels
    )


def labeled(manifest: Mapping[str, object]) -> list[str]:
    return sorted(name for name, labels in _entries(manifest) if MACOS_LABEL in labels)


def selftest() -> int:
    good: dict[str, object] = {"tests": [{"name": "test-osx-zone", "labels": ["macos", "serial"]}]}
    bad: dict[str, object] = {
        "tests": [{"name": "test-osx-zone", "labels": []}, {"name": "test-api", "labels": []}]
    }
    assert unlabeled(good) == [], unlabeled(good)
    assert unlabeled(bad) == ["test-osx-zone"], unlabeled(bad)
    assert labeled(good) == ["test-osx-zone"]
    try:
        unlabeled({"nope": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("a manifest without `tests` must be rejected")
    print("check_macos_labels selftest OK")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    rc = 0
    for arg in argv:
        path = Path(arg)
        manifest = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        missing = unlabeled(manifest)
        have = labeled(manifest)
        print(f"{path}: {len(have)} test(s) labelled `{MACOS_LABEL}`: {have or 'none'}")
        if missing:
            print(
                f"::error::{path}: Darwin-only tests without `LABELS {MACOS_LABEL}` "
                f"(they would never execute in the selective lane): {missing}"
            )
            rc = 1
        if not have:
            print(
                f"::error::{path}: no test carries `{MACOS_LABEL}`; the selective lane "
                "would refuse to run (run_test_bundle.py --label)"
            )
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
