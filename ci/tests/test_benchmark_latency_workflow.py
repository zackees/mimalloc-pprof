from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import copy
import unittest

import check_benchmark_latency_workflow as policy


class BenchmarkLatencyWorkflowTests(unittest.TestCase):
    def workflow(self) -> dict[str, object]:
        return policy.load(policy.WORKFLOW)

    def test_production_workflow_passes(self) -> None:
        policy.validate(self.workflow())

    def test_parallel_matrix_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        build["strategy"] = {"matrix": {"allocator": ["a", "b"]}}
        with self.assertRaisesRegex(policy.LatencyWorkflowError, "parallel matrices"):
            policy.validate(value)

    def test_nondefault_publication_and_missing_raw_retention_are_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        steps = build["steps"]
        assert isinstance(steps, list)
        eligibility = next(
            item
            for item in steps
            if isinstance(item, dict) and item.get("name") == "compute publication eligibility"
        )
        eligibility["run"] = str(eligibility["run"]).replace(
            "refs/heads/main", "refs/heads/feature"
        )
        with self.assertRaisesRegex(policy.LatencyWorkflowError, "refs/heads/main"):
            policy.validate(value)

        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        raw = policy.steps_by_name(build)["upload raw latency artifact"]
        raw_with = raw["with"]
        assert isinstance(raw_with, dict)
        raw_with["retention-days"] = 1
        with self.assertRaisesRegex(policy.LatencyWorkflowError, "30 days"):
            policy.validate(value)

    def test_separate_concurrency_group_is_rejected(self) -> None:
        value = copy.deepcopy(self.workflow())
        value["concurrency"] = {"group": "latency-only", "cancel-in-progress": False}
        with self.assertRaisesRegex(policy.LatencyWorkflowError, "serialize"):
            policy.validate(value)

    def test_shell_interpolated_seed_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        seed = policy.steps_by_name(build)["determine run seed"]
        seed["run"] = 'SEED="${{ inputs.run_seed }}"'
        with self.assertRaisesRegex(policy.LatencyWorkflowError, "through env"):
            policy.validate(value)


if __name__ == "__main__":
    unittest.main()
