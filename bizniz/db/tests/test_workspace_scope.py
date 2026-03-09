"""
Unit tests for WorkspaceScope (unified DB replacement for WorkspaceDB).

Uses SQLite in-memory backend — no MySQL required.
"""

import pytest

from bizniz.db.bizniz_db import BiznizDB


@pytest.fixture
def db():
    with BiznizDB("sqlite:///:memory:") as d:
        yield d


@pytest.fixture
def ws(db):
    return db.for_workspace("test-project", "test-service")


# ── Problems ────────────────────────────────────────────────────────────────────

def test_save_and_get_problem(ws):
    problem_id = ws.save_problem("Build a todo app.")
    row = ws.get_problem(problem_id)
    assert row is not None
    assert row["statement"] == "Build a todo app."
    assert row["id"] == problem_id


def test_get_problem_returns_none_for_missing(ws):
    assert ws.get_problem(9999) is None


# ── Requirements ────────────────────────────────────────────────────────────────

def test_save_and_get_requirements(ws):
    pid = ws.save_problem("Example problem")
    ws.save_requirement(pid, "business", "Users must be able to log in.")
    ws.save_requirement(pid, "functional", "The system must store sessions.")
    ws.save_requirement(pid, "nonfunctional", "Response time < 200 ms.")

    rows = ws.get_requirements(pid)
    assert len(rows) == 3


def test_get_requirements_filtered_by_type(ws):
    pid = ws.save_problem("Example")
    ws.save_requirement(pid, "business", "Req 1")
    ws.save_requirement(pid, "functional", "Req 2")

    biz = ws.get_requirements(pid, req_type="business")
    assert len(biz) == 1
    assert biz[0]["type"] == "business"


def test_requirement_invalid_type_raises(ws):
    pid = ws.save_problem("Example")
    with pytest.raises(Exception):
        ws.save_requirement(pid, "invalid_type", "Some text")


# ── Use Cases ───────────────────────────────────────────────────────────────────

def test_save_and_get_use_cases(ws):
    pid = ws.save_problem("Shopping cart system")
    ws.save_use_case(pid, "Add item to cart", "A user selects an item and adds it.")
    ws.save_use_case(pid, "Remove item", "A user removes an item from the cart.")

    rows = ws.get_use_cases(pid)
    assert len(rows) == 2
    titles = [r["title"] for r in rows]
    assert "Add item to cart" in titles


# ── Issues ──────────────────────────────────────────────────────────────────────

def test_save_and_get_issue(ws):
    pid = ws.save_problem("Some problem")
    issue_id = ws.save_issue(
        problem_id=pid,
        title="Implement login",
        description="Create a login endpoint.",
        target_files=[{"filepath": "login.py", "action": "create"}],
        test_files=["test_login.py"],
    )

    row = ws.get_issue(issue_id)
    assert row is not None
    assert row["title"] == "Implement login"
    assert row["status"] == "open"
    assert row["target_files_json"] is not None
    assert row["test_files_json"] is not None


def test_get_open_issues(ws):
    pid = ws.save_problem("Problem")
    id1 = ws.save_issue(pid, "Task 1", "Desc 1", [{"filepath": "a.py", "action": "create"}], ["test_a.py"])
    id2 = ws.save_issue(pid, "Task 2", "Desc 2", [{"filepath": "b.py", "action": "create"}], ["test_b.py"])

    open_issues = ws.get_open_issues(problem_id=pid)
    assert len(open_issues) == 2

    ws.close_issue(id1)
    open_issues = ws.get_open_issues(problem_id=pid)
    assert len(open_issues) == 1
    assert open_issues[0]["id"] == id2


def test_update_issue_status(ws):
    pid = ws.save_problem("Problem")
    iid = ws.save_issue(pid, "Task", "Desc", [{"filepath": "c.py", "action": "create"}], ["test_c.py"])

    ws.update_issue_status(iid, "in_progress")
    row = ws.get_issue(iid)
    assert row["status"] == "in_progress"


def test_close_issue_sets_closed_at(ws):
    pid = ws.save_problem("Problem")
    iid = ws.save_issue(pid, "Task", "Desc", [{"filepath": "d.py", "action": "create"}], ["test_d.py"])

    ws.close_issue(iid)
    row = ws.get_issue(iid)
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


def test_get_open_issues_all_problems(ws):
    pid1 = ws.save_problem("P1")
    pid2 = ws.save_problem("P2")
    ws.save_issue(pid1, "T1", "D1", [{"filepath": "e.py", "action": "create"}], ["test_e.py"])
    ws.save_issue(pid2, "T2", "D2", [{"filepath": "f.py", "action": "create"}], ["test_f.py"])

    all_open = ws.get_open_issues()
    assert len(all_open) == 2


# ── Context queries ──────────────────────────────────────────────────────────────

def test_get_context_for_code_file(ws):
    pid = ws.save_problem("Build an expense tracker")
    ws.save_issue(
        problem_id=pid,
        title="Implement storage layer",
        description="Create the SQLite storage backend.",
        target_files=[{"filepath": "tracker/storage.py", "action": "create"}],
        test_files=["tests/test_storage.py"],
    )

    ctx = ws.get_context_for_code_file("tracker/storage.py")
    assert ctx is not None
    assert ctx["problem_statement"] == "Build an expense tracker"
    assert ctx["issue_title"] == "Implement storage layer"
    assert ctx["issue_description"] == "Create the SQLite storage backend."


def test_get_context_for_code_file_returns_none(ws):
    assert ws.get_context_for_code_file("nonexistent.py") is None


# ── Scope isolation ──────────────────────────────────────────────────────────────

def test_different_workspaces_isolated(db):
    ws1 = db.for_workspace("proj-a", "svc-1")
    ws2 = db.for_workspace("proj-a", "svc-2")

    ws1.save_problem("Problem in svc-1")
    ws2.save_problem("Problem in svc-2")

    # Each scope sees only its own problems
    assert ws1.get_problem(1) is not None
    assert ws1.get_problem(2) is None
    assert ws2.get_problem(1) is None
    assert ws2.get_problem(2) is not None


def test_different_projects_isolated(db):
    ws1 = db.for_workspace("proj-a", "svc")
    ws2 = db.for_workspace("proj-b", "svc")

    ws1.save_problem("Problem in proj-a")
    ws2.save_problem("Problem in proj-b")

    assert ws1.get_problem(1) is not None
    assert ws1.get_problem(2) is None


# ── Context manager ─────────────────────────────────────────────────────────────

def test_context_manager(db):
    with db.for_workspace("proj", "svc") as ws:
        pid = ws.save_problem("Context test")

    # Data persists (connection is still open via BiznizDB)
    ws2 = db.for_workspace("proj", "svc")
    row = ws2.get_problem(pid)
    assert row["statement"] == "Context test"
