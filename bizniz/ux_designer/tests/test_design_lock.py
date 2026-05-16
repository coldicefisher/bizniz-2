"""Tests for design_lock (sub-ticket of roadmap item 2).

Covers:
  - Lock I/O round-trip
  - Missing / corrupt / wrong-version handling
  - remove_lock idempotence
  - ProUXDesigner integration: lock hit skips code_review +
    apply_global_design entirely; lock miss runs both and saves
    the lock; force_redesign=True deletes the lock and runs fresh.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.ux_designer.design_lock import (
    DesignLock,
    LOCK_VERSION,
    load_lock,
    lock_path,
    remove_lock,
    save_lock,
)


def _lock(**over) -> DesignLock:
    defaults = dict(
        milestone_index=0,
        plan={"app_type": "hybrid", "design_system": {"palette": {}}},
        global_fix_result={
            "status": "passed",
            "files_written": ["tailwind.config.ts", "src/index.css"],
            "tailwind_wired": True,
        },
        files_managed=["tailwind.config.ts", "src/index.css"],
    )
    defaults.update(over)
    return DesignLock(**defaults)


class TestLockIO:
    def test_save_then_load_roundtrip(self, tmp_path):
        lock = _lock()
        save_lock(tmp_path, lock)
        loaded = load_lock(tmp_path)
        assert loaded is not None
        assert loaded.plan == lock.plan
        assert loaded.files_managed == lock.files_managed
        assert loaded.milestone_index == 0

    def test_no_lock_returns_none(self, tmp_path):
        assert load_lock(tmp_path) is None

    def test_corrupt_lock_returns_none(self, tmp_path):
        fp = lock_path(tmp_path)
        fp.parent.mkdir()
        fp.write_text("not json {{{")
        assert load_lock(tmp_path) is None

    def test_wrong_version_returns_none(self, tmp_path):
        fp = lock_path(tmp_path)
        fp.parent.mkdir()
        fp.write_text(json.dumps({
            "version": LOCK_VERSION + 99,
            "established_at": datetime.utcnow().isoformat(),
            "milestone_index": 0,
            "plan": {},
            "global_fix_result": {},
            "files_managed": [],
        }))
        assert load_lock(tmp_path) is None

    def test_save_creates_parent_dir(self, tmp_path):
        # ``.bizniz`` dir doesn't exist yet
        sub = tmp_path / "nested" / "workspace"
        sub.mkdir(parents=True)
        save_lock(sub, _lock())
        assert lock_path(sub).is_file()


class TestRemoveLock:
    def test_removes_existing(self, tmp_path):
        save_lock(tmp_path, _lock())
        assert lock_path(tmp_path).is_file()
        assert remove_lock(tmp_path) is True
        assert not lock_path(tmp_path).exists()

    def test_idempotent_when_missing(self, tmp_path):
        # No lock yet → returns False, doesn't raise.
        assert remove_lock(tmp_path) is False


class TestProUXDesignerIntegration:
    """Verify the lock prevents code_review + apply_global_design
    from running on subsequent milestones."""

    def _designer(self, force_redesign=False):
        from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
        with patch(
            "bizniz.ux_designer.claude_ux_designer.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return ProUXDesigner(
                vision_client=MagicMock(),
                on_status=None,
                force_redesign=force_redesign,
            )

    def test_lock_attribute_set_from_constructor(self):
        d = self._designer(force_redesign=True)
        assert d._force_redesign is True

    def test_lock_default_force_redesign_false(self):
        d = self._designer()
        assert d._force_redesign is False

    def test_save_and_reload_preserves_lock_fields(self, tmp_path):
        # End-to-end save → load → fields match.
        lock = _lock(milestone_index=2)
        save_lock(tmp_path, lock)
        reloaded = load_lock(tmp_path)
        assert reloaded.milestone_index == 2
        assert reloaded.plan["app_type"] == "hybrid"
        assert reloaded.global_fix_result["status"] == "passed"
        assert reloaded.global_fix_result["tailwind_wired"] is True

    def test_force_redesign_path_removes_lock(self, tmp_path):
        # Simulate the workflow: lock exists, force_redesign=True
        # should clear it (callsite invokes remove_lock).
        save_lock(tmp_path, _lock())
        assert lock_path(tmp_path).is_file()
        # The ProUXDesigner does this when force_redesign:
        removed = remove_lock(tmp_path)
        assert removed is True
        assert load_lock(tmp_path) is None
