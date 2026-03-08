import pytest
from unittest.mock import MagicMock, patch

from bizniz.autodebugger.autodebugger import Autodebugger
from bizniz.autodebugger.types import AutodebuggerDiagnosis
from bizniz.environment.types import ExecutionEnvironmentResult, ExecutionEnvironmentErrorDetails
from bizniz.autocoder.types import AutocoderProcessResult, FileChange
from bizniz.autotester.types import AutotesterResult, GeneratedTestFile
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator


PROMPT = "Write an add function."
CODE_V1 = "def add(a, b): return a + b\n"
CODE_V2 = "def add(a, b): return a + b  # fixed\n"
TESTS = "def test_add():\n    assert add(1, 2) == 3\n"
TESTS_V2 = "from add import add\ndef test_add():\n    assert add(1, 2) == 3\n"

FAILURE_RESULT = ExecutionEnvironmentResult(
    success=False,
    error=ExecutionEnvironmentErrorDetails(type="AssertionError", message="fail"),
)
SUCCESS_RESULT = ExecutionEnvironmentResult(success=True)


def test_autodebugger_diagnoses_code_fix(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    """When autodebugger says fix_target=code, orchestrator repairs code."""
    mock_debugger = MagicMock(spec=Autodebugger)
    mock_debugger.diagnose.return_value = AutodebuggerDiagnosis(
        diagnosis="add function returns wrong value",
        fix_target="code",
        relevant_files={},
        suggested_approach="Fix the return statement",
    )

    mock_autocoder.repair.return_value = AutocoderProcessResult(changes=[FileChange(filepath="add.py", code=CODE_V2, action="modify")])

    # Fail once, then succeed after repair
    mock_test_env.execute.side_effect = [FAILURE_RESULT, SUCCESS_RESULT]

    orc = CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        autodebugger=mock_debugger,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=5,
    )

    result = orc.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    assert result.success is True
    assert mock_debugger.diagnose.call_count == 1
    assert mock_autocoder.repair.call_count == 1


def test_autodebugger_diagnoses_test_fix(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    """When autodebugger says fix_target=tests, orchestrator regenerates tests."""
    mock_debugger = MagicMock(spec=Autodebugger)
    mock_debugger.diagnose.return_value = AutodebuggerDiagnosis(
        diagnosis="Tests import from wrong module",
        fix_target="tests",
        relevant_files={"add.py": "Defines add function"},
        suggested_approach="Fix the import statement",
    )

    # After test regeneration, tests pass
    mock_test_env.execute.side_effect = [FAILURE_RESULT, SUCCESS_RESULT]

    mock_autotester.process_from_prompt.return_value = AutotesterResult(
        test_files=[GeneratedTestFile(filepath="test_add.py", tests=TESTS_V2)],
        mode="from_prompt",
        success=True,
    )

    orc = CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        autodebugger=mock_debugger,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=5,
    )

    result = orc.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    assert result.success is True
    assert mock_debugger.diagnose.call_count == 1
    # process_from_prompt called twice: initial + regeneration
    assert mock_autotester.process_from_prompt.call_count == 2
    # Code was not repaired since debugger said to fix tests
    assert mock_autocoder.repair.call_count == 0


def test_autodebugger_failure_falls_back_to_repair(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    """When autodebugger raises an exception, orchestrator falls back to code repair."""
    mock_debugger = MagicMock(spec=Autodebugger)
    mock_debugger.diagnose.side_effect = Exception("AI failed")

    mock_autocoder.repair.return_value = AutocoderProcessResult(changes=[FileChange(filepath="add.py", code=CODE_V2, action="modify")])
    mock_test_env.execute.side_effect = [FAILURE_RESULT, SUCCESS_RESULT]

    orc = CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        autodebugger=mock_debugger,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=5,
    )

    result = orc.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    assert result.success is True
    assert mock_autocoder.repair.call_count == 1
