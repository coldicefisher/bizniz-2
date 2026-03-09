import json
import pytest
from pathlib import Path

from bizniz.project.project import Project
from bizniz.project.project_db import ProjectDB


@pytest.fixture
def project(tmp_path):
    return Project(root=tmp_path / "testproj", project_name="Test Project")


@pytest.fixture
def db(project):
    with ProjectDB(project) as d:
        yield d


# ── DB file creation ────────────────────────────────────────────────────────────

def test_db_file_created(project):
    db = ProjectDB(project)
    db.close()
    assert (project.root / ".bizniz" / "project.db").exists()


# ── Services ────────────────────────────────────────────────────────────────────

def test_save_and_get_service(db):
    sid = db.save_service("backend", "api", "flask", "python", "/tmp/backend")
    row = db.get_service("backend")
    assert row is not None
    assert row["name"] == "backend"
    assert row["service_type"] == "api"
    assert row["framework"] == "flask"
    assert row["language"] == "python"
    assert row["workspace_path"] == "/tmp/backend"
    assert row["status"] == "open"
    assert row["image_name"] is None


def test_save_service_with_image(db):
    db.save_service("frontend", "web", "react", "javascript", "/tmp/frontend", image_name="frontend:latest")
    row = db.get_service("frontend")
    assert row["image_name"] == "frontend:latest"


def test_get_service_returns_none_for_missing(db):
    assert db.get_service("nonexistent") is None


def test_get_services(db):
    db.save_service("api", "backend", "flask", "python", "/tmp/api")
    db.save_service("web", "frontend", "react", "javascript", "/tmp/web")
    rows = db.get_services()
    assert len(rows) == 2
    names = [r["name"] for r in rows]
    assert "api" in names
    assert "web" in names


def test_update_service_status(db):
    db.save_service("api", "backend", "flask", "python", "/tmp/api")
    db.update_service_status("api", "building")
    row = db.get_service("api")
    assert row["status"] == "building"


def test_update_service_image(db):
    db.save_service("api", "backend", "flask", "python", "/tmp/api")
    db.update_service_image("api", "api:v2")
    row = db.get_service("api")
    assert row["image_name"] == "api:v2"


def test_service_name_upsert(db):
    db.save_service("api", "backend", "flask", "python", "/tmp/api")
    db.save_service("api", "backend", "django", "python", "/tmp/api2")
    row = db.get_service("api")
    assert row["framework"] == "django"
    assert row["workspace_path"] == "/tmp/api2"
    assert row["status"] == "open"


# ── Architecture Snapshots ──────────────────────────────────────────────────────

def test_save_architecture_snapshot(db):
    snap_id = db.save_architecture_snapshot('{"services": ["api"]}', "Initial architecture")
    changes = db.get_architecture_changes()
    assert len(changes) == 1
    assert changes[0]["snapshot_json"] == '{"services": ["api"]}'
    assert changes[0]["description"] == "Initial architecture"
    assert changes[0]["version"] == 1


def test_architecture_version_auto_increments(db):
    db.save_architecture_snapshot('{"v": 1}', "v1")
    db.save_architecture_snapshot('{"v": 2}', "v2")
    changes = db.get_architecture_changes()
    assert len(changes) == 2
    assert changes[0]["version"] == 1
    assert changes[1]["version"] == 2


def test_get_latest_architecture(db):
    db.save_architecture_snapshot('{"v": 1}', "first")
    db.save_architecture_snapshot('{"v": 2}', "second")
    latest = db.get_latest_architecture()
    assert latest is not None
    assert latest["version"] == 2
    assert latest["description"] == "second"


def test_get_latest_architecture_returns_none_when_empty(db):
    assert db.get_latest_architecture() is None


# ── Issue Log ───────────────────────────────────────────────────────────────────

def test_log_and_get_issue(db):
    issue_id = db.log_issue("api", "Fix auth", "Auth is broken")
    issues = db.get_all_issues()
    assert len(issues) == 1
    assert issues[0]["service_name"] == "api"
    assert issues[0]["issue_title"] == "Fix auth"
    assert issues[0]["issue_description"] == "Auth is broken"
    assert issues[0]["status"] == "open"


