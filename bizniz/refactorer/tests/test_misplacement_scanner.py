"""Tests for the v3 refactorer's misplacement scanner (Signal 2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.refactorer.misplacement_scanner import (
    MisplacedLogicCandidate,
    MisplacementReport,
    MisplacementScanner,
    _parse_response,
)


# ── Parsing ──────────────────────────────────────────────────────


class TestParseResponse:
    def test_empty_candidates(self):
        out = _parse_response('{"candidates": []}', file_path="x.py")
        assert out == []

    def test_one_candidate(self):
        raw = (
            '{"candidates": [{"function_name": "create_recipe",'
            ' "line_start": 42, "line_end": 67,'
            ' "why": "computes tax", '
            ' "suggested_core_module": "core/python/recipes/pricing.py"}]}'
        )
        out = _parse_response(raw, file_path="app/api/routes/recipes.py")
        assert len(out) == 1
        c = out[0]
        assert c.function_name == "create_recipe"
        assert c.line_range == (42, 67)
        assert c.suggested_core_module == "core/python/recipes/pricing.py"
        assert c.file_path == "app/api/routes/recipes.py"

    def test_swaps_inverted_line_range(self):
        raw = (
            '{"candidates": [{"function_name": "f", "line_start": 100,'
            ' "line_end": 50, "why": "x", "suggested_core_module": "y"}]}'
        )
        out = _parse_response(raw, file_path="x.py")
        assert out[0].line_range == (50, 100)

    def test_strips_code_fences(self):
        raw = '```json\n{"candidates": []}\n```'
        assert _parse_response(raw, file_path="x.py") == []

    def test_invalid_json_returns_empty(self):
        assert _parse_response("not json", file_path="x.py") == []
        assert _parse_response("", file_path="x.py") == []

    def test_missing_candidates_key_returns_empty(self):
        out = _parse_response('{"something": []}', file_path="x.py")
        assert out == []

    def test_candidates_not_a_list_returns_empty(self):
        out = _parse_response('{"candidates": "oops"}', file_path="x.py")
        assert out == []

    def test_drops_malformed_candidates(self):
        # Two candidates: first malformed (missing line_end), second valid.
        raw = (
            '{"candidates": ['
            '{"function_name": "broken", "line_start": 1, "why": "x", '
            ' "suggested_core_module": "y"},'
            '{"function_name": "ok", "line_start": 5, "line_end": 10,'
            ' "why": "real", "suggested_core_module": "core/x.py"}'
            ']}'
        )
        out = _parse_response(raw, file_path="x.py")
        assert len(out) == 1
        assert out[0].function_name == "ok"

    def test_extracts_object_from_surrounding_prose(self):
        raw = (
            "Sure, I scanned the file:\n"
            '{"candidates": [{"function_name": "f", "line_start": 1,'
            ' "line_end": 2, "why": "x", "suggested_core_module": "y"}]}'
            "\nLet me know if you want more."
        )
        out = _parse_response(raw, file_path="x.py")
        assert len(out) == 1


# ── Scanner dispatch ─────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    """Build a tiny multi-service project tree under tmp."""
    (tmp_path / "backend" / "app" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "workers").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "main.py").write_text("# entry\n")
    # Should be scanned:
    (tmp_path / "backend" / "app" / "api" / "routes" / "recipes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n\n"
        "@router.post('/recipes')\n"
        "def create_recipe():\n"
        "    return {}\n"
    )
    (tmp_path / "backend" / "app" / "workers" / "indexer.py").write_text(
        "def reindex(): pass\n"
    )
    # Should be skipped:
    (tmp_path / "backend" / "app" / "api" / "routes" / "__init__.py").write_text("")
    (tmp_path / "backend" / "app" / "api" / "routes" / "test_recipes.py").write_text(
        "def test_create(): pass\n"
    )
    # Non-frontline file — not under api/routes/, workers/, or cli/.
    (tmp_path / "backend" / "app" / "models" / "user.py").parent.mkdir(parents=True)
    (tmp_path / "backend" / "app" / "models" / "user.py").write_text(
        "class User: pass\n"
    )
    # .bizniz state — never scanned.
    (tmp_path / ".bizniz" / "runs" / "x" / "stuff.py").parent.mkdir(parents=True)
    (tmp_path / ".bizniz" / "runs" / "x" / "stuff.py").write_text(
        "# state\n"
    )
    return tmp_path


class TestScannerDiscovery:
    def test_discovers_frontline_files_only(self, tmp_path):
        _project(tmp_path)
        scanner = MisplacementScanner(
            project_root=tmp_path,
            llm_invoker=MagicMock(return_value='{"candidates": []}'),
        )
        files = scanner._discover_frontline_files()
        rels = sorted(str(p.relative_to(tmp_path)) for p in files)
        assert "backend/app/api/routes/recipes.py" in rels
        assert "backend/app/workers/indexer.py" in rels
        # __init__.py and test_*.py excluded.
        assert "backend/app/api/routes/__init__.py" not in rels
        assert "backend/app/api/routes/test_recipes.py" not in rels
        # Non-frontline file excluded.
        assert "backend/app/models/user.py" not in rels
        # main.py is not in api/routes/, workers/, or cli/.
        assert "backend/app/main.py" not in rels
        # .bizniz state excluded.
        assert not any(".bizniz" in r for r in rels)


class TestScanFlow:
    def test_zero_candidate_response_produces_empty_report(self, tmp_path):
        _project(tmp_path)
        invoker = MagicMock(return_value='{"candidates": []}')
        scanner = MisplacementScanner(
            project_root=tmp_path, llm_invoker=invoker,
        )
        report = scanner.scan()
        assert report.candidates == []
        assert report.files_scanned == 2  # recipes + indexer
        # Invoker called once per scannable file.
        assert invoker.call_count == 2

    def test_aggregates_candidates_across_files(self, tmp_path):
        _project(tmp_path)
        invoker = MagicMock(side_effect=[
            (
                '{"candidates": [{"function_name": "create_recipe",'
                ' "line_start": 4, "line_end": 6, "why": "tax computed",'
                ' "suggested_core_module": "core/python/recipes/pricing.py"}]}'
            ),
            '{"candidates": []}',
        ])
        scanner = MisplacementScanner(
            project_root=tmp_path, llm_invoker=invoker,
        )
        report = scanner.scan()
        assert len(report.candidates) == 1
        c = report.candidates[0]
        assert "recipes.py" in c.file_path
        assert c.function_name == "create_recipe"

    def test_invoker_exception_skips_file_doesnt_halt(self, tmp_path):
        _project(tmp_path)
        invoker = MagicMock(side_effect=[
            RuntimeError("api down"),
            '{"candidates": []}',
        ])
        scanner = MisplacementScanner(
            project_root=tmp_path, llm_invoker=invoker,
        )
        report = scanner.scan()
        # Both files were attempted.
        assert invoker.call_count == 2
        # Neither produced candidates (one failed, one returned empty).
        assert report.candidates == []
        assert report.files_scanned == 2

    def test_truncates_huge_files_in_prompt(self, tmp_path):
        _project(tmp_path)
        huge = (tmp_path / "backend" / "app" / "api" / "routes" / "big.py")
        huge.write_text("# " + ("x" * 50000))
        prompts = []

        def fake(_sys, user):
            prompts.append(user)
            return '{"candidates": []}'

        scanner = MisplacementScanner(
            project_root=tmp_path,
            llm_invoker=fake,
            max_file_chars=5000,
        )
        scanner.scan()
        # Locate the prompt for the huge file (alphabetical ordering
        # not guaranteed across platforms).
        huge_prompt = next(p for p in prompts if "big.py" in p)
        assert len(huge_prompt) < 8000
        assert "truncated" in huge_prompt

    def test_prompt_includes_relative_path(self, tmp_path):
        _project(tmp_path)
        seen = []

        def fake(_sys, user):
            seen.append(user)
            return '{"candidates": []}'

        MisplacementScanner(
            project_root=tmp_path, llm_invoker=fake,
        ).scan()
        # Every prompt names the file being scanned (relative path).
        assert any("recipes.py" in u for u in seen)
        assert any("indexer.py" in u for u in seen)
