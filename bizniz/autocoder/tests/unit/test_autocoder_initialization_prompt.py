from bizniz.autocoder.autocoder import Autocoder


def test_process_system_prompt_is_string(autocoder):
    prompt = autocoder._process_system_prompt
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_process_system_prompt_contains_environment_description(autocoder):
    prompt = autocoder._process_system_prompt.lower()
    assert "execution environment" in prompt


def test_process_system_prompt_contains_response_format_instructions(autocoder):
    prompt = autocoder._process_system_prompt
    assert "call_spec" in prompt
    assert "code" in prompt


def test_process_system_prompt_reflects_environment_describe(mock_client, mock_workspace):
    from unittest.mock import MagicMock
    from bizniz.environment.base_environment import BaseExecutionEnvironment

    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "UNIQUE_ENV_DESCRIPTION_XYZ"
    env.execute.return_value = None

    autocoder = Autocoder(
        client=mock_client,
        environment=env,
        workspace=mock_workspace,
    )

    assert "UNIQUE_ENV_DESCRIPTION_XYZ" in autocoder._process_system_prompt