def test_log_issue_custom_status(db):
    issue_id = db.log_issue("api", "Task", "Desc", status="in_progress")
    issues = db.get_all_issues()
    assert issues[0]["status"] == "in_progress"


def test_update_issue(db):
    issue_id = db.log_issue("api", "Task", "Desc")
    db.update_issue(issue_id, "in_progress", strategy_used="autocoder", iterations=3)
    issues = db.get_all_issues()
    assert issues[0]["status"] == "in_progress"
    assert issues[0]["strategy_used"] == "autocoder"
    assert issues[0]["iterations"] == 3


def test_close_issue(db):
    issue_id = db.log_issue("api", "Task", "Desc")
    db.close_issue(issue_id, strategy_used="debugger", iterations=5)
    issues = db.get_all_issues()
    assert issues[0]["status"] == "closed"
    assert issues[0]["closed_at"] is not None
    assert issues[0]["strategy_used"] == "debugger"
    assert issues[0]["iterations"] == 5


def test_get_all_issues_filtered_by_service(db):
    db.log_issue("api", "Task 1", "Desc 1")
    db.log_issue("web", "Task 2", "Desc 2")
    api_issues = db.get_all_issues(service_name="api")
    assert len(api_issues) == 1
    assert api_issues[0]["service_name"] == "api"


def test_get_open_issues(db):
    id1 = db.log_issue("api", "Task 1", "Desc 1")
    id2 = db.log_issue("api", "Task 2", "Desc 2")
    db.close_issue(id1)

    open_issues = db.get_open_issues()
    assert len(open_issues) == 1
    assert open_issues[0]["id"] == id2


def test_get_open_issues_filtered_by_service(db):
    db.log_issue("api", "Task 1", "Desc 1")
    db.log_issue("web", "Task 2", "Desc 2")
    open_api = db.get_open_issues(service_name="api")
    assert len(open_api) == 1
    assert open_api[0]["service_name"] == "api"


# ── Build Log ───────────────────────────────────────────────────────────────────

def test_log_build_event(db):
    bid = db.log_build_event("api", "image_build", True, "Built successfully")
    logs = db.get_build_log()
    assert len(logs) == 1
    assert logs[0]["service_name"] == "api"
    assert logs[0]["event_type"] == "image_build"
    assert logs[0]["success"] == 1
    assert logs[0]["detail"] == "Built successfully"


def test_log_build_event_failure(db):
    db.log_build_event("api", "image_build", False, "Dockerfile error")
    logs = db.get_build_log()
    assert logs[0]["success"] == 0


def test_get_build_log_filtered_by_service(db):
    db.log_build_event("api", "image_build", True)
    db.log_build_event("web", "package_install", True)
    api_logs = db.get_build_log(service_name="api")
    assert len(api_logs) == 1
    assert api_logs[0]["service_name"] == "api"


def test_build_event_type_constraint(db):
    with pytest.raises(Exception):
        db.log_build_event("api", "invalid_type", True)


# ── Drift Events ────────────────────────────────────────────────────────────────

def test_log_drift(db):
    did = db.log_drift("api", ["models.py", "views.py"], "Regenerated files")
    events = db.get_drift_events()
    assert len(events) == 1
    assert events[0]["service_name"] == "api"
    assert json.loads(events[0]["drift_files_json"]) == ["models.py", "views.py"]
    assert events[0]["resolution"] == "Regenerated files"


def test_log_drift_default_resolution(db):
    db.log_drift("api", ["config.py"])
    events = db.get_drift_events()
    assert events[0]["resolution"] == ""


def test_get_drift_events_filtered_by_service(db):
    db.log_drift("api", ["a.py"])
    db.log_drift("web", ["b.py"])
    api_events = db.get_drift_events(service_name="api")
    assert len(api_events) == 1
    assert api_events[0]["service_name"] == "api"


# ── Context Manager ─────────────────────────────────────────────────────────────

def test_context_manager(project):
    with ProjectDB(project) as db:
        db.save_service("api", "backend", "flask", "python", "/tmp/api")
    # After __exit__, connection is closed; re-open to verify persistence
    with ProjectDB(project) as db2:
        row = db2.get_service("api")
        assert row["name"] == "api"
