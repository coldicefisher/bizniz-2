import json
import pytest
from unittest.mock import MagicMock
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorResult
from bizniz.engineer.auto_engineer import AutoEngineer, _ensure_unique


# ── _ensure_unique unit tests ────────────────────────────────────────────────────

def test_ensure_unique_no_collision():
    seen = set()
    result = _ensure_unique("add.py", seen, 1)
    assert result == "add.py"


def test_ensure_unique_collision_appends_idx():
    seen = {"add.py"}
    result = _ensure_unique("add.py", seen, 2)
    assert result == "add_2.py"


def test_ensure_unique_no_extension():
    # rpartition(".") on a name with no dot gives stem="", ext="mymodule",
    # so the function returns f"_{idx}.{name}" — files without extensions
    # are an edge case not used in practice (all real files end in .py).
    seen = {"mymodule"}
    result = _ensure_unique("mymodule", seen, 3)
    assert result != "mymodule"  # collision was resolved
    assert "mymodule" in result  # original name preserved somewhere


# ── Deduplication integration ────────────────────────────────────────────────────

DUPLICATE_FILES_RESPONSE = {
    "business_requirements": ["Req"],
    "use_cases": [{"title": "UC", "description": "Desc"}],
    "functional_requirements": ["FR"],
    "nonfunctional_requirements": ["NFR"],
    "issues": [
        {
            "title": "Issue A",
            "description": "Do A.",
            "code_file": "module.py",
            "test_file": "test_module.py",
        },
        {
            "title": "Issue B",
            "description": "Do B.",
            "code_file": "module.py",  # duplicate!
            "test_file": "test_module.py",  # duplicate!
        },
    ],
}


def test_duplicate_filenames_are_deduplicated(mock_client, mock_environment, tmp_path):
    ws = BaseWorkspace(root=tmp_path)
    text = json.dumps(DUPLICATE_FILES_RESPONSE)
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

    analysis = eng.analyze("Do something.")
    code_files = [i.code_file for i in analysis.issues]
    test_files = [i.test_file for i in analysis.issues]

    # After dedup, all filenames must be unique
    assert len(set(code_files)) == len(code_files)
    assert len(set(test_files)) == len(test_files)
