"""Tests for CostTracker DB persistence + ProjectDB cost methods."""
from pathlib import Path

import pytest

from bizniz.cost.tracker import CostTracker
from bizniz.project.project import Project
from bizniz.project.project_db import ProjectDB


@pytest.fixture
def project(tmp_path):
    p = Project(root=tmp_path / "proj", project_name="Test Project")
    p.create_structure()
    return p


# ── ProjectDB schema ──────────────────────────────────────────────────────────

def test_jobs_table_exists(project):
    cur = project.db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
    )
    assert cur.fetchone() is not None


def test_api_calls_table_exists_with_indexes(project):
    cur = project.db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='api_calls'"
    )
    assert cur.fetchone() is not None
    cur = project.db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='api_calls'"
    )
    indexes = {row[0] for row in cur.fetchall()}
    assert "idx_api_calls_job" in indexes
    assert "idx_api_calls_issue" in indexes


# ── ProjectDB methods ─────────────────────────────────────────────────────────

def test_start_job_inserts_row(project):
    project.db.start_job("job-1", "myproj", "build a thing")
    job = project.db.get_job("job-1")
    assert job is not None
    assert job["status"] == "running"
    assert job["project_slug"] == "myproj"
    assert "build a thing" in job["problem_statement"]


def test_start_job_is_idempotent(project):
    project.db.start_job("job-1", "myproj", "first")
    project.db.start_job("job-1", "myproj", "second")  # should be no-op
    rows = project.db._conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE id='job-1'"
    ).fetchone()
    assert rows[0] == 1


def test_save_api_call_persists_with_full_context(project):
    from bizniz.cost.pricing import CallCost
    from bizniz.cost.tracker import CallRecord

    project.db.start_job("job-1", "myproj")
    rec = CallRecord(
        timestamp="2026-04-30T00:00:00Z",
        agent="autocoder",
        model="gemini-flash",
        input_tokens=1000,
        output_tokens=500,
        duration_ms=2500,
        cost=CallCost(input_cost=0.0003, output_cost=0.00125,
                      total_cost=0.00155, model="gemini-3.1-flash-lite-preview", priced=True),
        problem_id=42,
        issue_id=7,
        job_id="job-1",
        service_name="backend",
        phase="phase2.gemini-flash",
    )
    rowid = project.db.save_api_call(rec)
    assert rowid is not None

    row = project.db._conn.execute(
        "SELECT * FROM api_calls WHERE id=?", (rowid,)
    ).fetchone()
    assert row["job_id"] == "job-1"
    assert row["agent"] == "autocoder"
    assert row["service_name"] == "backend"
    assert row["issue_id"] == 7
    assert row["phase"] == "phase2.gemini-flash"
    assert row["input_tokens"] == 1000
    assert row["total_cost"] == pytest.approx(0.00155)
    assert row["priced"] == 1


def test_finish_job_rolls_up_totals(project):
    from bizniz.cost.tracker import CallRecord
    from bizniz.cost.pricing import CallCost

    project.db.start_job("job-1", "myproj")

    def _rec(input_t, output_t, total):
        return CallRecord(
            timestamp="2026-04-30T00:00:00Z",
            agent="x", model="gpt-4o-mini",
            input_tokens=input_t, output_tokens=output_t, duration_ms=0,
            cost=CallCost(0, 0, total, "gpt-4o-mini", True),
            job_id="job-1",
        )

    project.db.save_api_call(_rec(1000, 500, 0.001))
    project.db.save_api_call(_rec(2000, 800, 0.002))
    project.db.save_api_call(_rec(500, 200, 0.0005))

    project.db.finish_job("job-1", status="succeeded")
    job = project.db.get_job("job-1")
    assert job["status"] == "succeeded"
    assert job["finished_at"] is not None
    assert job["total_calls"] == 3
    assert job["total_input_tokens"] == 3500
    assert job["total_output_tokens"] == 1500
    assert job["total_cost"] == pytest.approx(0.0035)


def test_cost_by_issue_aggregates(project):
    from bizniz.cost.tracker import CallRecord
    from bizniz.cost.pricing import CallCost

    project.db.start_job("job-1", "myproj")

    def _rec(issue_id, total):
        return CallRecord(
            timestamp="x", agent="x", model="gpt-4o-mini",
            input_tokens=100, output_tokens=50, duration_ms=0,
            cost=CallCost(0, 0, total, "gpt-4o-mini", True),
            issue_id=issue_id, job_id="job-1",
        )

    project.db.save_api_call(_rec(1, 0.01))
    project.db.save_api_call(_rec(1, 0.02))
    project.db.save_api_call(_rec(2, 0.05))
    project.db.save_api_call(_rec(None, 0.99))  # NULL issue dropped

    rollup = {row["issue_id"]: row for row in project.db.cost_by_issue("job-1")}
    assert set(rollup.keys()) == {1, 2}
    assert rollup[1]["total_cost"] == pytest.approx(0.03)
    assert rollup[2]["total_cost"] == pytest.approx(0.05)


