import pytest
from pathlib import Path

from bizniz.project.project import Project


@pytest.fixture
def project(tmp_path):
    return Project(root=tmp_path / "myproject", project_name="My Test Project")


# ── Basic properties ────────────────────────────────────────────────────────────

def test_root_is_resolved_path(tmp_path):
    proj = Project(root=tmp_path / "proj", project_name="Proj")
    assert proj.root == (tmp_path / "proj").resolve()


def test_root_directory_created(tmp_path):
    root = tmp_path / "proj"
    assert not root.exists()
    Project(root=root, project_name="Proj")
    assert root.exists()


def test_project_name(project):
    assert project.project_name == "My Test Project"


def test_dev_root(project):
    expected = project.root / "dockerfiles" / "development"
    assert project.dev_root == expected


# ── create_structure ─────────────────────────────────────────────────────────────

def test_create_structure(project):
    project.create_structure()
    assert project.dev_root.exists()
    assert project.dev_root.is_dir()


# ── get_service_workspace ────────────────────────────────────────────────────────

def test_get_service_workspace(project):
    project.create_structure()
    ws = project.get_service_workspace("backend")
    expected_path = project.dev_root / "backend"
    assert ws.root == expected_path.resolve()
    assert expected_path.exists()


def test_get_service_workspace_creates_directory(project):
    project.create_structure()
    ws_path = project.dev_root / "frontend"
    assert not ws_path.exists()
    ws = project.get_service_workspace("frontend")
    assert ws_path.exists()


# ── write_docker_compose ─────────────────────────────────────────────────────────

def test_write_docker_compose(project):
    content = "version: '3'\nservices:\n  backend:\n    image: myapp\n"
    project.write_docker_compose(content)
    compose_path = project.dev_root / "docker-compose.yml"
    assert compose_path.exists()
    assert compose_path.read_text() == content


def test_write_docker_compose_creates_dev_root(project):
    assert not project.dev_root.exists()
    project.write_docker_compose("version: '3'")
    assert project.dev_root.exists()


# ── write_env_file ───────────────────────────────────────────────────────────────

def test_write_env_file(project):
    content = "DB_HOST=localhost\nDB_PORT=5432\n"
    project.write_env_file(content)
    env_path = project.dev_root / ".env"
    assert env_path.exists()
    assert env_path.read_text() == content


def test_write_env_file_creates_dev_root(project):
    assert not project.dev_root.exists()
    project.write_env_file("KEY=val")
    assert project.dev_root.exists()


# ── from_name ────────────────────────────────────────────────────────────────────

def test_from_name(tmp_path):
    proj = Project.from_name("Fraydit Solutions", parent=tmp_path)
    assert proj.project_name == "Fraydit Solutions"
    assert proj.root == (tmp_path / "fraydit_solutions").resolve()


def test_from_name_slugifies(tmp_path):
    proj = Project.from_name("My Cool Project!", parent=tmp_path)
    assert proj.root.name == "my_cool_project"


# ── db property ──────────────────────────────────────────────────────────────────

def test_db_lazy_creation(project):
    from bizniz.project.project_db import ProjectDB
    db = project.db
    assert isinstance(db, ProjectDB)
    db_path = project.root / ".bizniz" / "project.db"
    assert db_path.exists()
    db.close()


def test_db_returns_same_instance(project):
    db1 = project.db
    db2 = project.db
    assert db1 is db2
    db1.close()


# ── delegation to db ────────────────────────────────────────────────────────────

def test_get_service_status(project):
    project.db.save_service("api", "backend", "flask", "python", "/tmp/api")
    services = project.get_service_status()
    assert len(services) == 1
    assert services[0]["name"] == "api"
    project.db.close()


def test_get_issue_history(project):
    project.db.log_issue("api", "Fix bug", "Something broken")
    issues = project.get_issue_history()
    assert len(issues) == 1
    assert issues[0]["issue_title"] == "Fix bug"
    project.db.close()


def test_get_architecture_changes(project):
    project.db.save_architecture_snapshot('{"services": []}', "Initial")
    changes = project.get_architecture_changes()
    assert len(changes) == 1
    assert changes[0]["description"] == "Initial"
    project.db.close()
