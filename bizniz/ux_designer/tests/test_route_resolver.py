"""Tests for the dynamic-route resolver.

Cover the deterministic surfaces (cache I/O, staleness probe, JSON
parser, the prompt builders) and the dispatcher's cache-vs-agent
routing. The Claude CLI invocation itself is stubbed via the
``agent_fn`` seam — the subprocess path is covered by functional
runs, not unit tests.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from bizniz.ux_designer.route_discovery import RouteSpec
from bizniz.ux_designer.route_resolver import (
    CACHE_VERSION,
    ResolvedRoute,
    _build_auth_section,
    _build_openapi_section,
    _build_templates_block,
    _parse_resolver_result,
    cache_path,
    is_still_valid,
    load_cache,
    resolve_dynamic_routes,
    save_cache,
)


# ── helpers ─────────────────────────────────────────────────────────────


def _spec(path: str, source: str = "src/x.tsx") -> RouteSpec:
    params = []
    # Quick param extract for tests
    import re as _re
    params = _re.findall(r":([A-Za-z_]\w*)", path)
    return RouteSpec(
        path=path, params=params, is_dynamic=bool(params),
        source_file=source,
    )


def _resolved(template: str, url: str, verify: str = "") -> ResolvedRoute:
    return ResolvedRoute(
        template=template, concrete_url=url,
        verification_url=verify,
        strategy="existing_from_list",
    )


# ── Cache I/O ──────────────────────────────────────────────────────────


class TestCacheIO:
    def test_roundtrip(self, tmp_path):
        entries = {
            "/recipes/:id": _resolved(
                "/recipes/:id", "/recipes/abc", "/api/recipes/abc",
            ),
            "/users/:userId": _resolved(
                "/users/:userId", "/users/42", "/api/users/42",
            ),
        }
        save_cache(tmp_path, entries)
        loaded = load_cache(tmp_path)
        assert set(loaded.keys()) == set(entries.keys())
        assert loaded["/recipes/:id"].concrete_url == "/recipes/abc"
        assert loaded["/users/:userId"].verification_url == "/api/users/42"

    def test_missing_returns_empty(self, tmp_path):
        assert load_cache(tmp_path) == {}

    def test_corrupt_returns_empty(self, tmp_path):
        fp = cache_path(tmp_path)
        fp.parent.mkdir(parents=True)
        fp.write_text("not json {{{")
        assert load_cache(tmp_path) == {}

    def test_wrong_version_returns_empty(self, tmp_path):
        fp = cache_path(tmp_path)
        fp.parent.mkdir(parents=True)
        fp.write_text(json.dumps({
            "version": CACHE_VERSION + 99,
            "entries": {"/x/:id": {"template": "/x/:id", "concrete_url": "/x/1"}},
        }))
        assert load_cache(tmp_path) == {}

    def test_partial_corrupt_skips_bad_entries(self, tmp_path):
        fp = cache_path(tmp_path)
        fp.parent.mkdir(parents=True)
        fp.write_text(json.dumps({
            "version": CACHE_VERSION,
            "entries": {
                "/good/:id": {
                    "template": "/good/:id",
                    "concrete_url": "/good/1",
                },
                "/bad/:id": {"not": "a valid resolved-route"},
            },
        }))
        loaded = load_cache(tmp_path)
        assert set(loaded.keys()) == {"/good/:id"}


# ── Staleness probe ────────────────────────────────────────────────────


class TestStaleness:
    def test_no_backend_returns_valid(self):
        r = _resolved("/x/:id", "/x/1", "/api/x/1")
        assert is_still_valid(r, backend_url=None) is True

    def test_no_verification_url_returns_valid(self):
        r = _resolved("/x/:id", "/x/1", verify="")
        assert is_still_valid(r, backend_url="http://localhost:8000") is True

    def test_2xx_is_valid(self):
        r = _resolved("/x/:id", "/x/1", "/api/x/1")
        with patch(
            "bizniz.ux_designer.route_resolver.urllib.request.urlopen"
        ) as m:
            m.return_value.__enter__.return_value.status = 200
            assert is_still_valid(r, "http://localhost:8000") is True

    def test_404_is_invalid(self):
        import urllib.error
        r = _resolved("/x/:id", "/x/1", "/api/x/1")
        with patch(
            "bizniz.ux_designer.route_resolver.urllib.request.urlopen"
        ) as m:
            m.side_effect = urllib.error.HTTPError(
                url="x", code=404, msg="", hdrs=None, fp=None,
            )
            assert is_still_valid(r, "http://localhost:8000") is False

    def test_403_keeps_entry(self):
        # Auth-protected resource — we don't carry a token in the
        # probe, so 403 doesn't tell us the resource is gone.
        import urllib.error
        r = _resolved("/x/:id", "/x/1", "/api/x/1")
        with patch(
            "bizniz.ux_designer.route_resolver.urllib.request.urlopen"
        ) as m:
            m.side_effect = urllib.error.HTTPError(
                url="x", code=403, msg="", hdrs=None, fp=None,
            )
            assert is_still_valid(r, "http://localhost:8000") is True

    def test_5xx_keeps_entry(self):
        # Transient backend failure shouldn't burn cache.
        import urllib.error
        r = _resolved("/x/:id", "/x/1", "/api/x/1")
        with patch(
            "bizniz.ux_designer.route_resolver.urllib.request.urlopen"
        ) as m:
            m.side_effect = urllib.error.HTTPError(
                url="x", code=502, msg="", hdrs=None, fp=None,
            )
            assert is_still_valid(r, "http://localhost:8000") is True

    def test_network_error_keeps_entry(self):
        r = _resolved("/x/:id", "/x/1", "/api/x/1")
        with patch(
            "bizniz.ux_designer.route_resolver.urllib.request.urlopen"
        ) as m:
            m.side_effect = ConnectionError("refused")
            assert is_still_valid(r, "http://localhost:8000") is True


# ── JSON parser ────────────────────────────────────────────────────────


class TestParseResolverResult:
    def test_direct_object(self):
        text = json.dumps({
            "resolved": [{
                "template": "/recipes/:id",
                "concrete_url": "/recipes/abc",
                "verification_url": "/api/recipes/abc",
                "strategy": "existing_from_list",
                "notes": "first id in list",
            }],
        })
        out = _parse_resolver_result(text)
        assert len(out) == 1
        assert out[0].template == "/recipes/:id"
        assert out[0].concrete_url == "/recipes/abc"
        assert out[0].strategy == "existing_from_list"

    def test_fenced_json(self):
        text = (
            "Here are the resolved routes:\n"
            "```json\n"
            + json.dumps({
                "resolved": [{
                    "template": "/users/:id",
                    "concrete_url": "/users/42",
                }],
            }, indent=2)
            + "\n```\n"
        )
        out = _parse_resolver_result(text)
        assert len(out) == 1
        assert out[0].concrete_url == "/users/42"

    def test_prose_then_object(self):
        text = (
            "I resolved 2 routes. Final answer:\n"
            + json.dumps({
                "resolved": [
                    {"template": "/a/:id", "concrete_url": "/a/1"},
                    {"template": "/b/:slug", "concrete_url": "/b/hello"},
                ],
            })
        )
        out = _parse_resolver_result(text)
        assert [r.template for r in out] == ["/a/:id", "/b/:slug"]

    def test_empty_returns_empty(self):
        assert _parse_resolver_result("") == []
        assert _parse_resolver_result("nothing useful here") == []

    def test_skips_entries_without_template(self):
        text = json.dumps({
            "resolved": [
                {"template": "/a/:id", "concrete_url": "/a/1"},
                {"concrete_url": "/b/2"},  # missing template — skipped
                "not even a dict",         # skipped
            ],
        })
        out = _parse_resolver_result(text)
        assert [r.template for r in out] == ["/a/:id"]

    def test_defaults_strategy_to_other(self):
        text = json.dumps({
            "resolved": [{"template": "/x/:id", "concrete_url": "/x/1"}],
        })
        out = _parse_resolver_result(text)
        assert out[0].strategy == "other"


# ── Prompt builders ────────────────────────────────────────────────────


class TestPromptBuilders:
    def test_templates_block_lists_each_route(self):
        specs = [_spec("/recipes/:id"), _spec("/users/:userId/edit")]
        block = _build_templates_block(specs)
        assert "/recipes/:id" in block
        assert "/users/:userId/edit" in block
        assert "params: id" in block
        assert "params: userId" in block

    def test_templates_block_empty(self):
        assert "(none)" in _build_templates_block([])

    def test_openapi_section_with_file(self, tmp_path):
        fp = tmp_path / "x.openapi.json"
        fp.write_text("{}")
        out = _build_openapi_section(fp)
        assert "available at" in out
        assert str(fp) in out

    def test_openapi_section_without_file(self, tmp_path):
        out = _build_openapi_section(None)
        assert "not provided" in out

    def test_auth_section_with_contract(self):
        out = _build_auth_section("admin: admin@example.com / hunter2")
        assert "AUTH CONTRACT" in out
        assert "hunter2" in out

    def test_auth_section_without_contract(self):
        out = _build_auth_section(None)
        assert "public" in out


# ── Dispatcher (cache + agent) ─────────────────────────────────────────


class TestResolveDispatcher:
    def test_no_dynamic_routes_no_op(self, tmp_path):
        static = [_spec("/about"), _spec("/login")]
        out = resolve_dynamic_routes(tmp_path, static)
        assert out == {}

    def test_all_cached_no_agent_call(self, tmp_path):
        cached = {
            "/recipes/:id": _resolved("/recipes/:id", "/recipes/abc"),
        }
        save_cache(tmp_path, cached)
        called = []

        def fake_agent(ws, routes, base_url, auth):
            called.append(routes)
            return []

        out = resolve_dynamic_routes(
            tmp_path,
            [_spec("/recipes/:id")],
            agent_fn=fake_agent,
        )
        assert called == []  # cache hit — no agent invocation
        assert out["/recipes/:id"].concrete_url == "/recipes/abc"

    def test_partial_cache_only_misses_resolved(self, tmp_path):
        # One cached, one missing — agent called only for the miss.
        cached = {
            "/recipes/:id": _resolved("/recipes/:id", "/recipes/abc"),
        }
        save_cache(tmp_path, cached)
        seen_templates: List[List[str]] = []

        def fake_agent(ws, routes, base_url, auth):
            seen_templates.append([r.path for r in routes])
            return [_resolved("/users/:userId", "/users/42")]

        out = resolve_dynamic_routes(
            tmp_path,
            [_spec("/recipes/:id"), _spec("/users/:userId")],
            agent_fn=fake_agent,
        )
        assert seen_templates == [["/users/:userId"]]
        assert out["/recipes/:id"].concrete_url == "/recipes/abc"
        assert out["/users/:userId"].concrete_url == "/users/42"

    def test_stale_cache_invalidates_and_re_resolves(self, tmp_path):
        import urllib.error
        save_cache(tmp_path, {
            "/recipes/:id": _resolved(
                "/recipes/:id", "/recipes/dead",
                "/api/recipes/dead",
            ),
        })
        with patch(
            "bizniz.ux_designer.route_resolver.urllib.request.urlopen"
        ) as m:
            m.side_effect = urllib.error.HTTPError(
                url="x", code=404, msg="", hdrs=None, fp=None,
            )
            calls: List[List[str]] = []

            def fake_agent(ws, routes, base_url, auth):
                calls.append([r.path for r in routes])
                return [_resolved("/recipes/:id", "/recipes/alive")]

            out = resolve_dynamic_routes(
                tmp_path,
                [_spec("/recipes/:id")],
                backend_url="http://localhost:8000",
                agent_fn=fake_agent,
            )
        assert calls == [["/recipes/:id"]]
        assert out["/recipes/:id"].concrete_url == "/recipes/alive"

    def test_persists_after_resolve(self, tmp_path):
        def fake_agent(ws, routes, base_url, auth):
            return [_resolved("/users/:userId", "/users/42")]

        resolve_dynamic_routes(
            tmp_path,
            [_spec("/users/:userId")],
            agent_fn=fake_agent,
        )
        # Second call: cache hit, no agent invocation.
        calls = []

        def watching_agent(ws, routes, base_url, auth):
            calls.append(routes)
            return []

        out = resolve_dynamic_routes(
            tmp_path,
            [_spec("/users/:userId")],
            agent_fn=watching_agent,
        )
        assert calls == []
        assert out["/users/:userId"].concrete_url == "/users/42"

    def test_agent_passes_backend_url_and_auth(self, tmp_path):
        captured = {}

        def fake_agent(ws, routes, base_url, auth):
            captured["base_url"] = base_url
            captured["auth"] = auth
            return [_resolved("/x/:id", "/x/1")]

        resolve_dynamic_routes(
            tmp_path,
            [_spec("/x/:id")],
            backend_url="http://localhost:9000",
            auth_contract="admin: a / b",
            agent_fn=fake_agent,
        )
        assert captured["base_url"] == "http://localhost:9000"
        assert captured["auth"] == "admin: a / b"
