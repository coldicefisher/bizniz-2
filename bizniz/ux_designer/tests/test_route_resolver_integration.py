"""Tests for the ProUXDesigner ↔ route_resolver integration helpers.

The resolver itself is unit-tested in test_route_resolver.py. These
tests cover the three glue methods on ProUXDesigner:
  - _translate_to_concrete  (template → concrete URL before capture)
  - _rekey_to_templates     (concrete URL → template after capture)
  - _find_openapi           (best-effort OpenAPI discovery)
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.ux_designer.pro_ux_designer import ProUXDesigner


def _designer():
    with patch(
        "bizniz.ux_designer.claude_ux_designer.shutil.which",
        return_value="/usr/bin/claude",
    ):
        return ProUXDesigner(vision_client=MagicMock(), on_status=None)


class TestTranslateToConcrete:
    def test_no_mapping_returns_input_unchanged(self):
        d = _designer()
        d._resolved_url_by_template = {}
        assert d._translate_to_concrete(["/recipes/:id", "/"]) == [
            "/recipes/:id", "/",
        ]

    def test_substitutes_known_templates(self):
        d = _designer()
        d._resolved_url_by_template = {
            "/recipes/:id": "/recipes/abc",
            "/users/:userId": "/users/42",
        }
        out = d._translate_to_concrete([
            "/recipes/:id", "/", "/users/:userId", "/login",
        ])
        assert out == ["/recipes/abc", "/", "/users/42", "/login"]

    def test_unknown_template_passes_through(self):
        d = _designer()
        d._resolved_url_by_template = {"/recipes/:id": "/recipes/abc"}
        out = d._translate_to_concrete(["/admin/users/:id"])
        assert out == ["/admin/users/:id"]

    def test_missing_attr_returns_input(self):
        # If review_frontend hasn't run yet, the attribute may not
        # exist. Helper should not blow up.
        d = _designer()
        if hasattr(d, "_resolved_url_by_template"):
            delattr(d, "_resolved_url_by_template")
        assert d._translate_to_concrete(["/a", "/b"]) == ["/a", "/b"]


class TestRekeyToTemplates:
    def test_no_mapping_returns_input_unchanged(self):
        d = _designer()
        d._resolved_url_by_template = {}
        buckets = {"/recipes/abc": [{"name": "x"}]}
        assert d._rekey_to_templates(buckets) == buckets

    def test_concrete_url_remaps_to_template(self):
        d = _designer()
        d._resolved_url_by_template = {"/recipes/:id": "/recipes/abc"}
        buckets = {
            "/recipes/abc": [{"name": "detail"}],
            "/": [{"name": "home"}],
        }
        out = d._rekey_to_templates(buckets)
        assert set(out.keys()) == {"/recipes/:id", "/"}
        assert out["/recipes/:id"][0]["name"] == "detail"
        assert out["/"][0]["name"] == "home"

    def test_multiple_dynamic_routes(self):
        d = _designer()
        d._resolved_url_by_template = {
            "/recipes/:id": "/recipes/abc",
            "/users/:id": "/users/42",
        }
        buckets = {
            "/recipes/abc": [{"name": "r"}],
            "/users/42": [{"name": "u"}],
        }
        out = d._rekey_to_templates(buckets)
        assert "/recipes/:id" in out
        assert "/users/:id" in out

    def test_static_routes_unchanged(self):
        d = _designer()
        d._resolved_url_by_template = {"/recipes/:id": "/recipes/abc"}
        buckets = {"/dashboard": [{"name": "d"}]}
        out = d._rekey_to_templates(buckets)
        assert out == {"/dashboard": [{"name": "d"}]}

    def test_empty_buckets(self):
        d = _designer()
        d._resolved_url_by_template = {"/recipes/:id": "/recipes/abc"}
        assert d._rekey_to_templates({}) == {}


class TestFindOpenapi:
    def test_returns_first_contract(self, tmp_path):
        # Frontend workspace is <project>/frontend; openapi lives at
        # <project>/contracts/*.openapi.json.
        project = tmp_path
        (project / "contracts").mkdir()
        (project / "contracts" / "backend.openapi.json").write_text("{}")
        (project / "frontend").mkdir()
        d = _designer()
        found = d._find_openapi(project / "frontend")
        assert found is not None
        assert found.name == "backend.openapi.json"

    def test_no_contracts_dir_returns_none(self, tmp_path):
        (tmp_path / "frontend").mkdir()
        d = _designer()
        assert d._find_openapi(tmp_path / "frontend") is None

    def test_empty_contracts_dir_returns_none(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "frontend").mkdir()
        d = _designer()
        assert d._find_openapi(tmp_path / "frontend") is None

    def test_picks_alphabetically_first(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "b.openapi.json").write_text("{}")
        (tmp_path / "contracts" / "a.openapi.json").write_text("{}")
        (tmp_path / "frontend").mkdir()
        d = _designer()
        found = d._find_openapi(tmp_path / "frontend")
        assert found.name == "a.openapi.json"
