"""qa_pipeline/checks/report.py — pure rendering/aggregation logic. ARTIFACTS_BUCKET is ""
in a test process (ONCA_QA_ARTIFACTS_BUCKET unset), which makes upload_artifact()/_presign()
safe no-ops — no AWS mocking needed to exercise the logic under test."""
from __future__ import annotations

import pytest

from qa_pipeline.checks import report

ALL_OK_BRANCHES = [
    [{"ok": True, "browser": "chromium", "viewport": "desktop"}],
    {"ok": True, "checks": [{"name": "login", "ok": True, "detail": ""}]},
    {"ok": True, "checks": [{"name": "hash", "ok": True, "detail": ""}]},
    {"ok": True, "checks": [{"name": "route_ok:/exec", "ok": True, "detail": ""}]},
    {"ok": True, "findings": []},
]


def test_all_hard_gates_ok_returns_normally():
    event = {"run_id": "test-run-1", "branches": ALL_OK_BRANCHES}
    result = report.run(event)
    assert result["ok"] is True
    assert result["hard_gates_ok"] is True


def test_a_failed_hard_gate_branch_raises_after_building_the_report():
    """The report itself must still be BUILT (no exception during rendering) even though
    the overall call raises — that's the whole point (ADR 027 #133): the execution must
    fail (for the CloudWatch alarm) but the report must not disappear."""
    branches = list(ALL_OK_BRANCHES)
    branches[1] = {"ok": False, "checks": [{"name": "login", "ok": False, "detail": "boom"}]}
    event = {"run_id": "test-run-2", "branches": branches}
    with pytest.raises(AssertionError, match="hard gate"):
        report.run(event)


def test_a_failed_matrix_shard_counts_as_a_failed_hard_gate():
    branches = list(ALL_OK_BRANCHES)
    branches[0] = [{"ok": True}, {"ok": False}]
    event = {"run_id": "test-run-3", "branches": branches}
    with pytest.raises(AssertionError):
        report.run(event)


def test_vision_findings_never_affect_hard_gates_ok():
    """Vision is advisory (#132) — even a fully-flagged vision branch must not fail the
    report/execution."""
    branches = list(ALL_OK_BRANCHES)
    branches[4] = {"ok": True, "findings": [
        {"panel": "mapa_competitivo", "captured": True, "available": True,
         "checklist": [{"item": "x", "pass": False, "confidence": 0.9, "note": "flagged"}]},
    ]}
    event = {"run_id": "test-run-4", "branches": branches}
    result = report.run(event)
    assert result["hard_gates_ok"] is True


def test_missing_branches_default_gracefully_not_a_crash():
    """An error-shaped branch from a .add_catch() (no 'checks'/'ok' key at all) must not
    crash rendering — it should just read as a failed hard gate."""
    event = {"run_id": "test-run-5", "branches": [
        [], {"error": {"Error": "States.TaskFailed"}}, {}, {}, {},
    ]}
    with pytest.raises(AssertionError):
        report.run(event)
