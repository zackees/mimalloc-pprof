from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_benchmark_scaling_workflow as policy


class BenchmarkScalingWorkflowTests(unittest.TestCase):
    def workflow(self) -> dict[str, object]:
        return policy.load(policy.WORKFLOW)

    def test_production_workflow_passes(self) -> None:
        policy.validate(self.workflow())

    def test_selftest_negative_controls_all_fail_closed(self) -> None:
        # The selftest is the real guard against a checker that checks nothing.
        policy.selftest(policy.WORKFLOW)
        self.assertGreaterEqual(len(policy.MUTATIONS), 15)

    def test_budget_over_twenty_minutes_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        build["timeout-minutes"] = 45
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "20"):
            policy.validate(value)

    def test_parallel_matrix_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        build["strategy"] = {"matrix": {"allocator": ["a", "b"]}}
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "parallel matrices"):
            policy.validate(value)

    def test_separate_concurrency_group_is_rejected(self) -> None:
        value = copy.deepcopy(self.workflow())
        value["concurrency"] = {"group": "scaling-only", "cancel-in-progress": False}
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "serialize"):
            policy.validate(value)

    def test_eligibility_must_pin_main_full_and_three_blocks(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        eligibility = policy.steps_by_name(build)["compute publication eligibility"]
        eligibility["run"] = str(eligibility["run"]).replace("-eq 3", "-ge 1")
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "eq 3"):
            policy.validate(value)

    def test_shell_interpolated_seed_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        seed = policy.steps_by_name(build)["determine run seed"]
        seed["run"] = 'SEED="${{ inputs.run_seed }}"'
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "through env"):
            policy.validate(value)

    def test_raw_artifact_retention_is_enforced(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        raw = policy.steps_by_name(build)["upload raw scaling artifact"]
        raw_with = raw["with"]
        assert isinstance(raw_with, dict)
        raw_with["retention-days"] = 1
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "30 days"):
            policy.validate(value)


if __name__ == "__main__":
    unittest.main()
