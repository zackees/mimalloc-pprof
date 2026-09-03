#!/usr/bin/env python3
"""Gate the Recovery-mode macOS lane on an EXACT set of expected failures.

Three tests cannot pass inside macOS Recovery, for an environment reason rather
than a product one: Recovery has no dyld shared cache, so a stack PC in a system
library does not resolve to a loaded module image. All three trip the same
assertion:

    Assertion failed: (visit_ctx.out_of_range == 0),
    function test_macos_stack_pcs_resolve_to_modules, file test-profile.c:345

`ci/run_test_bundle.py` has `--only` but no `--skip`, and enumerating every
other test to exclude these would silently drop any test added later -- the
exact failure mode `ci/bundle_coverage.py` exists to prevent. So the bundle runs
in full and this asserts the failure set is EXACTLY the expected one:

  * any additional failure  -> red (a real regression)
  * an expected failure that now PASSES -> red (the waiver is stale, drop it)

so the waiver cannot rot in either direction.

    python3 ci/recovery_expected_failures.py bundle-release.xml [more.xml ...]
"""

import sys
import xml.etree.ElementTree as ET

# Keyed by bare test name; these are the only failures Recovery may produce.
EXPECTED = {
    "test-profile",
    "test-profile-accum",
    "test-profile-auto",
}
REASON = "no dyld shared cache in Recovery; stack PCs do not resolve to modules"


def failures(path: str) -> set[str]:
    out: set[str] = set()
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            name = case.get("name")
            if name is not None:
                out.add(name)
    return out


def names(path: str) -> set[str]:
    root = ET.parse(path).getroot()
    return {name for case in root.iter("testcase") if (name := case.get("name")) is not None}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: recovery_expected_failures.py <junit.xml> [...]", file=sys.stderr)
        return 2

    failed: set[str] = set()
    seen: set[str] = set()
    for p in argv:
        failed |= failures(p)
        seen |= names(p)

    unexpected = failed - EXPECTED
    # Only waivers for tests that actually ran can be judged stale; a test absent
    # from this bundle says nothing either way.
    stale = (EXPECTED & seen) - failed

    print(f"ran {len(seen)} tests across {len(argv)} bundle(s)")
    print(f"failed: {sorted(failed) or 'none'}")

    rc = 0
    if unexpected:
        print(f"::error::Recovery lane has UNEXPECTED failures: {sorted(unexpected)}")
        rc = 1
    if stale:
        print(
            f"::error::These are waived as Recovery-only but PASSED: {sorted(stale)}. "
            "Remove them from EXPECTED in ci/recovery_expected_failures.py."
        )
        rc = 1
    if not rc:
        waived = sorted(EXPECTED & seen)
        print(
            f"OK: the only failures are the {len(waived)} waived "
            f"Recovery-environment ones ({REASON}): {waived}"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
