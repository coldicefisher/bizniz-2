"""Tests for TypeScriptAstDocumenter (sidecar-based).

These tests dispatch the actual ``bizniz-doc-typescript:latest``
sidecar — they're marked as ``functional`` so they're excluded from
the default ``pytest`` run (same convention as our other sidecar-
based tests). Run with ``pytest -m functional``.

If you change the extract.js script, run these to confirm the
sidecar still extracts the contract we expect.
"""
import pytest
from pathlib import Path

from bizniz.documenters.typescript_ast import TypeScriptAstDocumenter


pytestmark = pytest.mark.functional


def _write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_extracts_zustand_store_members(tmp_path):
    """The bug we're solving: a coder writing LoginPage assumes the
    authStore exposes a `login()` method that doesn't exist. The
    documenter must surface what the store actually exposes."""
    _write(tmp_path, "package.json", '{"name": "test", "type": "module"}')
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"jsx": "react-jsx"}}')
    _write(tmp_path, "src/stores/authStore.ts", '''
import { create } from "zustand";

interface AuthState {
  token: string | null;
  isAuthenticated: boolean;
  setSession: (token: string) => void;
  hydrate: () => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  isAuthenticated: false,
  setSession: (token) => set({ token, isAuthenticated: true }),
  hydrate: () => {},
  logout: () => set({ token: null, isAuthenticated: false }),
}));
''')

    doc = TypeScriptAstDocumenter(workspace_root=tmp_path, service_name="frontend").extract()
    auth = doc["files"]["src/stores/authStore.ts"]

    assert "zustand" in doc["framework_hints"]
    stores = auth["stores"]
    assert len(stores) == 1
    store = stores[0]
    assert store["name"] == "useAuthStore"
    assert store["type_arg"] == "AuthState"
    members = set(store["members"])
    assert {"token", "isAuthenticated", "setSession", "hydrate", "logout"} <= members
    # And critically, the made-up method is NOT in the contract:
    assert "login" not in members


def test_extracts_exported_functions_and_types(tmp_path):
    _write(tmp_path, "src/api/auth.ts", '''
import { api } from "./client";

export interface LoginPayload {
  email: string;
  password: string;
}

export type Token = {
  access_token: string;
  refresh_token: string;
};

export async function login(payload: LoginPayload): Promise<Token> {
  return api.post("/auth/login", payload);
}

export const STORAGE_KEY: string = "session";
''')

    doc = TypeScriptAstDocumenter(workspace_root=tmp_path, service_name="frontend").extract()
    f = doc["files"]["src/api/auth.ts"]

    exports_by_name = {e["name"]: e for e in f["exports"]}
    assert "login" in exports_by_name
    assert exports_by_name["login"]["kind"] == "function"
    assert exports_by_name["login"]["async"] is True
    login_params = {p["name"]: p["type"] for p in exports_by_name["login"]["params"]}
    assert login_params == {"payload": "LoginPayload"}
    assert exports_by_name["login"]["return_type"] == "Promise<Token>"

    interfaces = {i["name"]: i for i in f["interfaces"]}
    assert "LoginPayload" in interfaces
    members = {m["name"]: m["type"] for m in interfaces["LoginPayload"]["members"]}
    assert members == {"email": "string", "password": "string"}

    type_aliases = {t["name"]: t for t in f["types"]}
    assert "Token" in type_aliases

    consts = {e["name"] for e in f["exports"] if e["kind"] == "const"}
    assert "STORAGE_KEY" in consts


def test_skips_test_files_and_node_modules(tmp_path):
    _write(tmp_path, "src/foo.ts", "export const x = 1;")
    _write(tmp_path, "src/foo.test.ts", "export const ignore = true;")
    _write(tmp_path, "src/foo.spec.tsx", "export const alsoIgnore = true;")
    _write(tmp_path, "node_modules/lib/index.ts", "export const noTouch = true;")

    doc = TypeScriptAstDocumenter(workspace_root=tmp_path, service_name="frontend").extract()
    files = set(doc["files"].keys())

    assert "src/foo.ts" in files
    assert "src/foo.test.ts" not in files
    assert "src/foo.spec.tsx" not in files
    assert not any("node_modules" in f for f in files)


def test_detects_react_imports(tmp_path):
    _write(tmp_path, "src/App.tsx", '''
import React from "react";
import { useNavigate } from "react-router-dom";

export default function App() {
  return null as any;
}
''')

    doc = TypeScriptAstDocumenter(workspace_root=tmp_path, service_name="frontend").extract()
    assert "react" in doc["framework_hints"]
    assert "react-router" in doc["framework_hints"]
