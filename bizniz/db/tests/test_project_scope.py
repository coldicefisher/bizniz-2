"""
Unit tests for ProjectScope (unified DB replacement for ProjectDB).

Uses SQLite in-memory backend — no MySQL required.
"""

import json
import pytest

from bizniz.db.bizniz_db import BiznizDB


@pytest.fixture
def db():
    with BiznizDB("sqlite:///:memory:") as d:
        yield d


@pytest.fixture
def ps(db):
    return db.for_project("test-project")


# ── Services ────────────────────────────────────────────────────────────────────

def test_save_and_get_service(ps):
    ps.save_service("backend", "api", "flask", "python", "/tmp/backend")
    row = ps.get_service("backend")
    assert row is not None
    assert row["name"] == "backend"
    assert row["service_type"] == "api"
    assert row["framework"] == "flask"
    assert row["language"] == "python"
    assert row["workspace_path"] == "/tmp/backend"
    assert row["status"] == "open"
    assert row["image_name"] is None


def test_save_service_with_image(ps):
    ps.save_service("frontend", "web", "react", "javascript", "/tmp/frontend", image_name="frontend:latest")
    row = ps.get_service("frontend")
    assert row["image_name"] == "frontend:latest"


def test_get_service_returns_none_for_missing(ps):
    assert ps.get_service("nonexistent") is None


def test_get_services(ps):
    ps.save_service("api", "backend", "flask", "python", "/tmp/api")
    ps.save_service("web", "frontend", "react", "javascript", "/tmp/web")
    rows = ps.get_services()
    assert len(rows) == 2
    names = [r["name"] for r in rows]
    assert "api" in names
    assert "web" in names


def test_update_service_status(ps):
    ps.save_service("api", "backend", "flask", "python", "/tmp/api")
    ps.update_service_status("api", "building")
    row = ps.get_service("api")
    assert row["status"] == "building"


def test_update_service_image(ps):
    ps.save_service("api", "backend", "flask", "python", "/tmp/api")
    ps.update_service_image("api", "api:v2")
    row = ps.get_service("api")
    assert row["image_name"] == "api:v2"


def test_service_name_upsert(ps):
    ps.save_service("api", "backend", "flask", "python", "/tmp/api")
    ps.save_service("api", "backend", "django", "python", "/tmp/api2")
    row = ps.get_service("api")
    assert row["framework"] == "django"
    assert row["workspace_path"] == "/tmp/api2"
    assert row["status"] == "open"


# ── Architecture Snapshots ──────────────────────────────────────────────────────

def test_save_architecture_snapshot(ps):
    ps.save_architecture_snapshot('{"services": ["api"]}', "Initial architecture")
    changes = ps.get_architecture_changes()
    assert len(changes) == 1
    assert changes[0]["snapshot_json"] == '{"services": ["api"]}'
    assert changes[0]["description"] == "Initial architecture"
    assert changes[0]["version"] == 1


def test_architecture_version_auto_increments(ps):
    ps.save_architecture_snapshot('{"v": 1}', "v1")
    ps.save_architecture_snapshot('{"v": 2}', "v2")
    changes = ps.get_architecture_changes()
    assert len(changes) == 2
    assert changes[0]["version"] == 1
    assert changes[1]["version"] == 2


def test_get_latest_architecture(ps):
    ps.save_architecture_snapshot('{"v": 1}', "first")
    ps.save_architecture_snapshot('{"v": 2}', "second")
    latest = ps.get_latest_architecture()
    assert latest is not None
    assert latest["version"] == 2
    assert latest["description"] == "second"


def test_get_latest_architecture_returns_none_when_empty(ps):
    assert ps.get_latest_architecture() is None


# ── Issue Log ───────────────────────────────────────────────────────────────────

def test_log_and_get_issue(ps):
    ps.log_issue("api", "Fix auth", "Auth is broken")
    issues = ps.get_all_issues()
    assert len(issues) == 1
    assert issues[0]["service_name"] == "api"
    assert issues[0]["issue_title"] == "Fix auth"
    assert issues[0]["issue_description"] == "Auth is broken"
    assert issues[0]["status"] == "open"


def test_log_issue_custom_status(ps):
    ps.log_issue("api", "Task", "Desc", status="in_progress")
    issues = ps.get_all_issues()
    assert issues[0]["status"] == "in_progress"


