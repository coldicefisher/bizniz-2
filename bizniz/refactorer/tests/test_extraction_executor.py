"""Tests for the extraction executor (Phase F)."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from bizniz.refactorer.extraction_executor import (
    ExtractionExecutor,
    ExtractionResult,
    GitOps,
    TestRunResult,
    _parse_executor_json,
    _build_user_prompt,
)
from bizniz.refactorer.extraction_planner import ExtractionPlan


def _plan(
    hash_: str = "abc123",
    language: str = "python",
    services: List[str] = None,
    source_files: List[str] = None,
    target: str = "business/company.py",
) -> ExtractionPlan:
    return ExtractionPlan(
        duplicate_hash=hash_,
        language=language,
        services_involved=services or ["svc_a", "svc_b"],
        source_files=source_files or [
            "/proj/svc_a/foo.py", "/proj/svc_b/foo.py",
        ],
        token_count=80,
        files_count=2,
        instance_count=2,
        suggested_core_path=target,
        risk_score=0.2,
    )


class _FakeGitOps(GitOps):
    """Records what was committed / reverted."""
    def __init__(self, head: str = "rev0"):
        self._head = head
        self.commit_msgs: List[str] = []
        self.reverted_to: Optional[str] = None
    def head_rev(self) -> Optional[str]:
        return self._head
    def commit_all(self, message: str) -> Optional[str]:
        self.commit_msgs.append(message)
        new = f"rev{len(self.commit_msgs)}"
        self._head = new
        return new
    def revert_to(self, rev: str) -> None:
        self.reverted_to = rev


# ── Prompt builder ───────────────────────────────────────────────


class TestBuildUserPrompt:
    def test_includes_plan_metadata(self):
        plan = _plan(
            hash_="deadbeef",
            services=["backend", "worker"],
            target="business/company.py",
        )
        prompt = _build_user_prompt(plan)
        assert "deadbeef" in prompt
        assert "backend" in prompt and "worker" in prompt
        assert "business/company.py" in prompt
        assert "/proj/svc_a/foo.py" in prompt

    def test_planner_notes_threaded(self):
        plan = _plan()
        plan.notes = ["high-risk: 10 files", "service-local deps?"]
        prompt = _build_user_prompt(plan)
        assert "high-risk" in prompt
        assert "service-local" in prompt

    def test_empty_notes_renders_none(self):
        plan = _plan()
        plan.notes = []
        prompt = _build_user_prompt(plan)
        assert "(none)" in prompt


# ── Result parsing ───────────────────────────────────────────────


class TestParseExecutorJson:
    def test_applied_status(self):
        raw = {
            "status": "applied",
            "files_written": ["/core/business/company.py"],
            "files_modified": ["/svc_a/foo.py", "/svc_b/foo.py"],
            "summary": "moved Company logic to core",
            "notes": [],
        }
        r = _parse_executor_json(raw, _plan())
        assert r.status == "applied"
        assert len(r.files_written) == 1
        assert len(r.files_modified) == 2

    def test_unknown_status_falls_back(self):
        r = _parse_executor_json({"status": "weird"}, _plan())
        assert r.status == "failed"

    def test_non_list_files_defaulted_empty(self):
        r = _parse_executor_json(
            {"status": "applied", "files_written": "not a list"},
            _plan(),
        )
        assert r.files_written == []

    def test_non_string_files_filtered(self):
        r = _parse_executor_json(
            {
                "status": "applied",
                "files_written": ["/a.py", 42, "/b.py"],
            },
            _plan(),
        )
        assert r.files_written == ["/a.py", "/b.py"]

    def test_plan_hash_propagates(self):
        r = _parse_executor_json({}, _plan(hash_="xyz"))
        assert r.plan_hash == "xyz"


# ── Executor flow ────────────────────────────────────────────────


class TestExecuteApplied:
    def test_applied_commits_when_tests_pass(self, tmp_path):
        git = _FakeGitOps(head="pre")
        def llm(plan, prompt):
            return {
                "status": "applied",
                "files_written": ["/core/python/business/company.py"],
                "files_modified": ["/svc_a/foo.py"],
                "summary": "extracted Company",
            }
        def tests():
            return TestRunResult(passed=True, output_tail="all green")

        executor = ExtractionExecutor(
            project_root=tmp_path,
            llm_invoker=llm,
            test_runner=tests,
            git_ops=git,
        )
        result = executor.execute(_plan())
        assert result.status == "applied"
        assert result.test_passed is True
        assert result.commit_hash is not None
        # Git ops record one commit.
        assert len(git.commit_msgs) == 1
        assert "refactor" in git.commit_msgs[0].lower()
        # No revert.
        assert git.reverted_to is None


class TestExecuteFailedTests:
    def test_failed_tests_reverts_and_marks_status_reverted(self, tmp_path):
        git = _FakeGitOps(head="pre-rev")
        def llm(plan, prompt):
            return {
                "status": "applied",
                "files_written": ["/core/python/x.py"],
                "files_modified": ["/svc_a/y.py"],
                "summary": "extracted",
            }
        def tests():
            return TestRunResult(
                passed=False,
                output_tail="FAILED tests/test_x.py::test_y",
            )

        executor = ExtractionExecutor(
            project_root=tmp_path,
            llm_invoker=llm, test_runner=tests, git_ops=git,
        )
        result = executor.execute(_plan())
        assert result.status == "reverted"
        assert result.test_passed is False
        assert "FAILED" in result.test_output_tail
        # Revert happened, no commit recorded.
        assert git.reverted_to == "pre-rev"
        assert git.commit_msgs == []


class TestExecuteNoChanges:
    def test_no_changes_short_circuits_no_tests_no_commit(self, tmp_path):
        git = _FakeGitOps()
        called: List[str] = []
        def tests():
            called.append("ran")
            return TestRunResult(passed=True)

        def llm(plan, prompt):
            return {
                "status": "no_changes",
                "summary": "service-local DB session — can't extract",
            }
        executor = ExtractionExecutor(
            project_root=tmp_path,
            llm_invoker=llm, test_runner=tests, git_ops=git,
        )
        result = executor.execute(_plan())
        assert result.status == "no_changes"
        # Tests NOT run (no edits made).
        assert called == []
        # No commit.
        assert git.commit_msgs == []


class TestExecuteLLMFailure:
    def test_llm_none_marks_failed(self, tmp_path):
        executor = ExtractionExecutor(
            project_root=tmp_path,
            llm_invoker=lambda plan, prompt: None,
            test_runner=lambda: TestRunResult(passed=True),
            git_ops=_FakeGitOps(),
        )
        result = executor.execute(_plan())
        assert result.status == "failed"
        assert "no parseable JSON" in result.summary


class TestExecuteStatusLogging:
    def test_on_status_called(self, tmp_path):
        statuses: List[str] = []
        def llm(plan, prompt):
            return {"status": "applied", "summary": "ok"}
        executor = ExtractionExecutor(
            project_root=tmp_path,
            llm_invoker=llm,
            test_runner=lambda: TestRunResult(passed=True),
            git_ops=_FakeGitOps(),
            on_status=lambda m: statuses.append(m),
        )
        executor.execute(_plan(hash_="abc"))
        joined = " ".join(statuses)
        assert "abc" in joined
        assert "dispatching" in joined.lower()
        assert "running tests" in joined.lower() or "committed" in joined.lower()

    def test_buggy_on_status_does_not_crash(self, tmp_path):
        def boom(_):
            raise RuntimeError("logger broke")
        executor = ExtractionExecutor(
            project_root=tmp_path,
            llm_invoker=lambda plan, prompt: {"status": "applied"},
            test_runner=lambda: TestRunResult(passed=True),
            git_ops=_FakeGitOps(),
            on_status=boom,
        )
        result = executor.execute(_plan())
        assert result.status == "applied"
