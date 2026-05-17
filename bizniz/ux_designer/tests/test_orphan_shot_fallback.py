"""Tests for the orphan-shot fallback removal (D12, 2026-05-17).

Acceptance from ``docs/backlog/ux_orphan_shot_fallback.md``:

    A view whose route was not captured by the Playwright script
    gets marked ``not_reviewable`` with reason "no screenshot
    captured". App score excludes it. Coder is NOT dispatched
    with fixes based on an unrelated screenshot.

    Regression test: a fake workspace with PNGs for routes A and B
    (no metas) and a ``_view_iteration`` call for route C must
    produce ``not_reviewable=True``, not substitute A or B's PNG.

The bug: ``_take_view_screenshots`` used to substitute orphan
(no-meta) PNGs from OTHER routes when ``_bucket_shots_by_route``
returned 0 matches. The vision model then evaluated unrelated
screenshots against this route's design spec and dispatched code
fixes against the wrong page.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.pro_ux_designer import ProUXDesigner


def _designer():
    with patch(
        "bizniz.ux_designer.claude_ux_designer.shutil.which",
        return_value="/usr/bin/claude",
    ):
        return ProUXDesigner(vision_client=MagicMock(), on_status=None)


def _service():
    return ServiceDefinition(
        name="frontend", service_type="frontend",
        framework="react", language="typescript",
        description="x", workspace_name="frontend",
        port=5173, depends_on=[], requirements=[], skeleton="react",
    )


def _shot(path: Path, meta: dict = None) -> dict:
    """Build a fake screenshot dict; optionally write a .meta.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    if meta is not None:
        meta_path = path.with_suffix(".meta.json")
        meta_path.write_text(json.dumps(meta))
    return {"name": path.stem, "path": str(path), "bytes": b""}


class TestOrphanFallbackRemoved:
    """The core regression: orphans from other routes must NOT
    substitute for the asked-about route."""

    def test_orphan_pngs_from_other_routes_are_not_substituted(
        self, tmp_path,
    ):
        """Workspace has PNGs for routes A and B (no metas — orphans).
        Asked about route C. Result must be empty list, not the
        A/B PNGs."""
        d = _designer()
        # Two orphan PNGs that aren't for route C.
        shots_for_other_routes = [
            _shot(tmp_path / "deals-list.png"),
            _shot(tmp_path / "deal-detail.png"),
        ]
        # ``_take_screenshots`` is the captured grab-bag —
        # _take_view_screenshots filters/buckets it.
        d._take_screenshots = MagicMock(
            return_value=shots_for_other_routes,
        )
        d._translate_to_concrete = MagicMock(return_value=["/dashboard"])
        d._rekey_to_templates = MagicMock(return_value={})  # no /dashboard bucket

        workspace = MagicMock()
        workspace.root = str(tmp_path)

        shots = d._take_view_screenshots(
            route="/dashboard",
            view_label="dashboard",
            iteration=2,  # iter>=2 forces per-route capture (skips precaptured)
            service=_service(),
            workspace=workspace,
            compose_path="x",
            problem_statement="x",
            milestone_scope="",
            auth_contract=None,
            backend_url=None,
        )

        # Critical assertion: no orphan substitution. The function
        # used to return shots_for_other_routes when buckets["dashboard"]
        # was empty; now it must return [].
        assert shots == [], (
            f"Orphan-shot fallback regressed: returned {len(shots)} "
            f"shots for /dashboard when none matched. The fallback "
            f"was the root cause of CRM M4 evaluating deals "
            f"screenshots against the dashboard spec."
        )

    def test_returns_route_matched_shots_when_available(self, tmp_path):
        """Sanity: when the bucket DOES contain matching shots, return
        them. The fix should not break the happy path."""
        d = _designer()
        right_shot = _shot(
            tmp_path / "dashboard.png",
            meta={"requested_route": "/dashboard",
                  "final_pathname": "/dashboard"},
        )
        wrong_shots = [
            _shot(tmp_path / "deals.png"),  # orphan
        ]
        d._take_screenshots = MagicMock(
            return_value=[right_shot] + wrong_shots,
        )
        d._translate_to_concrete = MagicMock(return_value=["/dashboard"])
        d._rekey_to_templates = MagicMock(
            return_value={"/dashboard": [right_shot]},
        )

        workspace = MagicMock()
        workspace.root = str(tmp_path)

        shots = d._take_view_screenshots(
            route="/dashboard",
            view_label="dashboard",
            iteration=2,
            service=_service(),
            workspace=workspace,
            compose_path="x",
            problem_statement="x",
            milestone_scope="",
            auth_contract=None,
            backend_url=None,
        )

        assert len(shots) == 1
        assert "dashboard" in str(shots[0]["path"])

    def test_empty_capture_returns_empty(self, tmp_path):
        """When the underlying ``_take_screenshots`` returns nothing
        at all (e.g. Playwright failed to launch), the function
        should return empty list — caller marks not_reviewable."""
        d = _designer()
        d._take_screenshots = MagicMock(return_value=[])
        d._translate_to_concrete = MagicMock(return_value=["/dashboard"])
        d._rekey_to_templates = MagicMock(return_value={})

        workspace = MagicMock()
        workspace.root = str(tmp_path)

        shots = d._take_view_screenshots(
            route="/dashboard",
            view_label="dashboard",
            iteration=2,
            service=_service(),
            workspace=workspace,
            compose_path="x",
            problem_statement="x",
            milestone_scope="",
            auth_contract=None,
            backend_url=None,
        )

        assert shots == []
