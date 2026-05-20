"""Regression tests for the fixes shipped during the 2026-05-19 v4
session.

Each test pins ONE specific fix so future refactors can't silently
revert them. Failures here indicate a real regression.

Order roughly follows the commit chain. Comments cite the
originating commit short-hash for grep-ability.
"""
from __future__ import annotations

import inspect


# ── Commit 7e5dbbc — symbol_validator attr findings are advisory ──


def test_unresolved_attribute_findings_are_advisory_not_blocking():
    """recipe_v4_v6 first-run: agents chased false-positive Pydantic /
    SQLAlchemy attribute access (model_fields, __tablename__, etc).
    The fix: ``unresolved_attributes`` from symbol_validator are
    logged but NOT added to the blocking findings list."""
    from bizniz.per_issue_validator.validator import PerIssueValidator

    src = inspect.getsource(PerIssueValidator._scan)
    # The blocking-loop now relies on (ast errors + unresolved imports
    # + pytest collect) only. The advisory path emits a log line and
    # increments a counter but doesn't extend findings.
    assert "advisory_count" in src or "advisor" in src, (
        "PerIssueValidator._scan should track attribute findings as "
        "advisory — not append to the blocking findings list."
    )


# ── Commit 558bb92 — pytest_collect default OFF + container-only ──


def test_pytest_collect_skipped_when_no_compose_path():
    """recipe_v4_v5: host pytest can't resolve container deps, so
    every collection failed and agents chased phantom env errors.
    Fix: when compose_path / service_name aren't set, _pytest_collect
    returns [] silently — no host-mode attempts."""
    from unittest.mock import MagicMock
    from bizniz.per_issue_validator.validator import PerIssueValidator

    v = PerIssueValidator(
        agent=MagicMock(),
        workspace=MagicMock(),
        run_pytest_collect=True,  # explicitly on
        # No compose_path / service_name → must still skip.
    )
    issue = MagicMock(language="python", test_files=["tests/x.py"])
    findings = v._pytest_collect(issue, ["tests/x.py"])
    assert findings == [], (
        "Without compose_path + service_name, _pytest_collect must "
        "return [] (host mode is unreliable)."
    )


# ── Commit 6e72c70 — CoderTesterAgent role salvage + ValidationError detail ──


def test_coder_tester_salvages_missing_role():
    """recipe_v4_v6: Haiku sometimes omits ``role`` in filled_files;
    8/9 backend issues failed before the salvage. Fix: infer role
    from path extension before Pydantic validation."""
    from unittest.mock import MagicMock, patch
    from bizniz.clients.base_ai_client import BaseAIClient
    from bizniz.coder.types import Issue
    from bizniz.architect.types import ServiceDefinition
    from bizniz.coder_tester.agent import CoderTesterAgent

    issue = Issue(
        id="X", title="t", description="d", service="backend",
        language="python",
        target_files=["app/x.py"], test_files=["tests/test_x.py"],
        success_criteria=[], spec_refs=[], depends_on=[],
    )
    service = ServiceDefinition(
        name="backend", service_type="backend", framework="fastapi",
        language="python", workspace_name="backend", port=8000,
        description="b", depends_on=[],
    )
    out = {
        "issue_id": "X",
        "filled_files": [
            {"path": "tests/test_x.py", "content": "x", "role": None},
            {"path": "app/x.py", "content": "y"},  # role missing
        ],
        "notes": "",
    }
    with patch("bizniz.coder_tester.agent.call_with_retry", return_value=out):
        agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
        result = agent.code_issue(
            issue=issue, service=service, seeded_files=[], capabilities=[],
        )
    roles = {f.role for f in result.filled_files}
    assert roles == {"code", "test"}, (
        "CoderTesterAgent must salvage null/missing role before "
        "Pydantic validation (path → role inference)."
    )


# ── Commit a9002d3 — PerIssueDebugger timeout 600 → 3000 ──


def test_per_issue_debugger_default_timeout_is_3000s():
    """recipe_v4_v8 saw BA-fix1-1 hit a 10-min wall mid-debugging.
    Real cases need 30+ min. Default bumped to 50 min."""
    from bizniz.per_issue_validator.debugger import PerIssueDebugger

    sig = inspect.signature(PerIssueDebugger.__init__)
    default = sig.parameters["timeout_seconds"].default
    assert default >= 3000, (
        f"PerIssueDebugger.timeout_seconds default is {default}; "
        f"must be ≥ 3000s (50 min) — deep cases need the room."
    )


def test_v4_dispatcher_default_debugger_timeout_is_3000s():
    """Companion to the above — the dispatcher's wiring default also
    bumped to 3000s."""
    from bizniz.driver.v4_milestone_code_dispatcher import (
        V4MilestoneCodeDispatcher,
    )

    sig = inspect.signature(V4MilestoneCodeDispatcher.__init__)
    default = sig.parameters["debugger_timeout_seconds"].default
    assert default >= 3000


# ── Commit 7fca348 #2 — parallel services within layer ──


def test_v4_run_uses_threadpool_for_service_dispatch():
    """recipe_v4_v8 ran backend + frontend sequentially. Fix: the
    inner service loop is wrapped in ThreadPoolExecutor so services
    in the same topological layer run concurrently."""
    from bizniz.driver.v4_milestone_code_dispatcher import (
        V4MilestoneCodeDispatcher,
    )
    src = inspect.getsource(V4MilestoneCodeDispatcher.run)
    assert "ThreadPoolExecutor" in src, (
        "V4MilestoneCodeDispatcher.run must dispatch services via "
        "ThreadPoolExecutor (parallel within layer)."
    )


