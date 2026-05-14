"""Tests for ProUXDesigner capture verification (Stage B)."""
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


def _shot_with_meta(tmp_path, name, meta_dict):
    """Create a fake .png + .meta.json pair and return the screenshot
    dict the verifier expects."""
    png = tmp_path / f"{name}.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta_dict))
    return {"name": name, "path": png, "bytes": b""}


class TestRouteToRegex:
    def test_static_route_matches_exactly(self):
        r = ProUXDesigner._route_to_regex("/login")
        assert r.match("/login")
        assert not r.match("/login/extra")
        assert not r.match("/")

    def test_dynamic_route_matches_any_id(self):
        r = ProUXDesigner._route_to_regex("/recipes/:id")
        assert r.match("/recipes/abc-123")
        assert r.match("/recipes/new")  # caller handles sibling collision
        assert not r.match("/recipes/abc/edit")
        assert not r.match("/login")

    def test_trailing_slash_optional(self):
        r = ProUXDesigner._route_to_regex("/admin/users")
        assert r.match("/admin/users")
        assert r.match("/admin/users/")


class TestVerifyCapture:
    def test_match_url(self, tmp_path):
        d = _designer()
        shots = [_shot_with_meta(tmp_path, "dashboard", {
            "final_pathname": "/dashboard"
        })]
        ok, _ = d._verify_capture("/dashboard", shots)
        assert ok is True

    def test_redirected_to_login_is_mismatch(self, tmp_path):
        d = _designer()
        shots = [_shot_with_meta(tmp_path, "dashboard", {
            "final_pathname": "/login"
        })]
        ok, reason = d._verify_capture("/dashboard", shots)
        assert ok is False
        assert "/login" in reason

    def test_dynamic_route_with_literal_sibling_collision(self, tmp_path):
        """The exact failure mode from recipe_box: asked for
        /recipes/:id, the captured URL is /recipes/new (a literal
        sibling route). Detector should flag this even though the
        regex-only check would let it through."""
        d = _designer()
        shots = [_shot_with_meta(tmp_path, "recipe-detail", {
            "final_pathname": "/recipes/new"
        })]
        ok, reason = d._verify_capture(
            "/recipes/:id", shots,
            sibling_routes=["/recipes/new", "/recipes", "/"],
        )
        assert ok is False
        assert "collision" in reason

    def test_dynamic_route_with_real_id_passes(self, tmp_path):
        d = _designer()
        shots = [_shot_with_meta(tmp_path, "recipe-detail", {
            "final_pathname": "/recipes/c0ffee"
        })]
        ok, _ = d._verify_capture(
            "/recipes/:id", shots,
            sibling_routes=["/recipes/new", "/recipes"],
        )
        assert ok is True

    def test_missing_meta_passes_through(self, tmp_path):
        """Older screenshots without meta files shouldn't fail
        verification — that would punish runs that predate the
        meta-emission contract."""
        d = _designer()
        png = tmp_path / "home.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        # No meta file written.
        shots = [{"name": "home", "path": png, "bytes": b""}]
        ok, _ = d._verify_capture("/", shots)
        assert ok is True

    def test_empty_screenshots_fail_clean(self, tmp_path):
        d = _designer()
        ok, reason = d._verify_capture("/", [])
        assert ok is False
        assert "no screenshots" in reason

    def test_trailing_slash_normalized(self, tmp_path):
        d = _designer()
        shots = [_shot_with_meta(tmp_path, "admin", {
            "final_pathname": "/admin/"
        })]
        ok, _ = d._verify_capture("/admin", shots)
        assert ok is True
