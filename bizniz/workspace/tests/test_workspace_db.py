import pytest
from pathlib import Path

from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace.workspace_db import WorkspaceDB


@pytest.fixture
def workspace(tmp_path):
    return BaseWorkspace(root=tmp_path)


@pytest.fixture
def db(workspace):
    with WorkspaceDB(workspace) as d:
        yield d


# ── Problems ────────────────────────────────────────────────────────────────────

def test_save_and_get_problem(db):
    problem_id = db.save_problem("Build a todo app.")
    row = db.get_problem(problem_id)
    assert row is not None
    assert row["statement"] == "Build a todo app."
    assert row["id"] == problem_id


def test_get_problem_returns_none_for_missing(db):
    assert db.get_problem(9999) is None


def test_db_file_created(workspace):
    db = WorkspaceDB(workspace)
    db.close()
    assert (workspace.root / ".bizniz" / "bizniz.db").exists()


# ── Requirements ────────────────────────────────────────────────────────────────

def test_save_and_get_requirements(db):
    pid = db.save_problem("Example problem")
    db.save_requirement(pid, "business", "Users must be able to log in.")
    db.save_requirement(pid, "functional", "The system must store sessions.")
    db.save_requirement(pid, "nonfunctional", "Response time < 200 ms.")

    rows = db.get_requirements(pid)
    assert len(rows) == 3


def test_get_requirements_filtered_by_type(db):
    pid = db.save_problem("Example")
    db.save_requirement(pid, "business", "Req 1")
    db.save_requirement(pid, "functional", "Req 2")

    biz = db.get_requirements(pid, req_type="business")
    assert len(biz) == 1
    assert biz[0]["type"] == "business"


def test_requirement_invalid_type_raises(db):
    pid = db.save_problem("Example")
    with pytest.raises(Exception):
        db.save_requirement(pid, "invalid_type", "Some text")


# ── Use Cases ───────────────────────────────────────────────────────────────────

def test_save_and_get_use_cases(db):
    pid = db.save_problem("Shopping cart system")
    db.save_use_case(pid, "Add item to cart", "A user selects an item and adds it.")
    db.save_use_case(pid, "Remove item", "A user removes an item from the cart.")

    rows = db.get_use_cases(pid)
    assert len(rows) == 2
    titles = [r["title"] for r in rows]
    assert "Add item to cart" in titles


# ── Issues ──────────────────────────────────────────────────────────────────────

def test_save_and_get_issue(db):
    pid = db.save_problem("Some problem")
    issue_id = db.save_issue(
        problem_id=pid,
        title="Implement login",
        description="Create a login endpoint.",
        code_file="login.py",
        test_file="test_login.py",
    )

    row = db.get_issue(issue_id)
    assert row is not None
    assert row["title"] == "Implement login"
    assert row["status"] == "open"
    assert row["code_file"] == "login.py"
    assert row["test_file"] == "test_login.py"


def test_get_open_issues(db):
    pid = db.save_problem("Problem")
    id1 = db.save_issue(pid, "Task 1", "Desc 1", "a.py", "test_a.py")
    id2 = db.save_issue(pid, "Task 2", "Desc 2", "b.py", "test_b.py")

    open_issues = db.get_open_issues(problem_id=pid)
    assert len(open_issues) == 2

    db.close_issue(id1)
    open_issues = db.get_open_issues(problem_id=pid)
    assert len(open_issues) == 1
    assert open_issues[0]["id"] == id2


def test_update_issue_status(db):
    pid = db.save_problem("Problem")
    iid = db.save_issue(pid, "Task", "Desc", "c.py", "test_c.py")

    db.update_issue_status(iid, "in_progress")
    row = db.get_issue(iid)
    assert row["status"] == "in_progress"


def test_close_issue_sets_closed_at(db):
    pid = db.save_problem("Problem")
    iid = db.save_issue(pid, "Task", "Desc", "d.py", "test_d.py")

    db.close_issue(iid)
    row = db.get_issue(iid)
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


def test_get_open_issues_all_problems(db):
    pid1 = db.save_problem("P1")
    pid2 = db.save_problem("P2")
    db.save_issue(pid1, "T1", "D1", "e.py", "test_e.py")
    db.save_issue(pid2, "T2", "D2", "f.py", "test_f.py")

    all_open = db.get_open_issues()
    assert len(all_open) == 2


# ── Context manager ─────────────────────────────────────────────────────────────

def test_context_manager(workspace):
    with WorkspaceDB(workspace) as db:
        pid = db.save_problem("Context test")
    # After __exit__, connection is closed; re-open to verify persistence
    with WorkspaceDB(workspace) as db2:
        row = db2.get_problem(pid)
        assert row["statement"] == "Context test"
