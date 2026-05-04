"""Tests for the route-duplication reviewer.

The smoking-gun fixture mirrors the M1 v4 path-doubling bug: a
route file declares ``APIRouter(prefix='/auth')`` AND main.py
calls ``app.include_router(auth.router, prefix='/auth')``. The
reviewer must catch this.
"""
from pathlib import Path

from bizniz.reviewers import review_routes


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_clean_workspace_passes(tmp_path):
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login")
async def login(): ...

@router.post("/register")
async def register(): ...
''')
    _write(tmp_path, "app/main.py", '''
import importlib, pkgutil
from fastapi import FastAPI
import app.api.routes as _routes_pkg

app = FastAPI()

for mod_info in pkgutil.iter_modules(_routes_pkg.__path__):
    module = importlib.import_module(f"{_routes_pkg.__name__}.{mod_info.name}")
    if hasattr(module, "router"):
        app.include_router(module.router, prefix="/api/v1")
''')

    result = review_routes(tmp_path)
    assert result.ok, f"clean workspace flagged: {result.message()}"
    assert result.routes_seen == 2


def test_doubled_prefix_caught(tmp_path):
    """The smoking-gun M1 v4 bug: router prefix='/auth' AND
    include_router(prefix='/auth') → /auth/auth/login."""
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login")
async def login(): ...
''')
    _write(tmp_path, "app/main.py", '''
from fastapi import FastAPI
from app.api.routes import auth

app = FastAPI()
app.include_router(auth.router, prefix="/auth", tags=["auth"])
''')

    result = review_routes(tmp_path)
    assert not result.ok
    issues = result.issues
    assert any(i.kind == "doubled_prefix" for i in issues)
    msg = result.message()
    assert "doubled_prefix" in msg
    assert "/auth" in msg


def test_manual_and_auto_caught(tmp_path):
    """Auto-discovery loop AND manual include_router for the same
    file → router registered twice."""
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login")
async def login(): ...
''')
    _write(tmp_path, "app/main.py", '''
import importlib, pkgutil
from fastapi import FastAPI
from app.api.routes import auth
import app.api.routes as _routes_pkg

app = FastAPI()

# Auto-discovery
for mod_info in pkgutil.iter_modules(_routes_pkg.__path__):
    module = importlib.import_module(f"{_routes_pkg.__name__}.{mod_info.name}")
    if hasattr(module, "router"):
        app.include_router(module.router, prefix="/api/v1")

# AND a manual include of the same router (DOUBLE registration)
app.include_router(auth.router, prefix="/auth")
''')

    result = review_routes(tmp_path)
    assert not result.ok
    kinds = [i.kind for i in result.issues]
    assert "manual_and_auto" in kinds


def test_duplicate_path_caught(tmp_path):
    """Two route files both declare /auth/login at the same level."""
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/auth")

@router.post("/login")
async def login_handler(): ...
''')
    _write(tmp_path, "app/api/routes/legacy_auth.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/auth")

@router.post("/login")
async def legacy_login(): ...
''')

    result = review_routes(tmp_path)
    assert not result.ok
    assert any(i.kind == "duplicate_path" for i in result.issues)


def test_no_routes_dir_returns_clean(tmp_path):
    """Non-FastAPI services have no routes dir → reviewer is a no-op."""
    _write(tmp_path, "app/main.py", "x = 1\n")
    result = review_routes(tmp_path)
    assert result.ok
    assert result.routes_seen == 0


def test_message_lists_specific_locations(tmp_path):
    _write(tmp_path, "app/api/routes/auth.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/auth")

@router.post("/login")
async def login(): ...
''')
    _write(tmp_path, "app/main.py", '''
from fastapi import FastAPI
from app.api.routes import auth
app = FastAPI()
app.include_router(auth.router, prefix="/auth")
''')

    result = review_routes(tmp_path)
    msg = result.message()
    assert "main.py:" in msg  # location reference
    assert "auth.py" in msg
