from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import check_macos_memory_control as control


class NativeMacControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def runs(self, prefix: str, peak: float, leak: int) -> list[Path]:
        paths: list[Path] = []
        for index in range(8):
            path = self.root / f"{prefix}-{index}.json"
            path.write_text(
                json.dumps(
                    {
                        "platform": "macos",
                        "gated_metric": "peak_rss",
                        "mi_pprof": 1,
                        "inject_leak": leak,
                        "peak_mb": peak,
                    }
                ),
                encoding="utf-8",
            )
            paths.append(path)
        return paths

    def test_expected_leak_is_detected_without_a_committed_baseline(self) -> None:
        control.check(self.runs("normal", 100, 0), self.runs("leak", 125, 200000))

    def test_missing_measurement_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected 8"):
            control.check(self.runs("normal", 100, 0)[:-1], self.runs("leak", 125, 200000))

    def test_weak_control_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not fire"):
            control.check(self.runs("normal", 100, 0), self.runs("leak", 105, 200000))

    def test_uninstrumented_control_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "MI_BENCH_INJECT_LEAK"):
            control.check(self.runs("normal", 100, 0), self.runs("leak", 125, 0))


if __name__ == "__main__":
    unittest.main()
