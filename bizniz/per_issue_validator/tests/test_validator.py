"""Unit tests for ``PerIssueValidator``.

Exercises the write/scan/fix-loop without touching a real LLM (the
``CoderTesterAgent`` is mocked) and against a real LocalWorkspace
backed by ``tmp_path``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.coder.types import Issue
from bizniz.coder_tester.agent import CoderTesterAgent, CoderTesterError
from bizniz.coder_tester.types import CoderTesterResult, FilledFile
from bizniz.per_issue_validator.types import Finding
from bizniz.per_issue_validator.validator import (
    PerIssueValidator,
    _render_findings_for_prompt,
)
from bizniz.workspace.local_workspace import LocalWorkspace


# ── Fixtures ────────────────────────────────────────────────────────


def _service() -> ServiceDefinition:
    return ServiceDefinition(
        name="backend",
        service_type="backend",
        framework="fastapi",
        language="python",
        workspace_name="backend",
        port=8000,
        description="API backend",
        depends_on=[],
    )


def _issue() -> Issue:
    return Issue(
        id="BE-001",
        title="Add /me endpoint",
        description="Return current user's profile.",
        service="backend",
        language="python",
        target_files=["app/me.py"],
        test_files=["tests/test_me.py"],
        success_criteria=["GET /me returns 200"],
        spec_refs=[],
        depends_on=[],
    )


def _workspace(tmp_path) -> LocalWorkspace:
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    # Seed a minimal package layout for symbol_validator.
    (ws_root / "app").mkdir()
    (ws_root / "app" / "__init__.py").write_text("")
    (ws_root / "tests").mkdir()
    (ws_root / "tests" / "__init__.py").write_text("")
    # requirements.txt so third_party resolution is sane.
    (ws_root / "requirements.txt").write_text("")
    return LocalWorkspace(ws_root)


def _clean_result() -> CoderTesterResult:
    return CoderTesterResult(
        issue_id="BE-001",
        filled_files=[
            FilledFile(
                path="app/me.py",
                content="def me():\n    return {'ok': True}\n",
                role="code",
            ),
            FilledFile(
                path="tests/test_me.py",
                content=(
                    "from app.me import me\n"
                    "def test_me_happy():\n"
                    "    assert me() == {'ok': True}\n"
                ),
                role="test",
            ),
        ],
    )


def _broken_imports_result() -> CoderTesterResult:
    """Code that imports a module that doesn't exist."""
    return CoderTesterResult(
        issue_id="BE-001",
        filled_files=[
            FilledFile(
                path="app/me.py",
                content=(
                    "from nonexistent_pkg_for_test_only import thing\n"
                    "def me():\n    return thing()\n"
                ),
                role="code",
            ),
            FilledFile(
                path="tests/test_me.py",
                content="def test_me(): assert True\n",
                role="test",
            ),
        ],
    )


def _syntax_error_result() -> CoderTesterResult:
    return CoderTesterResult(
        issue_id="BE-001",
        filled_files=[
            FilledFile(
                path="app/me.py",
                content="def me(:  # syntax error\n    return 1\n",
                role="code",
            ),
            FilledFile(
                path="tests/test_me.py",
                content="def test_me(): assert True\n",
                role="test",
            ),
        ],
    )


# ── Clean first pass ────────────────────────────────────────────────


class TestCleanFirstPass:
    def test_no_findings_returns_clean(self, tmp_path):
        agent = MagicMock(spec=CoderTesterAgent)
        v = PerIssueValidator(
            agent=agent,
            workspace=_workspace(tmp_path),
            run_pytest_collect=False,
        )
        result = v.validate(
            issue=_issue(),
            initial_result=_clean_result(),
            service=_service(),
            capabilities=[],
            seeded_files=[],
        )
        assert result.clean is True
        assert result.debug_iterations == 0
        assert set(result.files_written) == {"app/me.py", "tests/test_me.py"}
        # Agent never called for a fix pass.
        agent.code_issue.assert_not_called()


# ── Symbol validator finds defects → fix loop ──────────────────────


class TestSymbolValidatorFindings:
    def test_broken_imports_trigger_fix_pass(self, tmp_path):
        agent = MagicMock(spec=CoderTesterAgent)
        # Fix pass returns clean code.
        agent.code_issue.return_value = _clean_result()

        v = PerIssueValidator(
            agent=agent,
            workspace=_workspace(tmp_path),
            run_pytest_collect=False,
        )
        result = v.validate(
            issue=_issue(),
            initial_result=_broken_imports_result(),
            service=_service(),
            capabilities=[],
            seeded_files=[],
        )
        assert result.clean is True
        assert result.debug_iterations == 1
        agent.code_issue.assert_called_once()

    def test_syntax_error_caught_and_fixed(self, tmp_path):
        agent = MagicMock(spec=CoderTesterAgent)
        agent.code_issue.return_value = _clean_result()

        v = PerIssueValidator(
            agent=agent,
            workspace=_workspace(tmp_path),
            run_pytest_collect=False,
        )
        result = v.validate(
            issue=_issue(),
            initial_result=_syntax_error_result(),
            service=_service(),
            capabilities=[],
            seeded_files=[],
        )
        assert result.clean is True
        # findings before fix should include an AST finding;
        # after fix they're gone.
        assert result.findings == []


