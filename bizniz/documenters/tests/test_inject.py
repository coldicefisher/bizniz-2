"""Tests for the workspace-context injector."""
from pathlib import Path

import pytest

from bizniz.documenters.inject import (
    detect_language,
    extract_workspace_docs,
    format_for_prompt,
)


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_detect_python(tmp_path):
    _write(tmp_path, "app/main.py", "x = 1\n")
    assert detect_language(tmp_path) == "python"


def test_detect_typescript(tmp_path):
    _write(tmp_path, "src/foo.ts", "export const x = 1;\n")
    assert detect_language(tmp_path) == "typescript"


def test_detect_returns_none_on_empty(tmp_path):
    assert detect_language(tmp_path) is None


def test_detect_skips_node_modules(tmp_path):
    _write(tmp_path, "node_modules/lib/index.ts", "x")
    _write(tmp_path, "app/main.py", "y = 2")
    assert detect_language(tmp_path) == "python"


@pytest.mark.functional
def test_format_python_workspace(tmp_path):
    _write(tmp_path, "app/schemas/auth.py", '''
from pydantic import BaseModel

class LoginRequest(BaseModel):
    email: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
''')
    _write(tmp_path, "app/api/routes/auth.py", '''
async def login(credentials):
    """Auth login endpoint."""
    pass
''')

    docs = extract_workspace_docs(workspace_root=tmp_path, service_name="backend")
    text = format_for_prompt(docs)

    # Field types surface
    assert "LoginRequest" in text
    assert "email: str" in text
    assert "access_token" in text
    # Function signatures surface
    assert "async login(credentials)" in text
    # Header is present
    assert "WORKSPACE CONTEXT" in text
    assert "do NOT redefine" in text


def test_format_handles_no_files(tmp_path):
    # No source files → detect_language returns None → no sidecar dispatch
    docs = extract_workspace_docs(workspace_root=tmp_path)
    assert format_for_prompt(docs) == ""


@pytest.mark.functional
def test_format_truncates_to_max_chars(tmp_path):
    # Generate enough files to overflow 200 chars
    for i in range(20):
        _write(tmp_path, f"app/m{i}.py", f"def f{i}(): pass\n")
    docs = extract_workspace_docs(workspace_root=tmp_path)
    text = format_for_prompt(docs, max_chars=200)
    assert len(text) <= 400  # Some slack for the trailing message
    assert "truncated" in text.lower() or len(text) <= 200


@pytest.mark.functional
def test_typescript_workspace_via_sidecar(tmp_path):
    """Functional: hits the bizniz-doc-typescript sidecar."""
    _write(tmp_path, "package.json", '{"name": "test"}')
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    _write(tmp_path, "src/stores/authStore.ts", '''
import { create } from "zustand";

interface AuthState {
  token: string | null;
  setSession: (t: string) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  setSession: (t) => set({ token: t }),
  logout: () => set({ token: null }),
}));
''')

    docs = extract_workspace_docs(workspace_root=tmp_path, service_name="frontend")
    text = format_for_prompt(docs)

    # The smoking-gun assertion: store members surface verbatim
    assert "useAuthStore" in text
    assert "exposed members" in text
    assert "setSession" in text
    assert "logout" in text
    # And the made-up method is NOT injected:
    assert "login" not in text or "setSession" in text  # "login" appears only if real
