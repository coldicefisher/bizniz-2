"""Contract tests for the docs-serving routes.

The docs surface is **always present** in this skeleton. These tests
pin the contract:

- Auth-gated: index, article, search all return 401 without a valid
  Bearer token.
- Path-traversal safe: `..`, leading `/`, and unsafe chars on the
  slug all return 404 (not 500, not the file).
- Round-trip: an article written to disk shows up in the manifest
  and reads back identically.
- Search returns hits with non-empty excerpts.

Auth in this skeleton is FusionAuth-issued JWT validated by
``get_current_user``. We override that dependency to return a mock
user — same pattern as ``test_auth.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.auth import get_current_user
from app.main import app
from app.models.user import User
from app.services.docs_loader import (
    DocsLoader,
    get_docs_loader,
    reset_docs_loader_for_testing,
)


# ── Helpers ──────────────────────────────────────────────────────


def _write_docs_tree(root: Path) -> None:
    """Build a small docs tree on disk for the loader to scan."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "# Project Overview\n\nThis is the README content.\n",
        encoding="utf-8",
    )
    (root / "architecture.md").write_text(
        "# Architecture\n\nThe system uses FastAPI and Postgres.\n",
        encoding="utf-8",
    )
    (root / "api").mkdir(exist_ok=True)
    (root / "api" / "backend.md").write_text(
        "# Backend API\n\nAll routes are under /api/v1.\n",
        encoding="utf-8",
    )
    (root / "services").mkdir(exist_ok=True)
    (root / "services" / "backend.md").write_text(
        "# Backend Service\n\nThe backend handles all requests.\n",
        encoding="utf-8",
    )


def _install_overrides(tmp_docs: Path, test_user: User) -> None:
    """Override the auth + loader dependencies for one test."""
    reset_docs_loader_for_testing()
    loader = DocsLoader(tmp_docs, auto_reload=False)
    app.dependency_overrides[get_docs_loader] = lambda: loader
    app.dependency_overrides[get_current_user] = lambda: test_user


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_docs_loader, None)
    app.dependency_overrides.pop(get_current_user, None)
    reset_docs_loader_for_testing()


# ── Auth gate ────────────────────────────────────────────────────


class TestAuthGating:
    async def test_index_without_token_is_401(self, client):
        # No dependency override → real get_current_user runs → 401.
        resp = await client.get("/api/v1/docs/index")
        assert resp.status_code == 401

    async def test_article_without_token_is_401(self, client):
        resp = await client.get("/api/v1/docs/article/README")
        assert resp.status_code == 401

    async def test_search_without_token_is_401(self, client):
        resp = await client.get("/api/v1/docs/search?q=hello")
        assert resp.status_code == 401


# ── Index ────────────────────────────────────────────────────────


class TestIndex:
    async def test_index_returns_tree_with_auth(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get("/api/v1/docs/index")
            assert resp.status_code == 200
            body = resp.json()
            assert body["version"] == 1
            slugs = {n["slug"] for n in body["tree"]}
            # Sections appear as their own root nodes; root-level
            # articles also appear in the tree.
            assert "api" in slugs
            assert "services" in slugs
            assert "README" in slugs
        finally:
            _clear_overrides()

    async def test_index_empty_when_docs_dir_missing(
        self, client, tmp_path, test_user,
    ):
        _install_overrides(tmp_path / "does-not-exist", test_user)
        try:
            resp = await client.get("/api/v1/docs/index")
            assert resp.status_code == 200
            assert resp.json()["tree"] == []
        finally:
            _clear_overrides()

    async def test_index_respects_manifest_when_present(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        manifest = {
            "version": 1,
            "generated_at": "2026-05-17T12:00:00Z",
            "tree": [
                {"slug": "README", "title": "Custom Title",
                 "path": "README.md"},
                {"slug": "architecture", "title": "Architecture",
                 "path": "architecture.md"},
            ],
        }
        (tmp_path / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get("/api/v1/docs/index")
            assert resp.status_code == 200
            body = resp.json()
            slugs = [n["slug"] for n in body["tree"]]
            assert slugs == ["README", "architecture"]
            assert body["tree"][0]["title"] == "Custom Title"
        finally:
            _clear_overrides()


# ── Article ──────────────────────────────────────────────────────


class TestArticle:
    async def test_round_trips_markdown_body(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get("/api/v1/docs/article/README")
            assert resp.status_code == 200
            body = resp.json()
            assert body["slug"] == "README"
            assert body["title"] == "Project Overview"
            assert "This is the README content" in body["markdown"]
        finally:
            _clear_overrides()

    async def test_nested_path_works(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get("/api/v1/docs/article/api/backend")
            assert resp.status_code == 200
            body = resp.json()
            assert body["slug"] == "api/backend"
            assert body["section"] == "api"
        finally:
            _clear_overrides()

    async def test_missing_slug_is_404(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get("/api/v1/docs/article/does-not-exist")
            assert resp.status_code == 404
        finally:
            _clear_overrides()


# ── Path traversal ───────────────────────────────────────────────


class TestPathTraversal:
    @pytest.mark.parametrize("bad_slug", [
        "../etc/passwd",
        "../../etc/passwd",
        "/etc/passwd",
        "api/../../etc/passwd",
        ".hidden",
        "api/../../README",
    ])
    async def test_traversal_attempts_return_404(
        self, client, tmp_path, test_user, bad_slug,
    ):
        # Make sure a real article exists so a non-404 is unambiguously
        # a traversal success.
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get(
                f"/api/v1/docs/article/{bad_slug}",
            )
            assert resp.status_code == 404, (
                f"Path traversal escaped: {bad_slug!r} returned "
                f"{resp.status_code}"
            )
        finally:
            _clear_overrides()


# ── Search ───────────────────────────────────────────────────────


class TestSearch:
    async def test_finds_term_in_body(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get(
                "/api/v1/docs/search?q=FastAPI",
            )
            assert resp.status_code == 200
            hits = resp.json()
            assert len(hits) >= 1
            assert any(h["slug"] == "architecture" for h in hits)
            assert all(h["excerpt"] for h in hits)
        finally:
            _clear_overrides()

    async def test_too_short_query_rejected(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get("/api/v1/docs/search?q=a")
            # FastAPI 422 from the min_length=2 validator.
            assert resp.status_code == 422
        finally:
            _clear_overrides()

    async def test_no_matches_returns_empty(
        self, client, tmp_path, test_user,
    ):
        _write_docs_tree(tmp_path)
        _install_overrides(tmp_path, test_user)
        try:
            resp = await client.get(
                "/api/v1/docs/search?q=zzzzznotinanydoc",
            )
            assert resp.status_code == 200
            assert resp.json() == []
        finally:
            _clear_overrides()
