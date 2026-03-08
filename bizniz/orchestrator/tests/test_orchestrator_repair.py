import pytest
from unittest.mock import MagicMock
from bizniz.environment.types import ExecutionEnvironmentResult, ExecutionEnvironmentErrorDetails
from bizniz.autocoder.types import AutocoderProcessResult
from bizniz.orchestrator.types import OrchestratorResult, OrchestratorMaxIterationsError

PROMPT = "Write an add function."

FAILURE_RESULT = ExecutionEnvironmentResult(
    success=False,
    error=ExecutionEnvironmentErrorDetails(type="AssertionError", message="test failed"),
    stdout="FAILED test_add.py::test_add",
)


def test_repair_called_after_test_failure(orchestrator, mock_autocoder, mock_test_env):
    # Fail once, then pass
    mock_test_env.execute.side_effect = [FAILURE_RESULT, ExecutionEnvironmentResult(success=True)]
    # Return different code on repair to avoid stale detection
    mock_autocoder.repair.return_value = AutocoderProcessResult(code="def add(a,b): return a+b\n")

    result = orchestrator.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")

    assert result.success is True
    mock_autocoder.repair.assert_called_once()


def test_repair_uses_failure_output(orchestrator, mock_autocoder, mock_test_env):
    mock_test_env.execute.side_effect = [FAILURE_RESULT, ExecutionEnvironmentResult(success=True)]
    mock_autocoder.repair.return_value = AutocoderProcessResult(code="def add(a,b): return a+b\n")

    orchestrator.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")

    _, kwargs = mock_autocoder.repair.call_args
    error_msg = kwargs.get("error_message") or mock_autocoder.repair.call_args[0][1]
    assert "AssertionError" in error_msg or "test failed" in error_msg


def test_max_iterations_raises(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    # Always fail; always return different code to avoid stale detection
    codes = [f"def add(a,b): return {i}\n" for i in range(20)]
    mock_autocoder.process.return_value = AutocoderProcessResult(code=codes[0])
    mock_autocoder.repair.side_effect = [AutocoderProcessResult(code=c) for c in codes[1:]]
    mock_workspace.read_file.side_effect = codes

    mock_test_env.execute.return_value = FAILURE_RESULT

    orc = CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=3,
    )

    with pytest.raises(OrchestratorMaxIterationsError):
        orc.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
