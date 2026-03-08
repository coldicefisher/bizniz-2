import json
import pytest
from unittest.mock import MagicMock
from bizniz.autocoder.types import FileChange
from bizniz.autotester.types import GeneratedTestFile
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.engineer.tests.conftest import (
    VALID_ANALYSIS_RESPONSE,
    VALID_PLAN_RESPONSE,
    make_ai_response,
)

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
            "target_files": [{"filepath": "task_manager/one.py", "action": "create"}],
            "test_files": ["tests/test_one.py"],
            "depends_on": [],
        },
        {
            "title": "Issue two",
            "description": "Do two.",
            "target_files": [{"filepath": "task_manager/two.py", "action": "create"}],
            "test_files": ["tests/test_two.py"],
            "depends_on": ["Issue one"],
        },
    ],
}


def test_run_returns_list_of_results(mock_environment, tmp_path):
    from bizniz.clients.base_ai_client import BaseAIClient
    from bizniz.engineer.auto_engineer import AutoEngineer
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    ws = BaseWorkspace(root=tmp_path)

    client = MagicMock(spec=BaseAIClient)
    client.get_text.side_effect = [
        make_ai_response(MULTI_ISSUE_RESPONSE),  # analysis
        make_ai_response(VALID_PLAN_RESPONSE),     # plan
        make_ai_response(MULTI_ISSUE_RESPONSE),   # refined analysis
    ]

    orc = MagicMock(spec=CodingOrchestrator)
    orc.run_multi.return_value = OrchestratorResult(success=True, changes=[FileChange(filepath="out.py", code="x", action="create")], test_files=[GeneratedTestFile(filepath="test_out.py", tests="y")], iterations=1)

    eng = AutoEngineer(
        client=client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda: orc,
        max_retries=3,
    )

    results = eng.run(PROBLEM)
    assert isinstance(results, list)
    assert len(results) == 2
    assert all(isinstance(r, OrchestratorResult) for r in results)


def test_run_dispatches_each_issue(mock_environment, tmp_path):
    from bizniz.clients.base_ai_client import BaseAIClient
    from bizniz.engineer.auto_engineer import AutoEngineer
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    ws = BaseWorkspace(root=tmp_path)

    client = MagicMock(spec=BaseAIClient)
    client.get_text.side_effect = [
        make_ai_response(MULTI_ISSUE_RESPONSE),
        make_ai_response(VALID_PLAN_RESPONSE),
        make_ai_response(MULTI_ISSUE_RESPONSE),
    ]

    orchestrators = []

    def factory():
        orc = MagicMock(spec=CodingOrchestrator)
        orc.run_multi.return_value = OrchestratorResult(success=True, changes=[FileChange(filepath="out.py", code="x", action="create")], test_files=[GeneratedTestFile(filepath="test_out.py", tests="y")], iterations=1)
        orchestrators.append(orc)
        return orc

    eng = AutoEngineer(
        client=client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=factory,
        max_retries=3,
    )

    eng.run(PROBLEM)
    assert len(orchestrators) == 2
    for orc in orchestrators:
        orc.run_multi.assert_called_once()
