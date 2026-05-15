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


class TestComputeAppScore:
    def test_empty_views_returns_nones(self):
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        s = ProUXDesigner.compute_app_score([])
        assert s["mean"] is None
        assert s["min"] is None
        assert s["passing"] == 0
        assert s["total"] == 0

    def test_aggregates_scores(self):
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        views = [
            {"route": "/", "final_score": 8},
            {"route": "/login", "final_score": 7},
            {"route": "/dashboard", "final_score": 9},
        ]
        s = ProUXDesigner.compute_app_score(views, acceptable_score=7)
        assert s["mean"] == 8.0
        assert s["min"] == 7
        assert s["min_route"] == "/login"
        assert s["passing"] == 3
        assert s["failing"] == []
        assert s["covered"] == 3
        assert s["total"] == 3

    def test_failing_routes_sorted_by_score(self):
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        views = [
            {"route": "/", "final_score": 8},
            {"route": "/recipes/:id", "final_score": 3},
            {"route": "/dashboard", "final_score": 5},
            {"route": "/admin", "final_score": 9},
        ]
        s = ProUXDesigner.compute_app_score(views, acceptable_score=7)
        # Laggards listed worst-first
        assert s["failing"][0] == "/recipes/:id"
        assert "/dashboard" in s["failing"]
        assert s["min_route"] == "/recipes/:id"
        assert s["passing"] == 2
        assert round(s["mean"], 2) == 6.25

    def test_skips_views_without_score(self):
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        views = [
            {"route": "/", "final_score": 8},
            {"route": "/skipped", "final_score": None},
            {"route": "/dashboard", "final_score": 6},
        ]
        s = ProUXDesigner.compute_app_score(views, acceptable_score=7)
        assert s["covered"] == 2
        assert s["total"] == 3
        assert s["mean"] == 7.0

    def test_excludes_not_reviewable_views(self):
        # Ticket 2 — capture mismatches mark a view not_reviewable.
        # Those routes must NOT pollute APP SCORE even if they
        # somehow have a score attached.
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        views = [
            {"route": "/", "final_score": 8, "not_reviewable": False},
            {"route": "/admin", "final_score": 9, "not_reviewable": True,
             "capture_mismatch_reason": "captured /admin/users"},
            {"route": "/dashboard", "final_score": 7},
        ]
        s = ProUXDesigner.compute_app_score(views, acceptable_score=7)
        # /admin's 9 must not inflate the mean.
        assert s["mean"] == 7.5
        assert s["covered"] == 2
        assert s["total"] == 3
        assert s["passing"] == 2
        assert s["not_reviewable_routes"] == ["/admin"]

    def test_all_not_reviewable_yields_empty_score(self):
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        views = [
            {"route": "/admin", "final_score": None, "not_reviewable": True},
            {"route": "/recipes", "final_score": None, "not_reviewable": True},
        ]
        s = ProUXDesigner.compute_app_score(views, acceptable_score=7)
        assert s["mean"] is None
        assert s["covered"] == 0
        assert set(s["not_reviewable_routes"]) == {"/admin", "/recipes"}
        assert s["total"] == 2


class TestBucketShotsByRoute:
    def test_groups_by_requested_route(self, tmp_path):
        d = _designer()
        shots = [
            _shot_with_meta(tmp_path, "home", {"requested_route": "/"}),
            _shot_with_meta(tmp_path, "dashboard",
                            {"requested_route": "/dashboard"}),
            _shot_with_meta(tmp_path, "home-after",
                            {"requested_route": "/"}),
        ]
        out = d._bucket_shots_by_route(shots)
        assert set(out.keys()) == {"/", "/dashboard"}
        assert len(out["/"]) == 2
        assert len(out["/dashboard"]) == 1

    def test_skips_shots_without_meta(self, tmp_path):
        d = _designer()
        png = tmp_path / "no-meta.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        shots_no_meta = [{"name": "no-meta", "path": png, "bytes": b""}]
        with_meta = _shot_with_meta(tmp_path, "home", {"requested_route": "/"})
        out = d._bucket_shots_by_route(shots_no_meta + [with_meta])
        # Only the shot with a parseable meta is kept.
        assert out == {"/": [with_meta]}

    def test_empty_returns_empty(self, tmp_path):
        d = _designer()
        assert d._bucket_shots_by_route([]) == {}


class TestAdaptiveBudget:
    def test_no_history_returns_default(self):
        d = _designer()
        d._max_home_iterations = 2
        assert d._budget_for_route(None) == 2

    def test_prior_hit_cap_below_threshold_bumps(self):
        from bizniz.ux_designer.review_store import ReviewRecord
        from datetime import datetime
        d = _designer()
        d._max_home_iterations = 2
        d._acceptable_score = 7
        rec = ReviewRecord(
            project_slug="x", route="/recipes/:id",
            last_score=3,
            iterations_to_acceptable=2,  # hit cap
            last_reviewed_at=datetime.utcnow(),
        )
        assert d._budget_for_route(rec) == 4

    def test_prior_passed_keeps_default(self):
        from bizniz.ux_designer.review_store import ReviewRecord
        from datetime import datetime
        d = _designer()
        d._max_home_iterations = 2
        d._acceptable_score = 7
        rec = ReviewRecord(
            project_slug="x", route="/dashboard",
            last_score=8,  # above threshold
            iterations_to_acceptable=2,
            last_reviewed_at=datetime.utcnow(),
        )
        assert d._budget_for_route(rec) == 2

    def test_prior_low_score_but_short_iters_keeps_default(self):
        # Score below threshold but didn't hit cap → vision recommended
        # stop early. No reason to bump budget.
        from bizniz.ux_designer.review_store import ReviewRecord
        from datetime import datetime
        d = _designer()
        d._max_home_iterations = 2
        d._acceptable_score = 7
        rec = ReviewRecord(
            project_slug="x", route="/x",
            last_score=4,
            iterations_to_acceptable=1,  # stopped early
            last_reviewed_at=datetime.utcnow(),
        )
        assert d._budget_for_route(rec) == 2


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
