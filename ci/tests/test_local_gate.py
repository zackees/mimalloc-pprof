"""ci/local_gate.py stays in step with python-lint.yml and local-gate.toml (zackees/ci.yml#166)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import local_gate

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "python-lint.yml"
SHA_RE = re.compile(r"zackees/ci\.yml(?:@| )([0-9a-f]{40})")


class LocalGateWiringTests(unittest.TestCase):
    def test_ci_lint_ref_matches_every_pin(self) -> None:
        for path in (WORKFLOW, ROOT / "local-gate.toml"):
            pins = set(SHA_RE.findall(path.read_text(encoding="utf-8")))
            self.assertEqual({local_gate.CI_LINT_REF}, pins, f"{path.name} pins {pins}")

    def test_lint_job_runs_only_the_gate_lane(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "run: uv run --no-project --python 3.13 python ci/local_gate.py --lane lint", text
        )
        self.assertNotIn("pip install", text)

    def test_check_names_are_unique(self) -> None:
        names = [check.name for check in local_gate.CHECKS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
