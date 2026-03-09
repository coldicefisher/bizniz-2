import json
import pytest
from unittest.mock import MagicMock
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace.workspace_db import WorkspaceDB
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.engineer.tests.conftest import (
    VALID_ANALYSIS_RESPONSE,
    VALID_PLAN_RESPONSE,
    make_ai_response,
)

PROBLEM = "Build a task management system."


def test_dispatch_runs_orchestrator(engineer, mock_orchestrator):
    analysis = engineer.analyze(PROBLEM)
    issue = analysis.issues[0]

    engineer.dispatch(issue.db_id)
    mock_orchestrator.run_multi.assert_called_once()


def test_dispatch_passes_issue_description_as_prompt(engineer, mock_orchestrator):
    analysis = engineer.analyze(PROBLEM)
    issue = analysis.issues[0]

    engineer.dispatch(issue.db_id)
    _, kwargs = mock_orchestrator.run_multi.call_args
    prompt = kwargs.get("prompt") or mock_orchestrator.run_multi.call_args[0][0]
    assert issue.description in prompt


def test_dispatch_returns_orchestrator_result(engineer, mock_orchestrator):
    analysis = engineer.analyze(PROBLEM)
    issue = analysis.issues[0]

    result = engineer.dispatch(issue.db_id)
    assert isinstance(result, OrchestratorResult)


def test_dispatch_closes_issue_on_success(mock_client, mock_environment, tmp_path):
    ws = BaseWorkspace(root=tmp_path)

    orc = MagicMock(spec=CodingOrchestrator)
    orc.run_multi.return_value = OrchestratorResult(
        success=True,
        changes=[FileChange(filepath="task_manager/storage.py", code="pass", action="create")],
        test_files=[GeneratedTestFile(filepath="tests/test_storage.py", tests="pass")],
        iterations=1,
    )

    eng = AutoEngineer(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda **kwargs: orc,
        max_retries=3,
    )

    analysis = eng.analyze(PROBLEM)
    issue = analysis.issues[0]
    eng.dispatch(issue.db_id)

    db = WorkspaceDB(ws)
    row = db.get_issue(issue.db_id)
    db.close()
    assert row["status"] == "closed"


def test_dispatch_raises_for_missing_issue(engineer):
    with pytest.raises(ValueError, match="not found"):
        engineer.dispatch(99999)


def test_dispatch_resets_status_on_failure(mock_environment, tmp_path):
    ws = BaseWorkspace(root=tmp_path)

    failing_orc = MagicMock(spec=CodingOrchestrator)
    failing_orc.run_multi.return_value = OrchestratorResult(success=False, changes=[], test_files=[], iterations=5)

    from bizniz.engineer.tests.conftest import _make_multi_response_client
    client = _make_multi_response_client()

    eng = AutoEngineer(
        client=client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda **kwargs: failing_orc,
        max_retries=3,
    )

    analysis = eng.analyze("Do the thing.")
    issue = analysis.issues[0]
    eng.dispatch(issue.db_id)

    db = WorkspaceDB(ws)
    row = db.get_issue(issue.db_id)
    db.close()
    assert row["status"] == "open"
