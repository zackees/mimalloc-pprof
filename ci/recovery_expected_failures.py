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


def failures(path):
    out = set()
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            out.add(case.get("name"))
    return out


def names(path):
    return {c.get("name") for c in ET.parse(path).getroot().iter("testcase")}


def main(argv):
    if not argv:
        print("usage: recovery_expected_failures.py <junit.xml> [...]", file=sys.stderr)
        return 2

    failed, seen = set(), set()
    for p in argv:
        failed |= failures(p)
        seen |= names(p)

    unexpected = failed - EXPECTED
    # Only waivers for tests that actually ran can be judged stale; a test absent
    # from this bundle says nothing either way.
    stale = (EXPECTED & seen) - failed

    print("ran %d tests across %d bundle(s)" % (len(seen), len(argv)))
    print("failed: %s" % (sorted(failed) or "none"))

    rc = 0
    if unexpected:
        print("::error::Recovery lane has UNEXPECTED failures: %s" % sorted(unexpected))
        rc = 1
    if stale:
        print("::error::These are waived as Recovery-only but PASSED: %s. "
              "Remove them from EXPECTED in ci/recovery_expected_failures.py."
              % sorted(stale))
        rc = 1
    if not rc:
        waived = sorted(EXPECTED & seen)
        print("OK: the only failures are the %d waived Recovery-environment ones (%s): %s"
              % (len(waived), REASON, waived))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
