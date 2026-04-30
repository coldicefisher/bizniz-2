from bizniz.agents.coder.coder import Coder


def test_process_system_prompt_is_string(coder):
    prompt = coder._process_system_prompt
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_process_system_prompt_contains_environment_description(coder):
    prompt = coder._process_system_prompt.lower()
    assert "execution environment" in prompt


def test_process_system_prompt_contains_response_format_instructions(coder):
    prompt = coder._process_system_prompt
    assert "call_spec" in prompt
    assert "code" in prompt


def test_process_system_prompt_reflects_environment_describe(mock_client, mock_workspace):
    from unittest.mock import MagicMock
    from bizniz.environment.base_environment import BaseExecutionEnvironment

    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "UNIQUE_ENV_DESCRIPTION_XYZ"
    env.execute.return_value = None

    coder = Coder(
        client=mock_client,
        environment=env,
        workspace=mock_workspace,
    )

    assert "UNIQUE_ENV_DESCRIPTION_XYZ" in coder._process_system_prompt