def test_update_issue(ps):
    issue_id = ps.log_issue("api", "Task", "Desc")
    ps.update_issue(issue_id, "in_progress", strategy_used="coder", iterations=3)
    issues = ps.get_all_issues()
    assert issues[0]["status"] == "in_progress"
    assert issues[0]["strategy_used"] == "coder"
    assert issues[0]["iterations"] == 3


def test_close_issue(ps):
    issue_id = ps.log_issue("api", "Task", "Desc")
    ps.close_issue(issue_id, strategy_used="debugger", iterations=5)
    issues = ps.get_all_issues()
    assert issues[0]["status"] == "closed"
    assert issues[0]["closed_at"] is not None
    assert issues[0]["strategy_used"] == "debugger"
    assert issues[0]["iterations"] == 5


def test_get_all_issues_filtered_by_service(ps):
    ps.log_issue("api", "Task 1", "Desc 1")
    ps.log_issue("web", "Task 2", "Desc 2")
    api_issues = ps.get_all_issues(service_name="api")
    assert len(api_issues) == 1
    assert api_issues[0]["service_name"] == "api"


def test_get_open_issues(ps):
    id1 = ps.log_issue("api", "Task 1", "Desc 1")
    id2 = ps.log_issue("api", "Task 2", "Desc 2")
    ps.close_issue(id1)

    open_issues = ps.get_open_issues()
    assert len(open_issues) == 1
    assert open_issues[0]["id"] == id2


def test_get_open_issues_filtered_by_service(ps):
    ps.log_issue("api", "Task 1", "Desc 1")
    ps.log_issue("web", "Task 2", "Desc 2")
    open_api = ps.get_open_issues(service_name="api")
    assert len(open_api) == 1
    assert open_api[0]["service_name"] == "api"


# ── Build Log ───────────────────────────────────────────────────────────────────

def test_log_build_event(ps):
    ps.log_build_event("api", "image_build", True, "Built successfully")
    logs = ps.get_build_log()
    assert len(logs) == 1
    assert logs[0]["service_name"] == "api"
    assert logs[0]["event_type"] == "image_build"
    assert logs[0]["success"] == 1
    assert logs[0]["detail"] == "Built successfully"


def test_log_build_event_failure(ps):
    ps.log_build_event("api", "image_build", False, "Dockerfile error")
    logs = ps.get_build_log()
    assert logs[0]["success"] == 0


def test_get_build_log_filtered_by_service(ps):
    ps.log_build_event("api", "image_build", True)
    ps.log_build_event("web", "package_install", True)
    api_logs = ps.get_build_log(service_name="api")
    assert len(api_logs) == 1
    assert api_logs[0]["service_name"] == "api"


def test_build_event_type_constraint(ps):
    with pytest.raises(Exception):
        ps.log_build_event("api", "invalid_type", True)


# ── Drift Events ────────────────────────────────────────────────────────────────

def test_log_drift(ps):
    ps.log_drift("api", ["models.py", "views.py"], "Regenerated files")
    events = ps.get_drift_events()
    assert len(events) == 1
    assert events[0]["service_name"] == "api"
    assert json.loads(events[0]["drift_files_json"]) == ["models.py", "views.py"]
    assert events[0]["resolution"] == "Regenerated files"


def test_log_drift_default_resolution(ps):
    ps.log_drift("api", ["config.py"])
    events = ps.get_drift_events()
    assert events[0]["resolution"] == ""


def test_get_drift_events_filtered_by_service(ps):
    ps.log_drift("api", ["a.py"])
    ps.log_drift("web", ["b.py"])
    api_events = ps.get_drift_events(service_name="api")
    assert len(api_events) == 1
    assert api_events[0]["service_name"] == "api"


# ── Scope isolation ──────────────────────────────────────────────────────────────

def test_different_projects_isolated(db):
    ps1 = db.for_project("proj-a")
    ps2 = db.for_project("proj-b")

    ps1.save_service("api", "backend", "flask", "python", "/tmp/a")
    ps2.save_service("api", "backend", "django", "python", "/tmp/b")

    assert len(ps1.get_services()) == 1
    assert ps1.get_service("api")["framework"] == "flask"
    assert len(ps2.get_services()) == 1
    assert ps2.get_service("api")["framework"] == "django"


# ── Context Manager ─────────────────────────────────────────────────────────────

def test_context_manager(db):
    with db.for_project("proj") as ps:
        ps.save_service("api", "backend", "flask", "python", "/tmp/api")

    ps2 = db.for_project("proj")
    row = ps2.get_service("api")
    assert row["name"] == "api"
