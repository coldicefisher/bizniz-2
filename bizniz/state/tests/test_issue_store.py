"""Tests for IssueStateStore — DB-backed single-source-of-truth for
issue-level IMPLEMENT phase state."""
import json
from pathlib import Path

import pytest

from bizniz.coder.types import CoderResult, Issue
from bizniz.project.project import Project
from bizniz.state.issue_store import IssueStateStore, ResumeBehavior


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    project = Project(root=tmp_path, project_name="test")
    return IssueStateStore(
        db=project.db, job_id="J1", milestone_index=1,
    )


def _issue(id_, deps=None):
    return Issue(
        id=id_, title=f"Implement {id_}", description="d",
        service="backend", language="python",
        target_files=[f"app/{id_.lower()}.py"],
        test_files=[f"tests/test_{id_.lower()}.py"],
        success_criteria=["compiles"],
        spec_refs=[f"cap_{id_.lower()}"],
        depends_on=deps or [],
    )


# ── Planning ───────────────────────────────────────────────────────────


class TestPlanning:
    def test_record_planned_persists_all_fields(self, store):
        issues = [_issue("BE-001"), _issue("BE-002", deps=["BE-001"])]
        store.record_planned("backend", issues)

        rows = store.all_rows()
        assert len(rows) == 2
        row = rows[0]
        assert row["job_id"] == "J1"
        assert row["milestone_index"] == 1
        assert row["service"] == "backend"
        assert row["issue_id"] == "BE-001"
        assert row["status"] == "pending"
        assert json.loads(row["target_files"]) == ["app/be-001.py"]

    def test_record_planned_idempotent(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        store.record_planned("backend", [_issue("BE-001")])  # again
        assert len(store.all_rows()) == 1

    def test_record_planned_preserves_runtime_state(self, store):
        # Simulate: plan, run, finish, replan — runtime state should
        # NOT be wiped by the second plan.
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_started("backend", "BE-001", "lite")
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed", summary="ok"),
        )
        # Replan with same issue — should keep status=passed
        store.record_planned("backend", [_issue("BE-001")])
        rows = store.all_rows()
        assert rows[0]["status"] == "passed"


# ── Resume gating ──────────────────────────────────────────────────────