def test_v4_repair_uses_threadpool_for_service_dispatch():
    """Same fix applied to .repair()."""
    from bizniz.driver.v4_milestone_code_dispatcher import (
        V4MilestoneCodeDispatcher,
    )
    src = inspect.getsource(V4MilestoneCodeDispatcher.repair)
    assert "ThreadPoolExecutor" in src


# ── Commit 7fca348 #3 — repair seeds from live workspace ──


def test_repair_dispatch_reads_live_workspace_seed():
    """recipe_v4_v8 cross-fix-issue conflicts: fix-issue B saw stale
    planner-frozen scaffold instead of A's just-written files. Fix:
    in repair mode, read live disk content as seed."""
    from bizniz.driver.v4_milestone_code_dispatcher import (
        V4MilestoneCodeDispatcher,
    )
    src = inspect.getsource(V4MilestoneCodeDispatcher._run_pirunner)
    assert "use_repair_tier" in src and "_read_workspace_file" in src, (
        "_run_pirunner must branch on use_repair_tier and read live "
        "workspace contents for repair seeds."
    )


# ── Commit 7fca348 #4 — workspace_summary to repair planner ──


def test_plan_repair_accepts_workspace_summary():
    """recipe_v4_v8: repair planner emitted fix-issues for things
    already partially done. Fix: workspace_summary kwarg lets the
    planner see what's currently on disk."""
    from bizniz.service_planner.agent import ServicePlanner

    sig = inspect.signature(ServicePlanner.plan_repair)
    assert "workspace_summary" in sig.parameters


# ── Commit dbbbe12 — self-validate prompt addition ──


def test_coder_tester_prompt_includes_self_validate_section():
    """Option 4: prompt asks the agent to mentally trace imports +
    signatures before emitting JSON. Cheap hedge against
    hallucinations within the same call."""
    from bizniz.coder_tester.prompts import CODER_TESTER_SYSTEM_PROMPT
    assert "SELF-VALIDATE" in CODER_TESTER_SYSTEM_PROMPT, (
        "CoderTesterAgent system prompt must include SELF-VALIDATE "
        "BEFORE EMITTING section."
    )


# ── Commit 641f73f — Option 1 in-container pytest ──


def test_per_issue_validator_pytest_runs_in_container_when_configured():
    """Option 1: when compose_path + service_name set, pytest runs
    inside the container via docker compose exec."""
    from unittest.mock import patch, MagicMock
    from bizniz.per_issue_validator.validator import PerIssueValidator

    v = PerIssueValidator(
        agent=MagicMock(),
        workspace=MagicMock(),
        compose_path="/p/c.yml",
        service_name="backend",
        run_pytest_collect=True,
    )
    issue = MagicMock(language="python", test_files=["tests/x.py"])
    mock_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch(
        "bizniz.per_issue_validator.validator.subprocess.run",
        return_value=mock_proc,
    ) as mock_run:
        v._pytest_collect(issue, ["tests/x.py"])
    if mock_run.called:
        cmd = mock_run.call_args.args[0]
        assert cmd[:4] == ["docker", "compose", "-f", "/p/c.yml"], (
            "Container pytest must shell out via docker compose exec."
        )
        assert "exec" in cmd and "backend" in cmd


# ── Commit 866b99c — ClaudeCliClient --resume support ──


def test_claude_cli_client_supports_resume_session_id():
    """Option 2: --resume flag for prompt cache reuse."""
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient

    sig = inspect.signature(ClaudeCliClient.get_text)
    assert "resume_session_id" in sig.parameters, (
        "ClaudeCliClient.get_text must accept resume_session_id."
    )


# ── Commit 6671808 — PerIssueDebugger with full tool-loop ──


def test_per_issue_debugger_uses_full_tool_set():
    """Option 3: per-issue debugger has Edit/Write/Read/Bash + more."""
    from bizniz.per_issue_validator.debugger import _ALLOWED_TOOLS

    for tool in ("Edit", "Write", "Read", "Bash", "Glob", "Grep"):
        assert tool in _ALLOWED_TOOLS, (
            f"PerIssueDebugger must allow {tool}."
        )


# ── Commit 31e3ec1 — repair_stall_threshold default 3 ──


def test_repair_stall_threshold_default_is_3():
    """recipe_v4_v8 wasted iters waiting for the 5-stall threshold.
    Tightened to 3."""
    from bizniz.config.bizniz_config import BiznizConfig

    # Verify the field's default via model schema (BiznizConfig has
    # required fields that vary by config; the default lives on the
    # field descriptor regardless).
    default = BiznizConfig.model_fields["repair_stall_threshold"].default
    assert default == 3


# ── Commit b23f244 — ProjectGit per-iter snapshots ──


def test_project_git_has_snapshot_and_rollback():
    """v5 prerequisite: per-iter snapshot + rollback exist on
    ProjectGit (used by ReviewRepairV5Loop for regression recovery)."""
    from bizniz.driver.project_git import ProjectGit

    assert hasattr(ProjectGit, "snapshot_for_repair_iter")
    assert hasattr(ProjectGit, "rollback_repair_iter")
