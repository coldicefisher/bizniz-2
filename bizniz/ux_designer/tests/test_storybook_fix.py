"""Tests for ``StoryFixDispatcher`` — per-story fix dispatch (Phase 4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from bizniz.ux_designer.storybook_discovery import StoryEntry
from bizniz.ux_designer.storybook_eval import StoryEvalIssue, StoryEvalResult
from bizniz.ux_designer.storybook_fix import (
    StoryFixDispatcher,
    StoryFixResult,
    _build_user_prompt,
    _format_issues,
    _parse_fix_json,
)


def _entry(component_file: Path = None) -> StoryEntry:
    return StoryEntry(
        story_id="common-toast--default",
        name="Default",
        title="Common/Toast",
        component_name="ToastContainer",
        component_file=component_file or Path("/tmp/Toast.tsx"),
        stories_file=Path("/tmp/Toast.stories.tsx"),
    )


def _eval(
    issues_count: int = 2, score: int = 5,
) -> StoryEvalResult:
    issues = [
        StoryEvalIssue(
            severity="major",
            description=f"issue #{i}",
            suggested_fix=f"fix #{i}",
        )
        for i in range(issues_count)
    ]
    return StoryEvalResult(
        story_id="common-toast--default",
        name="Default",
        title="Common/Toast",
        overall_score=score,
        issues=issues,
        stop_recommendation="iterate",
    )


# ── Format helpers ───────────────────────────────────────────────


class TestFormatIssues:
    def test_renders_each_issue(self):
        block = _format_issues(_eval(issues_count=3))
        assert "[MAJOR]" in block
        assert "issue #0" in block
        assert "issue #2" in block
        assert "fix #1" in block

    def test_empty_issues_placeholder(self):
        block = _format_issues(_eval(issues_count=0))
        assert "no issues" in block.lower()

    def test_missing_suggested_fix(self):
        ev = StoryEvalResult(
            story_id="x--y", name="Y", title="X",
            issues=[
                StoryEvalIssue(
                    severity="minor",
                    description="thing",
                    suggested_fix=None,
                ),
            ],
        )
        block = _format_issues(ev)
        assert "determine the fix from context" in block


# ── Prompt builder ───────────────────────────────────────────────


class TestBuildUserPrompt:
    def test_includes_component_file_path(self):
        e = _entry(component_file=Path("/abs/path/to/Button.tsx"))
        prompt = _build_user_prompt(e, _eval(), design_lock_json=None)
        assert "/abs/path/to/Button.tsx" in prompt

    def test_missing_component_file_falls_back_to_stories(self):
        e = StoryEntry(
            story_id="x--y", name="Y", title="X",
            stories_file=Path("/tmp/X.stories.tsx"),
            component_file=None,
        )
        prompt = _build_user_prompt(e, _eval(), design_lock_json=None)
        assert "X.stories.tsx" in prompt
        assert "locate the import" in prompt

    def test_design_lock_threaded(self):
        prompt = _build_user_prompt(
            _entry(), _eval(),
            design_lock_json='{"primary": "#123"}',
        )
        assert "#123" in prompt
        assert "tokens" in prompt.lower()


# ── Parser ───────────────────────────────────────────────────────


class TestParseFixJson:
    def test_applied_status(self):
        raw = {
            "status": "applied",
            "files_written": ["/tmp/Button.tsx"],
            "summary": "tightened spacing",
        }
        r = _parse_fix_json(raw, _entry())
        assert r.status == "applied"
        assert r.files_written == ["/tmp/Button.tsx"]

    def test_unknown_status_falls_back_to_failed(self):
        r = _parse_fix_json({"status": "weird"}, _entry())
        assert r.status == "failed"

    def test_missing_files_defaults_empty(self):
        r = _parse_fix_json({"status": "applied"}, _entry())
        assert r.files_written == []

    def test_non_list_files_defaults_empty(self):
        r = _parse_fix_json(
            {"status": "applied", "files_written": "not a list"},
            _entry(),
        )
        assert r.files_written == []

    def test_non_string_files_filtered(self):
        r = _parse_fix_json(
            {"status": "applied", "files_written": ["/a.tsx", 42, "/b.tsx"]},
            _entry(),
        )
        assert r.files_written == ["/a.tsx", "/b.tsx"]

    def test_entry_metadata_propagates(self):
        r = _parse_fix_json({}, _entry())
        assert r.story_id == "common-toast--default"
        assert r.name == "Default"


# ── Dispatcher ───────────────────────────────────────────────────


class TestStoryFixDispatcher:
    def test_dispatch_applied(self, tmp_path):
        ev = _eval()
        calls: list = []
        def fake(entry, prompt, frontend_root):
            calls.append((entry.story_id, prompt))
            return {
                "status": "applied",
                "files_written": [str(tmp_path / "Toast.tsx")],
                "summary": "fixed",
            }
        dispatcher = StoryFixDispatcher(coder_invoker=fake)
        result = dispatcher.dispatch(
            _entry(component_file=tmp_path / "Toast.tsx"),
            ev,
            frontend_root=tmp_path,
        )
        assert result.status == "applied"
        assert len(result.files_written) == 1
        assert len(calls) == 1

    def test_dispatch_no_issues_skipped(self, tmp_path):
        ev = _eval(issues_count=0)
        calls: list = []
        def fake(entry, prompt, frontend_root):
            calls.append(entry.story_id)
            return {"status": "applied"}
        dispatcher = StoryFixDispatcher(coder_invoker=fake)
        result = dispatcher.dispatch(_entry(), ev, frontend_root=tmp_path)
        assert result.status == "no_changes"
        assert result.summary == "no issues to fix"
        # Coder NOT called.
        assert calls == []

    def test_coder_returns_none(self, tmp_path):
        dispatcher = StoryFixDispatcher(
            coder_invoker=lambda entry, prompt, dir: None,
        )
        result = dispatcher.dispatch(_entry(), _eval(), frontend_root=tmp_path)
        assert result.status == "failed"
        assert "no parseable JSON" in result.summary

    def test_design_lock_threaded(self, tmp_path):
        prompts: list = []
        def fake(entry, prompt, frontend_root):
            prompts.append(prompt)
            return {"status": "applied"}
        dispatcher = StoryFixDispatcher(coder_invoker=fake)
        dispatcher.dispatch(
            _entry(), _eval(),
            frontend_root=tmp_path,
            design_lock_json='{"primary": "#abc"}',
        )
        assert "#abc" in prompts[0]

    def test_status_callback_emits_progress(self, tmp_path):
        statuses: list = []
        dispatcher = StoryFixDispatcher(
            on_status=lambda m: statuses.append(m),
            coder_invoker=lambda e, p, d: {"status": "applied",
                                            "files_written": ["/x.tsx"]},
        )
        dispatcher.dispatch(_entry(), _eval(), frontend_root=tmp_path)
        joined = " ".join(statuses)
        assert "dispatching" in joined
        assert "applied" in joined

    def test_status_callback_swallows_exceptions(self, tmp_path):
        def boom(_):
            raise RuntimeError("logger broke")
        dispatcher = StoryFixDispatcher(
            on_status=boom,
            coder_invoker=lambda e, p, d: {"status": "applied"},
        )
        result = dispatcher.dispatch(_entry(), _eval(), frontend_root=tmp_path)
        assert result.status == "applied"
