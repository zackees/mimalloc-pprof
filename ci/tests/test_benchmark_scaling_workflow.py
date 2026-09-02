from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import copy
import unittest

import benchmark_report as report
import check_benchmark_scaling_workflow as policy


class BenchmarkScalingWorkflowTests(unittest.TestCase):
    def workflow(self) -> dict[str, object]:
        return policy.load(policy.WORKFLOW)

    def test_production_workflow_passes(self) -> None:
        policy.validate(self.workflow())

    def test_selftest_negative_controls_all_fail_closed(self) -> None:
        # The selftest is the real guard against a checker that checks nothing.
        policy.selftest(policy.WORKFLOW, policy.SCALING_SOURCE)
        self.assertGreaterEqual(len(policy.MUTATIONS), 15)
        self.assertGreaterEqual(len(policy.SOURCE_MUTATIONS), 5)

    def test_production_scaling_source_matches_the_validator(self) -> None:
        policy.validate_source_contract(policy.SCALING_SOURCE.read_text(encoding="utf-8"))

    def test_rust_thread_points_drifting_from_python_is_rejected(self) -> None:
        # The failure this exists to catch: someone edits the dense sweep on one
        # side only, and the mismatch surfaces only after a scheduled run has
        # spent its whole budget producing a report the validator throws away.
        source = policy.SCALING_SOURCE.read_text(encoding="utf-8")
        drifted = source.replace(
            f"[u32; {len(report.SCALING_THREAD_POINTS)}] = "
            f"[{', '.join(str(point) for point in report.SCALING_THREAD_POINTS)}]",
            "[u32; 3] = [1, 4, 16]",
        )
        self.assertNotEqual(drifted, source)
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "SCALING_THREAD_POINTS"):
            policy.validate_source_contract(drifted)

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
