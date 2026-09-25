from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import copy
import unittest
from typing import cast

import benchmark_report as report
import check_benchmark_scaling_workflow as policy


class BenchmarkScalingWorkflowTests(unittest.TestCase):
    def workflow(self) -> dict[str, object]:
        return policy.load(policy.WORKFLOW)

    def test_production_workflow_passes(self) -> None:
        policy.validate(self.workflow())

    def test_daily_cadence_is_required(self) -> None:
        for schedule in ([], [{"cron": "23 7 * * 0"}], [{}]):
            with self.subTest(schedule=schedule):
                value = self.workflow()
                raw = cast(dict[object, object], value)  # PyYAML 1.1 may use True for `on`.
                triggers = raw.get("on", raw.get(True))
                assert isinstance(triggers, dict)
                triggers["schedule"] = schedule
                with self.assertRaisesRegex(policy.ScalingWorkflowError, "schedule"):
                    policy.validate(value)

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

    def test_rust_churn_contract_drifting_from_python_is_rejected(self) -> None:
        # #508: the thread-churn side-car's schema and post-drain offsets are
        # declared in scaling.rs and benchmark_report.py; a one-sided edit must fail.
        source = policy.SCALING_SOURCE.read_text(encoding="utf-8")
        renamed = source.replace(
            f'SCALING_CHURN_SCHEMA_VERSION: &str = "{report.SCALING_CHURN_SCHEMA}"',
            'SCALING_CHURN_SCHEMA_VERSION: &str = "thread-churn-post-drain-rss-v2"',
        )
        self.assertNotEqual(renamed, source)
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "SCALING_CHURN_SCHEMA_VERSION"):
            policy.validate_source_contract(renamed)
        offsets = ", ".join(str(offset) for offset in report.CHURN_OFFSETS_MS)
        drifted = source.replace(
            f"CHURN_POST_DRAIN_OFFSETS_MS: [u64; {len(report.CHURN_OFFSETS_MS)}] = [{offsets}]",
            "CHURN_POST_DRAIN_OFFSETS_MS: [u64; 2] = [100, 3000]",
        )
        self.assertNotEqual(drifted, source)
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "CHURN_POST_DRAIN_OFFSETS_MS"):
            policy.validate_source_contract(drifted)
        self.assertIn("churn schema renamed on one side", policy.SOURCE_MUTATIONS)
        self.assertIn("churn offsets diverge", policy.SOURCE_MUTATIONS)

    def test_rust_pattern_dropped_from_the_sweep_is_rejected(self) -> None:
        # #216: Larson/xmalloc-test join the four sparse patterns. Dropping one
        # from the array while trimming its length still leaves the name list
        # short, so this must be caught by the name comparison, not the length
        # check -- that is what distinguishes it from the length-lies control.
        source = policy.SCALING_SOURCE.read_text(encoding="utf-8")
        dropped = source.replace(
            f"[ScalingPattern; {len(report.SCALING_PATTERN_IDS)}]",
            f"[ScalingPattern; {len(report.SCALING_PATTERN_IDS) - 1}]",
        ).replace("    ScalingPattern::XmallocTest,\n", "", 1)
        self.assertNotEqual(dropped, source)
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "SCALING_PATTERNS"):
            policy.validate_source_contract(dropped)

    def test_rust_pattern_renamed_is_rejected(self) -> None:
        source = policy.SCALING_SOURCE.read_text(encoding="utf-8")
        renamed = source.replace('"larson"', '"larson-v2"')
        self.assertNotEqual(renamed, source)
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "SCALING_PATTERNS"):
            policy.validate_source_contract(renamed)

    def test_rust_pattern_array_length_lying_is_rejected(self) -> None:
        source = policy.SCALING_SOURCE.read_text(encoding="utf-8")
        lied = source.replace(
            f"[ScalingPattern; {len(report.SCALING_PATTERN_IDS)}]", "[ScalingPattern; 99]"
        )
        self.assertNotEqual(lied, source)
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "array length disagrees"):
            policy.validate_source_contract(lied)

    def test_budget_over_thirty_minutes_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build"]
        assert isinstance(build, dict)
        build["timeout-minutes"] = 45
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "30"):
            policy.validate(value)

    def test_measure_budget_above_approved_120_minutes_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        measure = jobs["measure"]
        assert isinstance(measure, dict)
        measure["timeout-minutes"] = 121
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "120"):
            policy.validate(value)

    def test_parallel_matrix_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build"]
        assert isinstance(build, dict)
        build["strategy"] = {"matrix": {"allocator": ["a", "b"]}}
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "parallel matrices"):
            policy.validate(value)

    def test_measure_matrix_is_rejected_to_preserve_one_host(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        measure = jobs["measure"]
        assert isinstance(measure, dict)
        measure["strategy"] = {"fail-fast": False, "matrix": {"shard": [0, 1, 2, 3, 4, 5]}}
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "one host"):
            policy.validate(value)

    def test_measure_shard_loop_must_cover_six_points(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        measure = jobs["measure"]
        assert isinstance(measure, dict)
        run = policy.steps_by_name(measure)["run sparse scaling sweep"]
        run["run"] = str(run["run"]).replace("0 1 2 3 4 5", "0 1 2")
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "SHARD"):
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
        assemble = jobs["assemble"]
        assert isinstance(assemble, dict)
        eligibility = policy.steps_by_name(assemble)["compute publication eligibility"]
        eligibility["run"] = str(eligibility["run"]).replace("-eq 3", "-ge 1")
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "eq 3"):
            policy.validate(value)

    def test_shell_interpolated_seed_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build"]
        assert isinstance(build, dict)
        seed = policy.steps_by_name(build)["determine run seed"]
        seed["run"] = 'SEED="${{ inputs.run_seed }}"'
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "through env"):
            policy.validate(value)

    def test_raw_artifact_retention_is_enforced(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        measure = jobs["measure"]
        assert isinstance(measure, dict)
        raw = policy.steps_by_name(measure)["upload raw scaling shard"]
        raw_with = raw["with"]
        assert isinstance(raw_with, dict)
        raw_with["retention-days"] = 1
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "30 days"):
            policy.validate(value)

    def test_shard_artifact_cannot_overwrite_merged_raw_report(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        measure = jobs["measure"]
        assemble = jobs["assemble"]
        assert isinstance(measure, dict) and isinstance(assemble, dict)
        raw = policy.steps_by_name(measure)["upload raw scaling shard"]
        merged = policy.steps_by_name(assemble)["upload merged raw scaling artifact"]
        raw_with = raw["with"]
        merged_with = merged["with"]
        assert isinstance(raw_with, dict) and isinstance(merged_with, dict)
        raw_with["name"] = merged_with["name"]
        with self.assertRaisesRegex(policy.ScalingWorkflowError, "dedicated raw artifact|distinct"):
            policy.validate(value)


if __name__ == "__main__":
    unittest.main()
