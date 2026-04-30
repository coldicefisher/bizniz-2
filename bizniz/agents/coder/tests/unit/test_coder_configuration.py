import pytest
from unittest.mock import MagicMock

from bizniz.agents.coder.coder import Coder
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace


def test_init_success(mock_client, mock_environment, mock_workspace):
    coder = Coder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=5,
    )
    assert coder.max_retries == 5
    assert coder._client is mock_client
    assert coder._environment is mock_environment
    assert coder._workspace is mock_workspace


def test_init_invalid_client_raises(mock_environment, mock_workspace):
    with pytest.raises(ValueError, match="client"):
        Coder(
            client="not_a_client",
            environment=mock_environment,
            workspace=mock_workspace,
        )


def test_init_invalid_environment_raises(mock_client, mock_workspace):
    with pytest.raises(ValueError, match="environment"):
        Coder(
            client=mock_client,
            environment="not_an_environment",
            workspace=mock_workspace,
        )


def test_init_invalid_max_retries_raises(mock_client, mock_environment, mock_workspace):
    with pytest.raises(ValueError):
        Coder(
            client=mock_client,
            environment=mock_environment,
            workspace=mock_workspace,
            max_retries=0,
        )


def test_init_callbacks_stored(mock_client, mock_environment, mock_workspace):
    on_event = MagicMock()
    on_status = MagicMock()

    coder = Coder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        on_event=on_event,
        on_status_message=on_status,
    )

    assert coder._on_event is on_event
    assert coder._on_status_message is on_status


def test_init_system_prompt_seeded_in_history(coder):
    system_messages = [m for m in coder._message_history if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert "execution environment" in system_messages[0]["content"].lower()