def test_cost_by_service_aggregates(project):
    from bizniz.cost.tracker import CallRecord
    from bizniz.cost.pricing import CallCost

    project.db.start_job("job-1", "myproj")

    def _rec(service, total):
        return CallRecord(
            timestamp="x", agent="x", model="gpt-4o-mini",
            input_tokens=100, output_tokens=50, duration_ms=0,
            cost=CallCost(0, 0, total, "gpt-4o-mini", True),
            service_name=service, job_id="job-1",
        )

    project.db.save_api_call(_rec("backend", 0.10))
    project.db.save_api_call(_rec("backend", 0.05))
    project.db.save_api_call(_rec("frontend", 0.03))

    rollup = {row["service_name"]: row for row in project.db.cost_by_service("job-1")}
    assert rollup["backend"]["total_cost"] == pytest.approx(0.15)
    assert rollup["frontend"]["total_cost"] == pytest.approx(0.03)
    # backend dominates → first when ordered by total_cost desc
    rows = project.db.cost_by_service("job-1")
    assert rows[0]["service_name"] == "backend"


def test_cost_by_model_aggregates(project):
    from bizniz.cost.tracker import CallRecord
    from bizniz.cost.pricing import CallCost

    project.db.start_job("job-1", "myproj")

    def _rec(model, in_t, out_t, total):
        return CallRecord(
            timestamp="x", agent="x", model=model,
            input_tokens=in_t, output_tokens=out_t, duration_ms=0,
            cost=CallCost(0, 0, total, model, True),
            job_id="job-1",
        )

    project.db.save_api_call(_rec("gemini-flash", 1000, 500, 0.001))
    project.db.save_api_call(_rec("gemini-pro", 2000, 1500, 0.030))
    project.db.save_api_call(_rec("gemini-pro", 500, 300, 0.005))

    rollup = {row["model"]: row for row in project.db.cost_by_model("job-1")}
    assert rollup["gemini-pro"]["calls"] == 2
    assert rollup["gemini-pro"]["total_cost"] == pytest.approx(0.035)
    assert rollup["gemini-flash"]["calls"] == 1


# ── CostTracker integration ───────────────────────────────────────────────────

def test_tracker_buffers_records_before_db_attach(project):
    t = CostTracker()
    t.start_job(project_slug="x")
    t.record(agent="a", model="gpt-4o-mini", input_tokens=100, output_tokens=50)
    t.record(agent="b", model="gpt-4o-mini", input_tokens=200, output_tokens=100)

    # No DB yet — nothing persisted
    rows = project.db._conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()
    assert rows[0] == 0

    # Attach project DB — buffered records flush
    t.attach_project_db(project.db)
    rows = project.db._conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()
    assert rows[0] == 2


def test_tracker_live_persists_after_attach(project):
    t = CostTracker()
    t.start_job(project_slug="x")
    t.attach_project_db(project.db)
    t.record(agent="x", model="gpt-4o-mini", input_tokens=100, output_tokens=50)
    t.record(agent="x", model="gpt-4o-mini", input_tokens=200, output_tokens=100)
    rows = project.db._conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()
    assert rows[0] == 2


def test_tracker_set_context_propagates(project):
    t = CostTracker()
    t.start_job(project_slug="x")
    t.attach_project_db(project.db)
    t.set_service("backend")
    t.set_issue(7)
    t.set_phase("phase2.gemini-flash")
    t.record(agent="autocoder", model="gemini-flash",
             input_tokens=1000, output_tokens=500)

    row = project.db._conn.execute(
        "SELECT * FROM api_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["service_name"] == "backend"
    assert row["issue_id"] == 7
    assert row["phase"] == "phase2.gemini-flash"


def test_tracker_attach_does_not_double_persist(project):
    t = CostTracker()
    t.start_job(project_slug="x")
    t.record(agent="a", model="gpt-4o-mini", input_tokens=100, output_tokens=50)
    t.attach_project_db(project.db)  # flushes
    t.attach_project_db(project.db)  # should NOT re-write
    rows = project.db._conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()
    assert rows[0] == 1


def test_tracker_finish_job_updates_jobs_row(project):
    t = CostTracker()
    job_id = t.start_job(project_slug="x", problem_statement="build it")
    t.attach_project_db(project.db)
    project.db.start_job(job_id, "x", "build it")  # also write the row
    t.record(agent="a", model="gpt-4o-mini",
             input_tokens=1_000_000, output_tokens=500_000)
    t.finish_job(status="succeeded")
    row = project.db.get_job(job_id)
    assert row["status"] == "succeeded"
    assert row["total_calls"] == 1
    assert row["total_input_tokens"] == 1_000_000
    assert row["total_cost"] > 0


def test_tracker_reset_clears_state(project):
    t = CostTracker()
    t.start_job(project_slug="x")
    t.set_service("backend")
    t.record(agent="a", model="gpt-4o-mini", input_tokens=100, output_tokens=50)
    t.reset()
    assert t.records() == []
    assert t.current_job_id is None


def test_tracker_attach_workspace_db_alias(project):
    """Backward-compat shim — old callers passing workspace_db now hit
    project DB code path. Nothing should explode."""
    t = CostTracker()
    t.start_job(project_slug="x")
    t.attach_workspace_db(project.db)  # legacy name
    t.record(agent="x", model="gpt-4o-mini", input_tokens=100, output_tokens=50)
    rows = project.db._conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()
    assert rows[0] == 1
