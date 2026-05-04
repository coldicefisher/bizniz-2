"""Tests for workspace_reader.format_existing_workspace_state."""
import json
from pathlib import Path

from bizniz.architect.workspace_reader import format_existing_workspace_state


def test_returns_empty_for_missing_docs(tmp_path):
    assert format_existing_workspace_state(tmp_path) == ""


def test_returns_empty_for_empty_docs(tmp_path):
    (tmp_path / "docs").mkdir()
    assert format_existing_workspace_state(tmp_path) == ""


def _write_doc(project_root: Path, service: str, doc: dict):
    code_dir = project_root / "docs" / service / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "api.json").write_text(json.dumps(doc))


def test_python_backend_summary(tmp_path):
    _write_doc(tmp_path, "backend", {
        "service": "backend",
        "language": "python",
        "files": {
            "app/api/routes/auth.py": {
                "imports": [],
                "classes": [],
                "functions": [
                    {"name": "login", "params": [], "return_type": None},
                    {"name": "register", "params": [], "return_type": None},
                ],
            },
            "app/schemas/auth.py": {
                "imports": [],
                "classes": [
                    {"name": "LoginRequest", "fields": [], "methods": [], "bases": []},
                    {"name": "LoginResponse", "fields": [], "methods": [], "bases": []},
                ],
                "functions": [],
            },
        },
    })

    text = format_existing_workspace_state(tmp_path)
    assert "EXISTING WORKSPACE STATE" in text
    assert "backend (python" in text
    assert "routes" in text
    assert "schemas" in text
    assert "login" in text
    assert "LoginRequest" in text


def test_typescript_frontend_with_zustand_store(tmp_path):
    """The smoking-gun: store members should surface verbatim so
    the architect knows what the store exposes."""
    _write_doc(tmp_path, "frontend", {
        "service": "frontend",
        "language": "typescript",
        "files": {
            "src/stores/authStore.ts": {
                "imports": [], "exports": [],
                "interfaces": [], "types": [],
                "stores": [{
                    "name": "useAuthStore",
                    "type_arg": "AuthState",
                    "members": ["token", "user", "setSession", "logout"],
                }],
            },
            "src/api/auth.ts": {
                "imports": [], "interfaces": [], "types": [], "stores": [],
                "exports": [
                    {"kind": "function", "name": "login"},
                    {"kind": "function", "name": "register"},
                ],
            },
        },
    })

    text = format_existing_workspace_state(tmp_path)
    assert "frontend (typescript" in text
    assert "useAuthStore" in text
    # Store members surface so the architect plans extensions
    # against them rather than re-imagining the store
    assert "setSession" in text
    assert "logout" in text


def test_handles_missing_api_json_gracefully(tmp_path):
    # Service folder exists but no api.json — should skip cleanly
    (tmp_path / "docs" / "ghost").mkdir(parents=True)
    assert format_existing_workspace_state(tmp_path) == ""


def test_handles_corrupt_json(tmp_path):
    code_dir = tmp_path / "docs" / "broken" / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "api.json").write_text("not json at all")
    # Should not raise — just skip the broken service
    text = format_existing_workspace_state(tmp_path)
    assert text == ""


def test_truncates_at_max_chars(tmp_path):
    # Create many services to exceed the budget
    for i in range(20):
        _write_doc(tmp_path, f"svc{i}", {
            "service": f"svc{i}",
            "language": "python",
            "files": {
                f"app/m{i}.py": {
                    "imports": [], "classes": [],
                    "functions": [{"name": f"f{i}", "params": [], "return_type": None}],
                },
            },
        })
    text = format_existing_workspace_state(tmp_path, max_chars=500)
    assert "truncated" in text
