"""Tests for the ReviewStore + dirty tracking."""
import time
from datetime import datetime
from pathlib import Path

import pytest

from bizniz.ux_designer.review_store import (
    GLOBAL_STYLE_FILES,
    ReviewRecord,
    ReviewStore,
    max_global_mtime,
    source_mtime,
)


def _store(tmp_path) -> ReviewStore:
    return ReviewStore(tmp_path / ".bizniz" / "ux_reviews.db")


def _record(**overrides) -> ReviewRecord:
    defaults = dict(
        project_slug="recipe_box",
        route="/dashboard",
        view_type="list",
        requires_auth=True,
        last_score=8,
        iterations_to_acceptable=2,
        last_reviewed_at=datetime(2026, 5, 14, 12, 0, 0),
        source_file="src/routes/dashboard.tsx",
        source_mtime=1700000000.0,
        global_styles_mtime=1700000000.0,
    )
    defaults.update(overrides)
    return ReviewRecord(**defaults)


class TestRoundTrip:
    def test_insert_then_get(self, tmp_path):
        store = _store(tmp_path)
        store.upsert(_record())
        got = store.get("recipe_box", "/dashboard")
        assert got is not None
        assert got.last_score == 8
        assert got.requires_auth is True
        assert got.iterations_to_acceptable == 2

    def test_upsert_overwrites(self, tmp_path):
        store = _store(tmp_path)
        store.upsert(_record(last_score=4, iterations_to_acceptable=1))
        store.upsert(_record(last_score=9, iterations_to_acceptable=3))
        got = store.get("recipe_box", "/dashboard")
        assert got.last_score == 9
        assert got.iterations_to_acceptable == 3

    def test_get_missing_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.get("recipe_box", "/dashboard") is None

    def test_list_for_project(self, tmp_path):
        store = _store(tmp_path)
        store.upsert(_record(route="/"))
        store.upsert(_record(route="/login"))
        store.upsert(_record(project_slug="other", route="/x"))
        rows = store.list_for_project("recipe_box")
        assert sorted(r.route for r in rows) == ["/", "/login"]


class TestIsDirty:
    def test_below_threshold_dirty(self):
        rec = _record(last_score=5)
        dirty, reason = ReviewStore.is_dirty(
            rec,
            current_source_mtime=rec.source_mtime,
            current_globals_mtime=rec.global_styles_mtime,
            acceptable_score=7,
        )
        assert dirty is True
        assert "below threshold" in reason

    def test_at_threshold_with_unchanged_files_clean(self):
        rec = _record(last_score=7)
        dirty, _ = ReviewStore.is_dirty(
            rec,
            current_source_mtime=rec.source_mtime,
            current_globals_mtime=rec.global_styles_mtime,
            acceptable_score=7,
        )
        assert dirty is False

    def test_source_file_changed_dirty(self):
        rec = _record(last_score=8, source_mtime=1700000000.0)
        dirty, reason = ReviewStore.is_dirty(
            rec,
            current_source_mtime=1700000100.0,  # newer
            current_globals_mtime=rec.global_styles_mtime,
            acceptable_score=7,
        )
        assert dirty is True
        assert "source file changed" in reason

    def test_global_style_changed_dirty(self):
        rec = _record(last_score=8, global_styles_mtime=1700000000.0)
        dirty, reason = ReviewStore.is_dirty(
            rec,
            current_source_mtime=rec.source_mtime,
            current_globals_mtime=1700000100.0,
            acceptable_score=7,
        )
        assert dirty is True
        assert "global style file changed" in reason

    def test_unknown_mtimes_pass_through(self):
        rec = _record(last_score=8, source_mtime=None, global_styles_mtime=None)
        dirty, _ = ReviewStore.is_dirty(
            rec,
            current_source_mtime=None,
            current_globals_mtime=None,
            acceptable_score=7,
        )
        # No mtime data to compare against → not dirty (don't punish
        # routes that predate the mtime contract).
        assert dirty is False


class TestFileMtimeHelpers:
    def test_max_global_mtime_picks_newest(self, tmp_path):
        # Create two watched files with distinct mtimes.
        (tmp_path / "tailwind.config.ts").write_text("// theme")
        (tmp_path / "src").mkdir()
        (tmp_path / "src/index.css").write_text("/* tw */")
        # Touch the css file slightly newer.
        time.sleep(0.01)
        (tmp_path / "src/index.css").write_text("/* tw v2 */")
        m = max_global_mtime(tmp_path)
        assert m is not None
        # Confirm it picked the newer of the two.
        css_mtime = (tmp_path / "src/index.css").stat().st_mtime
        assert m == css_mtime

    def test_max_global_mtime_no_files(self, tmp_path):
        assert max_global_mtime(tmp_path) is None

    def test_source_mtime_existing(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src/routes").mkdir()
        f = tmp_path / "src/routes/home.tsx"
        f.write_text("export default {};")
        m = source_mtime(tmp_path, "src/routes/home.tsx")
        assert m == f.stat().st_mtime

    def test_source_mtime_missing_returns_none(self, tmp_path):
        assert source_mtime(tmp_path, "src/routes/nope.tsx") is None

    def test_source_mtime_none_path(self, tmp_path):
        assert source_mtime(tmp_path, None) is None


class TestGlobalStyleWatch:
    def test_watched_files_includes_expected(self):
        assert "tailwind.config.ts" in GLOBAL_STYLE_FILES
        assert "src/index.css" in GLOBAL_STYLE_FILES
        assert "postcss.config.js" in GLOBAL_STYLE_FILES
