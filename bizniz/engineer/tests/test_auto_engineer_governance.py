"""
Tests for the governance loop integration in AutoEngineer.dispatch().
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.engineer.types import ArchitecturePlan, GovernanceDecision
from bizniz.engineer.tests.conftest import (
    VALID_ANALYSIS_RESPONSE,
    VALID_PLAN_RESPONSE,
    make_ai_response,
)


def _setup_engineer_with_plan(tmp_path, mock_env, orchestrator_result, governance_response=None):
    """
    Create an AutoEngineer with a persisted analysis + plan,
    and configure it to return the given orchestrator_result on dispatch.
    """
    ws = BaseWorkspace(root=tmp_path)

    client = MagicMock(spec=BaseAIClient)

    # Calls: analysis, plan, refined analysis, then governance if drift
    responses = [
        make_ai_response(VALID_ANALYSIS_RESPONSE),  # analyze
        make_ai_response(VALID_PLAN_RESPONSE),       # plan
        make_ai_response(VALID_ANALYSIS_RESPONSE),   # refined
    ]
    if governance_response:
        responses.append(make_ai_response(governance_response))  # governance

    client.get_text.side_effect = responses

    orc = MagicMock(spec=CodingOrchestrator)
    orc.run_multi.return_value = orchestrator_result

    eng = AutoEngineer(
        client=client,
        environment=mock_env,
        workspace=ws,
        orchestrator_factory=lambda **kwargs: orc,
        max_retries=3,
    )

    analysis = eng.analyze("Build a task manager")
    return eng, analysis, client


@pytest.fixture
def mock_env():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test env"
    return env


class TestGovernanceLoopIntegration:

    def test_no_governance_when_no_drift(self, mock_env, tmp_path):
        """Dispatch should not call governance when there is no drift."""
        result = OrchestratorResult(
            success=True,
            changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")],
            test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
            iterations=1,
            architecture_drift_detected=False,
        )

        eng, analysis, client = _setup_engineer_with_plan(tmp_path, mock_env, result)
        issue = analysis.issues[0]
        eng.dispatch(issue.db_id)

        # Only 3 AI calls (analyze, plan, refined) — no governance call
        assert client.get_text.call_count == 3

    def test_governance_called_on_drift(self, mock_env, tmp_path):
        """Dispatch should call governance review when drift is detected."""
        result = OrchestratorResult(
            success=True,
            changes=[
                FileChange(filepath="task_manager/storage.py", code="pass", action="create"),
                FileChange(filepath="task_manager/utils.py", code="# unplanned", action="create"),
            ],
            test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
            iterations=1,
            architecture_drift_detected=True,
            drift_files=["task_manager/utils.py"],
        )

        governance_resp = {
            "decision": "approve",
            "reason": "Utility module is reasonable.",
            "plan_updates": "",
        }

        eng, analysis, client = _setup_engineer_with_plan(
            tmp_path, mock_env, result, governance_resp
        )
        issue = analysis.issues[0]
        eng.dispatch(issue.db_id)

        # 4 AI calls: analyze, plan, refined, governance
        assert client.get_text.call_count == 4

    def test_governance_approve_closes_issue(self, mock_env, tmp_path):
        """Approved drift should still close the issue."""
        result = OrchestratorResult(
            success=True,
            changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")],
            test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
            iterations=1,
            architecture_drift_detected=True,
            drift_files=["task_manager/utils.py"],
        )

        governance_resp = {
            "decision": "approve",
            "reason": "OK",
            "plan_updates": "",
        }

        eng, analysis, client = _setup_engineer_with_plan(
            tmp_path, mock_env, result, governance_resp
        )
        issue = analysis.issues[0]
        eng.dispatch(issue.db_id)

        row = eng._workspace.db.get_issue(issue.db_id)
        assert row["status"] == "closed"

    def test_governance_modify_updates_plan(self, mock_env, tmp_path):
        """Modify decision should update the architecture plan in DB."""
        result = OrchestratorResult(
            success=True,
            changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")],
            test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
            iterations=1,
            architecture_drift_detected=True,
            drift_files=["task_manager/helpers.py"],
        )

        governance_resp = {
            "decision": "modify",
            "reason": "Adding helpers namespace to plan.",
            "plan_updates": json.dumps({
                "modules": [{"filepath": "task_manager/helpers.py", "class_name": "", "namespace_path": "task_manager", "methods": [], "docstring": "Helper utilities"}],
            }),
        }

        eng, analysis, client = _setup_engineer_with_plan(
            tmp_path, mock_env, result, governance_resp
        )
        issue = analysis.issues[0]
        eng.dispatch(issue.db_id)

        # Verify the plan was updated
        plan_row = eng._workspace.db.get_architecture_plan(analysis.problem_id)
        plan_data = json.loads(plan_row["plan_json"])
        module_paths = [m.get("filepath", "") for m in plan_data.get("modules", [])]
        assert "task_manager/helpers.py" in module_paths

    def test_governance_reject_still_closes_on_success(self, mock_env, tmp_path):
        """Even rejected drift closes the issue if orchestrator succeeded."""
        result = OrchestratorResult(
            success=True,
            changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")],
            test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
            iterations=1,
            architecture_drift_detected=True,
            drift_files=["task_manager/bad.py"],
        )

        governance_resp = {
            "decision": "reject",
            "reason": "This module is unnecessary.",
            "plan_updates": "",
        }

        eng, analysis, client = _setup_engineer_with_plan(
            tmp_path, mock_env, result, governance_resp
        )
        issue = analysis.issues[0]
        eng.dispatch(issue.db_id)

        row = eng._workspace.db.get_issue(issue.db_id)
        assert row["status"] == "closed"

    def test_no_governance_when_drift_files_empty(self, mock_env, tmp_path):
        """If drift_detected is True but drift_files is empty, skip governance."""
        result = OrchestratorResult(
            success=True,
            changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")],
            test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
            iterations=1,
            architecture_drift_detected=True,
            drift_files=[],  # empty
        )

        eng, analysis, client = _setup_engineer_with_plan(tmp_path, mock_env, result)
        issue = analysis.issues[0]
        eng.dispatch(issue.db_id)

        # Only 3 AI calls — governance not triggered
        assert client.get_text.call_count == 3
