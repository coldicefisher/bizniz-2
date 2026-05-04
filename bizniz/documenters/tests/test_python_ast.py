"""Tests for PythonAstDocumenter.

Goal: prove that on a representative FastAPI service workspace, the
documenter captures the things a downstream coder needs to know to
write correct code without guessing. Specifically:

- Pydantic models with field types.
- Functions with param types, return types, decorators.
- Imports.
- Skips test files and __pycache__.
- Detects framework hints (fastapi, pydantic).
"""
import json
from pathlib import Path

from bizniz.documenters.python_ast import PythonAstDocumenter


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_extracts_pydantic_models(tmp_path):
    _write(tmp_path, "app/schemas/auth.py", '''
from pydantic import BaseModel, EmailStr, Field

class LoginRequest(BaseModel):
    """Payload for POST /auth/login."""
    email: EmailStr
    password: str = Field(min_length=8)

class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str
''')

    doc = PythonAstDocumenter(workspace_root=tmp_path, service_name="backend").extract()

    file_doc = doc["files"]["app/schemas/auth.py"]
    classes = {c["name"]: c for c in file_doc["classes"]}

    assert "LoginRequest" in classes
    lr = classes["LoginRequest"]
    assert "BaseModel" in lr["bases"]
    assert lr["docstring"] == "Payload for POST /auth/login."

    fields = {f["name"]: f for f in lr["fields"]}
    assert fields["email"]["type"] == "EmailStr"
    assert fields["password"]["type"] == "str"
    assert fields["password"]["default"] == "Field(min_length=8)"

    assert "pydantic" in doc["framework_hints"]


def test_extracts_fastapi_routes(tmp_path):
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter, Depends
from app.schemas.auth import LoginRequest, LoginResponse
from app.core.auth import get_current_user

router = APIRouter()

@router.post("/login", response_model=LoginResponse)
async def login(credentials: LoginRequest) -> LoginResponse:
    """Authenticate and return tokens."""
    ...

@router.get("/me")
async def me(user = Depends(get_current_user)):
    return user
''')

    doc = PythonAstDocumenter(workspace_root=tmp_path, service_name="backend").extract()
    file_doc = doc["files"]["app/api/routes/auth.py"]

    funcs = {f["name"]: f for f in file_doc["functions"]}
    assert "login" in funcs
    login = funcs["login"]
    assert login["is_async"] is True
    assert login["return_type"] == "LoginResponse"
    assert login["docstring"] == "Authenticate and return tokens."
    assert any("router.post" in d for d in login["decorators"])

    params = {p["name"]: p for p in login["params"]}
    assert params["credentials"]["type"] == "LoginRequest"

    assert "fastapi" in doc["framework_hints"]


def test_skips_tests_and_caches(tmp_path):
    _write(tmp_path, "app/main.py", "x = 1\n")
    _write(tmp_path, "app/__pycache__/main.cpython-312.pyc", "")
    _write(tmp_path, "tests/test_main.py", "def test_x(): pass\n")
    _write(tmp_path, "app/services/test_helper.py", "y = 2\n")

    doc = PythonAstDocumenter(workspace_root=tmp_path, service_name="backend").extract()
    files = set(doc["files"].keys())

    assert "app/main.py" in files
    assert "tests/test_main.py" not in files
    assert "app/services/test_helper.py" not in files
    assert not any(".pyc" in f for f in files)


def test_extracts_imports(tmp_path):
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter, Depends
from app.schemas.auth import LoginRequest
import uuid
''')

    doc = PythonAstDocumenter(workspace_root=tmp_path, service_name="backend").extract()
    imports = doc["files"]["app/api/routes/auth.py"]["imports"]

    fastapi_imp = next((i for i in imports if i["module"] == "fastapi"), None)
    assert fastapi_imp is not None
    assert "APIRouter" in fastapi_imp["names"]
    assert "Depends" in fastapi_imp["names"]

    schemas_imp = next((i for i in imports if i["module"] == "app.schemas.auth"), None)
    assert "LoginRequest" in schemas_imp["names"]


def test_handles_syntax_error_gracefully(tmp_path):
    _write(tmp_path, "app/good.py", "x = 1\n")
    _write(tmp_path, "app/broken.py", "this is :: not valid python")

    doc = PythonAstDocumenter(workspace_root=tmp_path, service_name="backend").extract()
    assert "_parse_error" in doc["files"]["app/broken.py"]
    # Good file still extracted normally.
    assert "_parse_error" not in doc["files"]["app/good.py"]


def test_function_with_keyword_only_and_defaults(tmp_path):
    _write(tmp_path, "app/util.py", '''
def fetch(url: str, *, timeout: float = 10.0, retries: int = 3) -> dict:
    return {}
''')

    doc = PythonAstDocumenter(workspace_root=tmp_path, service_name="backend").extract()
    func = doc["files"]["app/util.py"]["functions"][0]
    params = {p["name"]: p for p in func["params"]}

    assert params["url"]["type"] == "str"
    assert params["url"]["default"] is None
    assert params["timeout"]["default"] == "10.0"
    assert params["timeout"].get("kw_only") is True
    assert params["retries"]["default"] == "3"


def test_write_produces_api_json(tmp_path):
    _write(tmp_path, "app/main.py", '''
def hello() -> str:
    return "world"
''')

    out_dir = tmp_path / "_docs_out"
    out_path = PythonAstDocumenter(
        workspace_root=tmp_path, service_name="backend",
    ).write(out_dir)

    assert out_path == out_dir / "api.json"
    assert out_path.exists()
    parsed = json.loads(out_path.read_text())
    assert parsed["service"] == "backend"
    assert parsed["language"] == "python"
    assert "app/main.py" in parsed["files"]
