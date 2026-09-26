"""External CI must follow effective permission, not fork or author history."""

# pyright: reportMissingTypeStubs=false

import json
import sys
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pr_ci_gate import FULL_JOBS, failures, required_jobs
from pr_ci_mode import external_author


@pytest.mark.parametrize(
    ("permission", "external"),
    [("admin", False), ("write", False), ("read", True), ("none", True)],
)
def test_effective_permission_selects_external(permission: str, external: bool) -> None:
    with patch("pr_ci_mode.urllib.request.urlopen") as request:
        request.return_value.__enter__.return_value = BytesIO(
            json.dumps({"permission": permission}).encode()
        )
        assert external_author("zackees/mimalloc-pprof", "contributor", "token") is external
        assert request.call_args.args[0].full_url.endswith(
            "/repos/zackees/mimalloc-pprof/collaborators/contributor/permission"
        )


def test_permission_failure_is_not_a_minimal_ci_decision() -> None:
    with patch("pr_ci_mode.urllib.request.urlopen") as request:
        request.side_effect = urllib.error.HTTPError("url", 403, "forbidden", Message(), None)
        with pytest.raises(RuntimeError, match="cannot determine"):
            external_author("zackees/mimalloc-pprof", "contributor", "token")


def test_external_requires_every_full_root_without_label() -> None:
    for workflow, roots in FULL_JOBS.items():
        assert roots <= required_jobs(workflow, True, False, False, False)


def test_internal_minimal_and_labels_preserve_tiers() -> None:
    assert "build" not in required_jobs("c-unit", False, False, False, False)
    assert "build" in required_jobs("c-unit", False, True, False, False)
    assert "test" not in required_jobs("rust-native", False, True, False, False)
    assert "test" in required_jobs("rust-native", False, False, True, False)


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "neutral", None])
def test_external_gate_rejects_missing_or_unsuccessful_job(result: Optional[str]) -> None:
    needs: dict[str, object] = {
        job: {"result": "success"} for job in required_jobs("c-unit", True, False, False, False)
    }
    needs["pr-ci-mode"] = {"result": "success", "outputs": {"external": "true"}}
    if result is None:
        del needs["run-linux"]
    else:
        needs["run-linux"] = {"result": result}
    assert any("run-linux:" in error for error in failures("c-unit", needs, False, False))


def test_external_gate_accepts_only_all_successful_jobs() -> None:
    needs: dict[str, object] = {
        job: {"result": "success"} for job in required_jobs("c-unit", True, False, False, False)
    }
    needs["pr-ci-mode"] = {"result": "success", "outputs": {"external": "true"}}
    assert failures("c-unit", needs, False, False) == []


def test_missing_permission_decision_fails_closed() -> None:
    needs: dict[str, object] = {"pr-ci-mode": {"result": "success", "outputs": {}}}
    assert failures("asan", needs, False, False)


def test_gate_rejects_different_candidate_sha() -> None:
    needs: dict[str, object] = {
        job: {"result": "success"} for job in required_jobs("cross", True, False, False, False)
    }
    needs["pr-ci-mode"] = {"result": "success", "outputs": {"external": "true"}}
    needs["resolve-candidate"] = {"result": "success", "outputs": {"sha": "a" * 40}}
    assert any(
        "resolve-candidate: expected" in error
        for error in failures("cross", needs, False, False, "b" * 40)
    )
