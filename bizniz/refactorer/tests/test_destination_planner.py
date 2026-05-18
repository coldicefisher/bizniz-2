"""Tests for the v3 refactorer's destination planner (Step 2)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.refactorer.destination_planner import (
    DestinationPlan,
    DestinationPlanner,
    SignatureChange,
    _parse_plan,
)


def _valid_plan_json(**overrides) -> str:
    base = {
        "destination_path": "core/python/recipes/pricing.py",
        "destination_kind": "existing",
        "functions_to_move": ["compute_total_with_tax"],
        "signature_changes": [
            {"parameter": "db", "reason": "was Depends(get_db)"},
        ],
        "consumer_import": (
            "from python_core.recipes.pricing import compute_total_with_tax"
        ),
        "rationale": "consolidates tax math into existing pricing module",
    }
    base.update(overrides)
    return json.dumps(base)


# ── Parsing ──────────────────────────────────────────────────────


class TestParsePlan:
    def test_valid_plan_parses(self):
        plan = _parse_plan(_valid_plan_json())
        assert plan is not None
        assert plan.destination_path == "core/python/recipes/pricing.py"
        assert plan.destination_kind == "existing"
        assert plan.functions_to_move == ["compute_total_with_tax"]
        assert len(plan.signature_changes) == 1
        assert plan.signature_changes[0].parameter == "db"

    def test_strips_code_fences(self):
        raw = f"```json\n{_valid_plan_json()}\n```"
        plan = _parse_plan(raw)
        assert plan is not None

    def test_extracts_from_surrounding_prose(self):
        raw = (
            "Here's the plan I came up with:\n"
            + _valid_plan_json()
            + "\nLet me know if you want changes."
        )
        plan = _parse_plan(raw)
        assert plan is not None

    def test_invalid_destination_kind_rejected(self):
        # Pydantic validation should fail; _parse_plan returns None.
        bad = json.dumps({
            "destination_path": "core/python/x.py",
            "destination_kind": "neither",  # invalid
            "functions_to_move": [],
            "signature_changes": [],
            "consumer_import": "from python_core.x import y",
            "rationale": "x",
        })
        assert _parse_plan(bad) is None

    def test_missing_required_fields_rejected(self):
        bad = json.dumps({
            "destination_path": "core/python/x.py",
            # missing destination_kind, consumer_import, rationale
        })
        assert _parse_plan(bad) is None

    def test_empty_returns_none(self):
        assert _parse_plan("") is None

    def test_garbage_returns_none(self):
        assert _parse_plan("not json at all") is None


# ── Planner dispatch ─────────────────────────────────────────────


class TestDestinationPlanner:
    def test_passes_through_valid_plan(self, tmp_path):
        invoker = MagicMock(return_value=_valid_plan_json())
        planner = DestinationPlanner(
            project_root=tmp_path,
            llm_invoker=invoker,
        )
        plan = planner.plan_for(
            candidate_kind="cpd_duplicate",
            summary="duplicated tax calc",
            source_file="backend/app/api/routes/recipes.py",
            snippet="def compute(): pass",
        )
        assert plan.destination_path == "core/python/recipes/pricing.py"
        assert plan.destination_kind == "existing"

    def test_invoker_exception_yields_fallback(self, tmp_path):
        invoker = MagicMock(side_effect=RuntimeError("api down"))
        planner = DestinationPlanner(
            project_root=tmp_path,
            llm_invoker=invoker,
        )
        plan = planner.plan_for(
            candidate_kind="cpd_duplicate",
            summary="something",
            source_file="x.py",
            snippet="",
        )
        # Fallback plan lands in uncategorized with the failure note.
        assert plan.destination_path.startswith("core/python/uncategorized/")
        assert plan.destination_kind == "new"
        assert "api down" in plan.rationale

    def test_bad_response_yields_fallback(self, tmp_path):
        invoker = MagicMock(return_value="not even close to JSON")
        planner = DestinationPlanner(
            project_root=tmp_path,
            llm_invoker=invoker,
        )
        plan = planner.plan_for(
            candidate_kind="anti_pattern",
            summary="hardcoded URL",
            source_file="x.py",
            snippet="url = 'http://...'",
        )
        assert plan.destination_kind == "new"
        assert "parse" in plan.rationale.lower()

    def test_out_of_scope_destination_rejected(self, tmp_path):
        """Agent proposes a destination NOT under core/python/ —
        sanity check rejects it and falls back. Protects against
        a confused agent putting domain code outside the core lib."""
        bad = _valid_plan_json(
            destination_path="backend/app/services/pricing.py",
        )
        invoker = MagicMock(return_value=bad)
        planner = DestinationPlanner(
            project_root=tmp_path,
            llm_invoker=invoker,
        )
        plan = planner.plan_for(
            candidate_kind="cpd_duplicate",
            summary="x", source_file="x.py", snippet="",
        )
        # Fell back because the agent's destination was out of scope.
        assert plan.destination_path.startswith("core/python/uncategorized/")
        assert "out-of-scope" in plan.rationale.lower()

    def test_prompt_includes_summary_and_kind(self, tmp_path):
        seen = {}

        def fake(_sys, user):
            seen["user"] = user
            return _valid_plan_json()

        planner = DestinationPlanner(
            project_root=tmp_path, llm_invoker=fake,
        )
        planner.plan_for(
            candidate_kind="misplaced_logic",
            summary="unique-marker-xyz",
            source_file="app/api/routes/recipes.py",
            snippet="def x(): pass",
            line_range=(10, 30),
            suggested_path="core/python/recipes/heuristic_guess.py",
        )
        assert "unique-marker-xyz" in seen["user"]
        assert "misplaced_logic" in seen["user"]
        assert "10-30" in seen["user"]
        # The deterministic hint is surfaced.
        assert "heuristic_guess" in seen["user"]

    def test_long_snippet_truncated(self, tmp_path):
        seen = {}

        def fake(_sys, user):
            seen["user"] = user
            return _valid_plan_json()

        planner = DestinationPlanner(
            project_root=tmp_path, llm_invoker=fake,
        )
        planner.plan_for(
            candidate_kind="cpd_duplicate",
            summary="x", source_file="x.py",
            snippet="x" * 20000,
        )
        # Total prompt stays bounded.
        assert len(seen["user"]) < 6000

    def test_fallback_slug_is_path_safe(self, tmp_path):
        invoker = MagicMock(side_effect=RuntimeError("x"))
        planner = DestinationPlanner(
            project_root=tmp_path, llm_invoker=invoker,
        )
        plan = planner.plan_for(
            candidate_kind="cpd_duplicate",
            summary="duplicated logic in /api/recipes!?",
            source_file="x.py",
            snippet="",
        )
        # Slug has no slashes / non-alnum chars.
        slug = plan.destination_path.rsplit("/", 1)[-1]
        assert "/" not in slug.replace(".py", "")
        assert "!" not in slug
