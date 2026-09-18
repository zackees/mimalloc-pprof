from __future__ import annotations

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
# ruff: noqa: I001

import copy
import unittest
from typing import cast

import benchmark_report as report
import check_benchmark_pprof_tax_workflow as policy


class BenchmarkPprofTaxWorkflowTests(unittest.TestCase):
    def workflow(self) -> dict[str, object]:
        return policy.load(policy.WORKFLOW)

    def test_production_workflow_passes(self) -> None:
        policy.validate(self.workflow())

    def test_weekly_cadence_is_required(self) -> None:
        for schedule in ([], [{"cron": "37 4 1 * *"}], [{}]):
            with self.subTest(schedule=schedule):
                value = self.workflow()
                raw = cast(dict[object, object], value)  # PyYAML 1.1 may use True for `on`.
                triggers = raw.get("on", raw.get(True))
                assert isinstance(triggers, dict)
                triggers["schedule"] = schedule
                with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "schedule"):
                    policy.validate(value)

    def test_selftest_negative_controls_all_fail_closed(self) -> None:
        # The selftest is the real guard against a checker that checks nothing.
        policy.selftest(policy.WORKFLOW, policy.PPROF_TAX_SOURCE)
        self.assertGreaterEqual(len(policy.MUTATIONS), 15)
        self.assertGreaterEqual(len(policy.SOURCE_MUTATIONS), 5)

    def test_production_source_matches_the_validator(self) -> None:
        policy.validate_source_contract(policy.PPROF_TAX_SOURCE.read_text(encoding="utf-8"))

    def test_rust_min_blocks_drifting_from_python_is_rejected(self) -> None:
        # The failure this exists to catch: someone edits the publishable-run floor on
        # one side only, and the mismatch surfaces only after a scheduled run has spent
        # its whole budget producing a report the validator throws away.
        source = policy.PPROF_TAX_SOURCE.read_text(encoding="utf-8")
        drifted = source.replace(
            f"PPROF_TAX_MIN_BLOCKS: u32 = {report.PPROF_TAX_MIN_BLOCKS};",
            "PPROF_TAX_MIN_BLOCKS: u32 = 1;",
        )
        self.assertNotEqual(drifted, source)
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "PPROF_TAX_MIN_BLOCKS"):
            policy.validate_source_contract(drifted)

    def test_rust_min_blocks_drift_is_rejected_via_monkeypatch(self) -> None:
        # Monkeypatching the parsed-value side (rather than mutating the real file) must
        # still catch a constant disagreeing with the source it is compared against.
        source_text = "pub const PPROF_TAX_MIN_BLOCKS: u32 = 7;\n"
        original = policy.PPROF_TAX_MIN_BLOCKS
        policy.PPROF_TAX_MIN_BLOCKS = 15
        try:
            with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "PPROF_TAX_MIN_BLOCKS"):
                policy.validate_source_contract(source_text)
        finally:
            policy.PPROF_TAX_MIN_BLOCKS = original

    def test_timeout_budget_over_sixty_minutes_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        publication_audit = jobs["publication-audit"]
        assert isinstance(publication_audit, dict)
        publication_audit["timeout-minutes"] = 30
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "60"):
            policy.validate(value)

    def test_parallel_matrix_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        build["strategy"] = {"matrix": {"configuration": ["a", "b"]}}
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "parallel matrices"):
            policy.validate(value)

    def test_separate_concurrency_group_is_rejected(self) -> None:
        value = copy.deepcopy(self.workflow())
        value["concurrency"] = {"group": "pprof-tax-only", "cancel-in-progress": False}
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "serialize"):
            policy.validate(value)

    def test_blocks_default_below_the_floor_is_rejected(self) -> None:
        value = self.workflow()
        raw = cast(dict[object, object], value)  # PyYAML 1.1 may use True for `on`.
        triggers = raw.get("on", raw.get(True))
        assert isinstance(triggers, dict)
        dispatch = triggers["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        inputs = dispatch["inputs"]
        assert isinstance(inputs, dict)
        blocks = inputs["blocks"]
        assert isinstance(blocks, dict)
        blocks["default"] = 14
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "at least"):
            policy.validate(value)

    def test_smoke_mode_becoming_publishable_is_rejected(self) -> None:
        # If the smoke-vs-full boundary in the measurement step erodes, a smoke run
        # (never overlaid onto the public site) could start satisfying the same
        # numeric bound the checker requires for a publishable run.
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        matrix = policy.steps_by_name(build)["run pprof-tax matrix"]
        matrix["run"] = str(matrix["run"]).replace("-lt 15", "-lt 999")
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "smoke"):
            policy.validate(value)

    def test_eligibility_must_pin_main_full_and_fifteen_blocks(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        eligibility = policy.steps_by_name(build)["compute publication eligibility"]
        eligibility["run"] = str(eligibility["run"]).replace("-ge 15", "-ge 1")
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "ge 15"):
            policy.validate(value)

    def test_shell_interpolated_seed_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        seed = policy.steps_by_name(build)["determine run seed"]
        seed["run"] = 'SEED="${{ inputs.run_seed }}"'
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "through env"):
            policy.validate(value)

    def test_raw_artifact_always_upload_is_enforced(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        raw = policy.steps_by_name(build)["upload raw pprof-tax artifact"]
        raw["if"] = "success()"
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "always"):
            policy.validate(value)

    def test_raw_artifact_retention_is_enforced(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        raw = policy.steps_by_name(build)["upload raw pprof-tax artifact"]
        raw_with = raw["with"]
        assert isinstance(raw_with, dict)
        raw_with["retention-days"] = 1
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "30 days"):
            policy.validate(value)

    def test_unpinned_action_is_rejected(self) -> None:
        value = self.workflow()
        jobs = value["jobs"]
        assert isinstance(jobs, dict)
        build = jobs["build-and-measure"]
        assert isinstance(build, dict)
        steps = build["steps"]
        assert isinstance(steps, list)
        first = steps[0]
        assert isinstance(first, dict)
        first["uses"] = "actions/checkout@v4"
        with self.assertRaisesRegex(policy.PprofTaxWorkflowError, "tag ref"):
            policy.validate(value)

    def test_selftest_cli_reports_success(self) -> None:
        policy.selftest(policy.WORKFLOW, policy.PPROF_TAX_SOURCE)


if __name__ == "__main__":
    unittest.main()