class TestResumeGating:
    def test_unknown_issue_redispatches(self, store):
        assert store.resume_decision("backend", "BE-999") == ResumeBehavior.REDISPATCH

    def test_passed_issue_skipped(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_started("backend", "BE-001", "lite")
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed"),
        )
        assert store.resume_decision("backend", "BE-001") == ResumeBehavior.SKIP

    def test_partial_issue_skipped_on_resume(self, store):
        # All tiers exhausted; partial is terminal. Resume should not
        # rerun — manual intervention or --force would be required.
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_started("backend", "BE-001", "lite")
        store.mark_finished(
            "backend", "BE-001", status="partial",
            result=CoderResult(issue_id="BE-001", status="partial"),
        )
        assert store.resume_decision("backend", "BE-001") == ResumeBehavior.SKIP

    def test_stalled_issue_skipped_on_resume(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_finished("backend", "BE-001", status="stalled", error="boom")
        assert store.resume_decision("backend", "BE-001") == ResumeBehavior.SKIP

    def test_running_issue_redispatches(self, store):
        # Killed mid-attempt — workspace may be partial. Redispatch.
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_started("backend", "BE-001", "lite")  # status=running
        assert store.resume_decision("backend", "BE-001") == ResumeBehavior.REDISPATCH

    def test_previous_outcome_only_for_terminal(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        # No outcome before any state mark
        assert store.previous_outcome("backend", "BE-001") is None
        # Even after mark_started, status is "running" — not terminal
        store.mark_started("backend", "BE-001", "lite")
        assert store.previous_outcome("backend", "BE-001") is None
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed", summary="ok"),
        )
        prior = store.previous_outcome("backend", "BE-001")
        assert prior is not None
        assert prior.disposition == "passed"


# ── Tier tracking ──────────────────────────────────────────────────────


class TestTierTracking:
    def test_tiers_used_appended(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_started("backend", "BE-001", "lite")
        store.mark_started("backend", "BE-001", "flash-top")
        store.mark_started("backend", "BE-001", "pro")

        row = store.all_rows()[0]
        assert json.loads(row["tiers_used"]) == ["lite", "flash-top", "pro"]
        assert row["current_tier"] == "pro"

    def test_same_tier_not_double_appended(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_started("backend", "BE-001", "lite")
        store.mark_started("backend", "BE-001", "lite")  # same tier
        row = store.all_rows()[0]
        assert json.loads(row["tiers_used"]) == ["lite"]


# ── is_implement_done ──────────────────────────────────────────────────


class TestImplementDone:
    def test_empty_returns_false(self, store):
        assert not store.is_implement_done()

    def test_some_pending_returns_false(self, store):
        store.record_planned("backend", [_issue("BE-001"), _issue("BE-002")])
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed"),
        )
        # BE-002 still pending
        assert not store.is_implement_done()

    def test_all_terminal_returns_true(self, store):
        store.record_planned("backend", [_issue("BE-001"), _issue("BE-002")])
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed"),
        )
        store.mark_finished("backend", "BE-002", status="stalled", error="x")
        assert store.is_implement_done()


# ── EngineerResult assembly ────────────────────────────────────────────


class TestAssembleEngineerResult:
    def test_all_passed_yields_passed_status(self, store):
        store.record_planned("backend", [_issue("BE-001"), _issue("BE-002")])
        for iid in ("BE-001", "BE-002"):
            store.mark_finished(
                "backend", iid, status="passed",
                result=CoderResult(issue_id=iid, status="passed"),
            )
        result = store.assemble_engineer_result()
        assert result.final_test_status == "passed"
        assert set(result.completed_issue_ids) == {"BE-001", "BE-002"}
        assert all(i.status == "done" for i in result.plan.issues)

    def test_partial_yields_partial_status(self, store):
        store.record_planned("backend", [_issue("BE-001"), _issue("BE-002")])
        store.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed"),
        )
        store.mark_finished("backend", "BE-002", status="stalled", error="x")
        result = store.assemble_engineer_result()
        assert result.final_test_status == "partial"
        assert "BE-001" in result.completed_issue_ids
        assert "BE-002" in result.deferred_issue_ids

    def test_all_failed_yields_failed_status(self, store):
        store.record_planned("backend", [_issue("BE-001")])
        store.mark_finished("backend", "BE-001", status="stalled", error="x")
        result = store.assemble_engineer_result()
        assert result.final_test_status == "failed"

    def test_empty_yields_not_run(self, store):
        result = store.assemble_engineer_result()
        assert result.final_test_status == "not_run"

    def test_status_mapping(self, store):
        store.record_planned("backend", [
            _issue("A"), _issue("B"), _issue("C"), _issue("D"), _issue("E"),
        ])
        store.mark_finished(
            "backend", "A", status="passed",
            result=CoderResult(issue_id="A", status="passed"),
        )
        store.mark_finished(
            "backend", "B", status="escalated",
            result=CoderResult(issue_id="B", status="passed"),
        )
        store.mark_finished("backend", "C", status="stalled", error="x")
        store.mark_finished("backend", "D", status="skipped", error="dep")
        store.mark_finished("backend", "E", status="deferred")

        result = store.assemble_engineer_result()
        statuses = {i.id: i.status for i in result.plan.issues}
        assert statuses["A"] == "done"
        assert statuses["B"] == "done"
        assert statuses["C"] == "blocked"
        assert statuses["D"] == "skipped"
        assert statuses["E"] == "skipped"


# ── Cross-job isolation ────────────────────────────────────────────────


class TestCrossJobIsolation:
    def test_two_jobs_dont_collide(self, tmp_path):
        project = Project(root=tmp_path, project_name="test")
        s_a = IssueStateStore(db=project.db, job_id="A", milestone_index=1)
        s_b = IssueStateStore(db=project.db, job_id="B", milestone_index=1)

        s_a.record_planned("backend", [_issue("BE-001")])
        s_a.mark_finished(
            "backend", "BE-001", status="passed",
            result=CoderResult(issue_id="BE-001", status="passed"),
        )

        # Job B sees no rows for the same milestone/service/issue
        assert s_b.resume_decision("backend", "BE-001") == ResumeBehavior.REDISPATCH
        assert not s_b.is_implement_done()
