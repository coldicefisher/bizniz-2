import pytest
from bizniz.orchestrator.types import OrchestratorResult

PROBLEM = "Build a task management system."


def test_dispatch_runs_orchestrator(engineer, mock_orchestrator):
    analysis = engineer.analyze(PROBLEM)
    issue = analysis.issues[0]

    engineer.dispatch(issue.db_id)
    mock_orchestrator.run.assert_called_once()


def test_dispatch_passes_issue_description_as_prompt(engineer, mock_orchestrator):
    analysis = engineer.analyze(PROBLEM)
    issue = analysis.issues[0]

    engineer.dispatch(issue.db_id)
    _, kwargs = mock_orchestrator.run.call_args
    prompt = kwargs.get("prompt") or mock_orchestrator.run.call_args[0][0]
    assert issue.description in prompt


def test_dispatch_returns_orchestrator_result(engineer, mock_orchestrator):
    analysis = engineer.analyze(PROBLEM)
    issue = analysis.issues[0]

    result = engineer.dispatch(issue.db_id)
    assert isinstance(result, OrchestratorResult)


def test_dispatch_closes_issue_on_success(engineer, mock_orchestrator, tmp_path):
    from bizniz.workspace.workspace_db import WorkspaceDB
    from bizniz.workspace.base_workspace import BaseWorkspace

    ws = BaseWorkspace(root=tmp_path)
    from bizniz.engineer.auto_engineer import AutoEngineer

    real_engineer = AutoEngineer(
        client=engineer._client,
        environment=engineer._environment,
        workspace=ws,
        orchestrator_factory=lambda: mock_orchestrator,
        max_retries=3,
    )

    analysis = real_engineer.analyze(PROBLEM)
    issue = analysis.issues[0]
    real_engineer.dispatch(issue.db_id)

    db = WorkspaceDB(ws)
    row = db.get_issue(issue.db_id)
    db.close()
    assert row["status"] == "closed"


def test_dispatch_raises_for_missing_issue(engineer):
    with pytest.raises(ValueError, match="not found"):
        engineer.dispatch(99999)


def test_dispatch_resets_status_on_failure(mock_client, mock_environment, tmp_path):
    from unittest.mock import MagicMock
    from bizniz.workspace.base_workspace import BaseWorkspace
    from bizniz.workspace.workspace_db import WorkspaceDB
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
    from bizniz.engineer.auto_engineer import AutoEngineer
    import json

    ws = BaseWorkspace(root=tmp_path)

    failing_orc = MagicMock(spec=CodingOrchestrator)
    failing_orc.run.return_value = OrchestratorResult(success=False, iterations=5)

    valid_response = {
        "business_requirements": ["Req"],
        "use_cases": [{"title": "UC", "description": "Desc"}],
        "functional_requirements": ["FR"],
        "nonfunctional_requirements": ["NFR"],
        "issues": [
            {
                "title": "Do thing",
                "description": "Do the thing.",
                "code_file": "thing.py",
                "test_file": "test_thing.py",
            }
        ],
    }
    text = json.dumps(valid_response)
    mock_client.get_text.return_value = (text, "jid", [{"role": "assistant", "content": text}])

    eng = AutoEngineer(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda: failing_orc,
        max_retries=3,
    )

    analysis = eng.analyze("Do the thing.")
    issue = analysis.issues[0]
    eng.dispatch(issue.db_id)

    db = WorkspaceDB(ws)
    row = db.get_issue(issue.db_id)
    db.close()
    assert row["status"] == "open"
