import json
import pytest
from unittest.mock import MagicMock
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.workspace.base_workspace import BaseWorkspace

PROBLEM = "Build a task management system."

MULTI_ISSUE_RESPONSE = {
    "business_requirements": ["Users can manage tasks."],
    "use_cases": [{"title": "UC", "description": "Desc"}],
    "functional_requirements": ["FR"],
    "nonfunctional_requirements": ["NFR"],
    "issues": [
        {
            "title": "Issue one",
            "description": "Do one.",
            "code_file": "one.py",
            "test_file": "test_one.py",
        },
        {
            "title": "Issue two",
            "description": "Do two.",
            "code_file": "two.py",
            "test_file": "test_two.py",
        },
    ],
}


def test_run_returns_list_of_results(mock_client, mock_environment, tmp_path):
    from bizniz.engineer.auto_engineer import AutoEngineer
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    ws = BaseWorkspace(root=tmp_path)
    text = json.dumps(MULTI_ISSUE_RESPONSE)
    mock_client.get_text.return_value = (text, "jid", [{"role": "assistant", "content": text}])

    orc = MagicMock(spec=CodingOrchestrator)
    orc.run.return_value = OrchestratorResult(success=True, code="x", tests="y", iterations=1)

    eng = AutoEngineer(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda: orc,
        max_retries=3,
    )

    results = eng.run(PROBLEM)
    assert isinstance(results, list)
    assert len(results) == 2
    assert all(isinstance(r, OrchestratorResult) for r in results)


def test_run_dispatches_each_issue(mock_client, mock_environment, tmp_path):
    from bizniz.engineer.auto_engineer import AutoEngineer
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    ws = BaseWorkspace(root=tmp_path)
    text = json.dumps(MULTI_ISSUE_RESPONSE)
    mock_client.get_text.return_value = (text, "jid", [{"role": "assistant", "content": text}])

    orchestrators = []

    def factory():
        orc = MagicMock(spec=CodingOrchestrator)
        orc.run.return_value = OrchestratorResult(success=True, code="x", tests="y", iterations=1)
        orchestrators.append(orc)
        return orc

    eng = AutoEngineer(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=factory,
        max_retries=3,
    )

    eng.run(PROBLEM)
    # Each issue gets its own orchestrator
    assert len(orchestrators) == 2
    for orc in orchestrators:
        orc.run.assert_called_once()
