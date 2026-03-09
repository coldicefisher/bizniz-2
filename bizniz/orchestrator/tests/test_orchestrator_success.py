from bizniz.orchestrator.types import OrchestratorResult

PROMPT = "Write an add function."


def test_success_on_first_iteration(orchestrator, mock_autocoder, mock_autotester, mock_test_env):
    result = orchestrator.run(
        prompt=PROMPT,
        code_filename="add.py",
        test_filename="test_add.py",
    )

    assert isinstance(result, OrchestratorResult)
    assert result.success is True
    assert result.iterations == 1


def test_autocoder_process_called_with_prompt(orchestrator, mock_autocoder):
    orchestrator.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    mock_autocoder.generate_only.assert_called_once()
    call_kwargs = mock_autocoder.generate_only.call_args[1]
    # In TDD mode, prompt includes test context
    assert PROMPT in call_kwargs["prompt"]
    assert call_kwargs["filename"] == "add.py"


def test_autotester_process_from_prompt_called(orchestrator, mock_autotester):
    orchestrator.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    mock_autotester.process_from_prompt.assert_called_once_with(
        prompt=PROMPT,
        output_path="test_add.py",
        code_filename="add.py",
    )


def test_test_environment_execute_called(orchestrator, mock_test_env):
    orchestrator.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    mock_test_env.execute.assert_called()


def test_result_carries_code_and_tests(orchestrator):
    result = orchestrator.run(prompt=PROMPT, code_filename="add.py", test_filename="test_add.py")
    assert result.changes is not None
    assert result.test_files is not None
