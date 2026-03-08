import pytest
from bizniz.environment.types import ExecutionEnvironmentResult, ExecutionEnvironmentErrorDetails
from bizniz.autocoder.types import AutocoderProcessResult
from bizniz.orchestrator.types import OrchestratorStalledError

PROMPT = "Write an add function."

FAILURE_RESULT = ExecutionEnvironmentResult(
    success=False,
    error=ExecutionEnvironmentErrorDetails(type="AssertionError", message="fail"),
)

SAME_CODE = "def add(a, b): return a + b\n"


def test_stale_loop_raises(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    # Always return the exact same code
    mock_autocoder.generate.return_value = AutocoderProcessResult(code=SAME_CODE)
    mock_autocoder.repair.return_value = AutocoderProcessResult(code=SAME_CODE)
    mock_workspace.read_file.return_value = SAME_CODE
    mock_test_env.execute.return_value = FAILURE_RESULT

    orc = CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=10,
    )

    with pytest.raises(OrchestratorStalledError):
        orc.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")


def test_no_stale_when_code_changes(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

    codes = [f"def add(a, b): return {i}\n" for i in range(10)]
    mock_autocoder.generate.return_value = AutocoderProcessResult(code=codes[0])
    mock_autocoder.repair.side_effect = [AutocoderProcessResult(code=c) for c in codes[1:]]
    mock_workspace.read_file.side_effect = codes

    # Fail twice, then succeed
    mock_test_env.execute.side_effect = [
        FAILURE_RESULT,
        FAILURE_RESULT,
        ExecutionEnvironmentResult(success=True),
    ]

    orc = CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=10,
    )

    result = orc.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    assert result.success is True