# ── Stall behavior ─────────────────────────────────────────────────


class TestStall:
    def test_no_progress_halts_at_stall_threshold(self, tmp_path):
        # Agent keeps returning the same broken code → no progress.
        agent = MagicMock(spec=CoderTesterAgent)
        agent.code_issue.return_value = _broken_imports_result()

        v = PerIssueValidator(
            agent=agent,
            workspace=_workspace(tmp_path),
            run_pytest_collect=False,
            stall_threshold=2,  # easier to hit in test
        )
        result = v.validate(
            issue=_issue(),
            initial_result=_broken_imports_result(),
            service=_service(),
            capabilities=[],
            seeded_files=[],
        )
        assert result.clean is False
        assert result.halt_reason == "stall"
        # At stall_threshold=2, we need 2 consecutive non-progress
        # iters → 2 debug iters total.
        assert result.debug_iterations == 2
        assert len(result.findings) >= 1

    def test_progress_resets_stall_counter(self, tmp_path):
        """Findings drop, then stall — stall counter resets on the
        drop, so we get more iterations than stall_threshold."""
        # Iter sequence: 2 findings → 1 finding (progress, reset) →
        # 1 finding (stall 1) → 1 finding (stall 2 → halt)
        agent = MagicMock(spec=CoderTesterAgent)
        # Three responses for the fix-loop:
        agent.code_issue.side_effect = [
            # Fix attempt 1: drops from 2→1 (progress)
            CoderTesterResult(
                issue_id="BE-001",
                filled_files=[
                    FilledFile(
                        path="app/me.py",
                        content=(
                            "from also_nonexistent_pkg_test_only import x\n"
                            "def me(): return x()\n"
                        ),
                        role="code",
                    ),
                    FilledFile(
                        path="tests/test_me.py",
                        content="def test_me(): assert True\n",
                        role="test",
                    ),
                ],
            ),
            # Fix attempt 2: same → no progress (stall 1)
            CoderTesterResult(
                issue_id="BE-001",
                filled_files=[
                    FilledFile(
                        path="app/me.py",
                        content=(
                            "from also_nonexistent_pkg_test_only import x\n"
                            "def me(): return x()\n"
                        ),
                        role="code",
                    ),
                    FilledFile(
                        path="tests/test_me.py",
                        content="def test_me(): assert True\n",
                        role="test",
                    ),
                ],
            ),
            # Fix attempt 3: same → stall 2 → halt
            CoderTesterResult(
                issue_id="BE-001",
                filled_files=[
                    FilledFile(
                        path="app/me.py",
                        content=(
                            "from also_nonexistent_pkg_test_only import x\n"
                            "def me(): return x()\n"
                        ),
                        role="code",
                    ),
                    FilledFile(
                        path="tests/test_me.py",
                        content="def test_me(): assert True\n",
                        role="test",
                    ),
                ],
            ),
        ]
        # Start with 2 findings — broken import + ALSO a bad re-import.
        initial = CoderTesterResult(
            issue_id="BE-001",
            filled_files=[
                FilledFile(
                    path="app/me.py",
                    content=(
                        "from nonexistent_pkg_for_test_only import a\n"
                        "from also_nonexistent_pkg_test_only import b\n"
                        "def me(): return a() + b()\n"
                    ),
                    role="code",
                ),
                FilledFile(
                    path="tests/test_me.py",
                    content="def test_me(): assert True\n",
                    role="test",
                ),
            ],
        )

        v = PerIssueValidator(
            agent=agent,
            workspace=_workspace(tmp_path),
            run_pytest_collect=False,
            stall_threshold=2,
        )
        result = v.validate(
            issue=_issue(),
            initial_result=initial,
            service=_service(),
            capabilities=[],
            seeded_files=[],
        )
        assert result.clean is False
        # Progress on iter 1 (reset), stall on 2 and 3 → halt at 3.
        assert result.debug_iterations == 3
        assert result.halt_reason == "stall"


# ── Agent error mid-loop ──────────────────────────────────────────


class TestAgentError:
    def test_agent_error_halts_with_reason(self, tmp_path):
        agent = MagicMock(spec=CoderTesterAgent)
        agent.code_issue.side_effect = CoderTesterError("simulated")

        v = PerIssueValidator(
            agent=agent,
            workspace=_workspace(tmp_path),
            run_pytest_collect=False,
        )
        result = v.validate(
            issue=_issue(),
            initial_result=_broken_imports_result(),
            service=_service(),
            capabilities=[],
            seeded_files=[],
        )
        assert result.clean is False
        assert "agent_error" in result.halt_reason
        # Files from the broken initial pass still written.
        assert "app/me.py" in result.files_written


# ── Findings renderer ─────────────────────────────────────────────


class TestFindingsRenderer:
    def test_empty_findings_returns_empty_string(self):
        assert _render_findings_for_prompt([]) == ""

    def test_groups_by_source_and_caps_at_20(self):
        findings = [
            Finding(source="ast", message=f"e{i}") for i in range(25)
        ]
        out = _render_findings_for_prompt(findings)
        assert "ast" in out
        assert "and 5 more" in out
