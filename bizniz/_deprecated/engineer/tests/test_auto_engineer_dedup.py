"""
Tests for multi-file issue creation in Engineer.
"""
import json
import pytest
from unittest.mock import MagicMock
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.engineer.engineer import Engineer
from bizniz.engineer.tests.conftest import VALID_PLAN_RESPONSE, make_ai_response


MULTI_FILE_RESPONSE = {
    "business_requirements": ["Req"],
    "use_cases": [{"title": "UC", "description": "Desc"}],
    "functional_requirements": ["FR"],
    "nonfunctional_requirements": ["NFR"],
    "issues": [
        {
            "title": "Issue A",
            "description": "Do A.",
            "target_files": [
                {"filepath": "pkg/module_a.py", "action": "create"},
                {"filepath": "pkg/__init__.py", "action": "modify"},
            ],
            "test_files": ["tests/test_module_a.py"],
            "depends_on": [],
        },
        {
            "title": "Issue B",
            "description": "Do B.",
            "target_files": [
                {"filepath": "pkg/module_b.py", "action": "create"},
            ],
            "test_files": ["tests/test_module_b.py"],
            "depends_on": ["Issue A"],
        },
    ],
}


def test_multi_file_issues_persisted(mock_environment, tmp_path):
    ws = BaseWorkspace(root=tmp_path)

    mock_client = MagicMock(spec=BaseAIClient)
    mock_client.get_text.side_effect = [
        make_ai_response(MULTI_FILE_RESPONSE),  # analysis
        make_ai_response(VALID_PLAN_RESPONSE),   # plan
        make_ai_response(MULTI_FILE_RESPONSE),  # refined analysis
    ]

    eng = Engineer(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
        max_retries=3,
    )

    analysis = eng.analyze("Do something.")

    assert len(analysis.issues) == 2

    issue_a = analysis.issues[0]
    assert issue_a.title == "Issue A"
    assert len(issue_a.target_files) == 2
    assert issue_a.target_files[0].filepath == "pkg/module_a.py"
    assert issue_a.target_files[0].action == "create"
    assert issue_a.test_files == ["tests/test_module_a.py"]

    issue_b = analysis.issues[1]
    assert len(issue_b.target_files) == 1
    assert issue_b.test_files == ["tests/test_module_b.py"]
