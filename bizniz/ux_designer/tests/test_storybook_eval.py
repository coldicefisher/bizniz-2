"""Tests for ``StoryEvaluator`` — per-story vision evaluation (Phase 3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from bizniz.ux_designer.storybook_capture import StoryCaptureResult
from bizniz.ux_designer.storybook_discovery import StoryEntry
from bizniz.ux_designer.storybook_eval import (
    StoryEvalIssue,
    StoryEvalResult,
    StoryEvaluator,
    _build_user_prompt,
    _parse_eval_json,
)


def _entry() -> StoryEntry:
    return StoryEntry(
        story_id="common-toast--default",
        name="Default",
        title="Common/Toast",
        component_name="ToastContainer",
        component_file=Path("/tmp/Toast.tsx"),
        stories_file=Path("/tmp/Toast.stories.tsx"),
    )


def _capture(tmp_path: Path, success: bool = True) -> StoryCaptureResult:
    png = tmp_path / "common-toast--default.png"
    if success:
        png.write_bytes(b"\x89PNG fake")
    return StoryCaptureResult(
        story_id="common-toast--default",
        name="Default",
        title="Common/Toast",
        screenshot_path=png if success else None,
        success=success,
        error=None if success else "capture failed",
    )


# ── Prompt builder ───────────────────────────────────────────────


class TestBuildUserPrompt:
    def test_includes_story_metadata(self):
        prompt = _build_user_prompt(_entry(), iteration=1, design_lock_json=None)
        assert "Common/Toast" in prompt
        assert "Default" in prompt
        assert "common-toast--default" in prompt
        assert "ToastContainer" in prompt

    def test_includes_design_lock_when_provided(self):
        lock = '{"colors": {"primary": "#0066cc"}}'
        prompt = _build_user_prompt(_entry(), iteration=1, design_lock_json=lock)
        assert "design system" in prompt.lower()
        assert "#0066cc" in prompt

    def test_iteration_omitted_when_first(self):
        prompt = _build_user_prompt(_entry(), iteration=1, design_lock_json=None)
        assert "Iteration:" not in prompt

    def test_iteration_shown_when_second(self):
        prompt = _build_user_prompt(_entry(), iteration=2, design_lock_json=None)
        assert "Iteration:** 2" in prompt


# ── Parser ───────────────────────────────────────────────────────


class TestParseEvalJson:
    def test_full_valid_json(self):
        raw = {
            "overall_score": 8,
            "matches_design_system": True,
            "issues": [
                {"severity": "minor", "description": "tighten padding"},
            ],
            "summary": "looks good",
            "stop_recommendation": "stop",
        }
        r = _parse_eval_json(raw, _entry())
        assert r.overall_score == 8
        assert r.matches_design_system is True
        assert len(r.issues) == 1
        assert r.summary == "looks good"
        assert r.stop_recommendation == "stop"

    def test_missing_fields_default_to_zero(self):
        r = _parse_eval_json({}, _entry())
        assert r.overall_score == 0
        assert r.matches_design_system is False
        assert r.issues == []
        assert r.stop_recommendation == "stop"

    def test_score_clamped_to_range(self):
        assert _parse_eval_json({"overall_score": 99}, _entry()).overall_score == 10
        assert _parse_eval_json({"overall_score": -5}, _entry()).overall_score == 0

    def test_score_non_numeric_defaults_to_zero(self):
        r = _parse_eval_json({"overall_score": "high"}, _entry())
        assert r.overall_score == 0

    def test_stop_recommendation_clamped(self):
        # Unknown value falls back to "stop".
        r = _parse_eval_json({"stop_recommendation": "maybe"}, _entry())
        assert r.stop_recommendation == "stop"

    def test_garbage_issues_skipped_individually(self):
        raw = {
            "issues": [
                {"severity": "critical", "description": "bad colors"},
                "not a dict",  # should be skipped
                {"severity": "INVALID", "description": "x"},  # invalid enum → skipped
                {"severity": "major", "description": "spacing off"},
            ],
        }
        r = _parse_eval_json(raw, _entry())
        # Two valid issues remain.
        assert len(r.issues) == 2
        assert {i.severity for i in r.issues} == {"critical", "major"}

    def test_entry_metadata_propagates(self):
        r = _parse_eval_json({}, _entry())
        assert r.story_id == "common-toast--default"
        assert r.name == "Default"
        assert r.title == "Common/Toast"


# ── Evaluator ────────────────────────────────────────────────────


class TestStoryEvaluator:
    def test_evaluate_happy_path(self, tmp_path):
        capture = _capture(tmp_path)
        # Vision returns clean JSON.
        def fake_vision(entry, prompt, screenshot_dir):
            return {
                "overall_score": 9,
                "matches_design_system": True,
                "issues": [],
                "summary": "looks great",
                "stop_recommendation": "stop",
            }
        evaluator = StoryEvaluator(vision_invoker=fake_vision)
        result = evaluator.evaluate(capture, _entry())
        assert result.overall_score == 9
        assert result.stop_recommendation == "stop"

    def test_evaluate_iterate_recommendation(self, tmp_path):
        capture = _capture(tmp_path)
        def fake_vision(entry, prompt, screenshot_dir):
            return {
                "overall_score": 4,
                "issues": [
                    {"severity": "major", "description": "needs spacing"}
                ],
                "stop_recommendation": "iterate",
            }
        evaluator = StoryEvaluator(vision_invoker=fake_vision)
        result = evaluator.evaluate(capture, _entry())
        assert result.overall_score == 4
        assert result.stop_recommendation == "iterate"
        assert len(result.issues) == 1

    def test_no_capture_returns_zero_score(self, tmp_path):
        capture = _capture(tmp_path, success=False)
        calls: list = []
        def fake_vision(entry, prompt, screenshot_dir):
            calls.append(entry.story_id)
            return {"overall_score": 10}
        evaluator = StoryEvaluator(vision_invoker=fake_vision)
        result = evaluator.evaluate(capture, _entry())
        assert result.overall_score == 0
        assert result.summary == "no capture available"
        # Vision should NOT have been called.
        assert calls == []

    def test_vision_returns_none(self, tmp_path):
        capture = _capture(tmp_path)
        evaluator = StoryEvaluator(
            vision_invoker=lambda entry, prompt, dir: None,
        )
        result = evaluator.evaluate(capture, _entry())
        assert result.overall_score == 0
        assert "no parseable JSON" in result.summary

    def test_design_lock_threaded_to_invoker(self, tmp_path):
        capture = _capture(tmp_path)
        prompts: list = []
        def fake_vision(entry, prompt, screenshot_dir):
            prompts.append(prompt)
            return {"overall_score": 8}
        evaluator = StoryEvaluator(vision_invoker=fake_vision)
        evaluator.evaluate(
            capture, _entry(),
            design_lock_json='{"primary": "#abc"}',
        )
        assert len(prompts) == 1
        assert "#abc" in prompts[0]

    def test_iteration_threaded_to_invoker(self, tmp_path):
        capture = _capture(tmp_path)
        prompts: list = []
        def fake_vision(entry, prompt, screenshot_dir):
            prompts.append(prompt)
            return {"overall_score": 8}
        evaluator = StoryEvaluator(vision_invoker=fake_vision)
        evaluator.evaluate(capture, _entry(), iteration=3)
        assert "Iteration:** 3" in prompts[0]

    def test_status_callback_emits_progress(self, tmp_path):
        capture = _capture(tmp_path)
        statuses: list = []
        evaluator = StoryEvaluator(
            on_status=lambda m: statuses.append(m),
            vision_invoker=lambda e, p, d: {"overall_score": 7},
        )
        evaluator.evaluate(capture, _entry())
        joined = " ".join(statuses)
        assert "evaluating" in joined.lower()
        assert "score=7" in joined

    def test_buggy_status_callback_does_not_crash(self, tmp_path):
        capture = _capture(tmp_path)
        def boom(_):
            raise RuntimeError("logger broke")
        evaluator = StoryEvaluator(
            on_status=boom,
            vision_invoker=lambda e, p, d: {"overall_score": 7},
        )
        result = evaluator.evaluate(capture, _entry())
        assert result.overall_score == 7
