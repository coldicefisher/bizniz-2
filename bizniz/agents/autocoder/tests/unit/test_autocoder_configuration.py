import pytest
from unittest.mock import MagicMock

from bizniz.agents.autocoder.autocoder import Autocoder
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace


def test_init_success(mock_client, mock_environment, mock_workspace):
    autocoder = Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        max_retries=5,
    )
    assert autocoder.max_retries == 5
    assert autocoder._client is mock_client
    assert autocoder._environment is mock_environment
    assert autocoder._workspace is mock_workspace


def test_init_invalid_client_raises(mock_environment, mock_workspace):
    with pytest.raises(ValueError, match="client"):
        Autocoder(
            client="not_a_client",
            environment=mock_environment,
            workspace=mock_workspace,
        )


def test_init_invalid_environment_raises(mock_client, mock_workspace):
    with pytest.raises(ValueError, match="environment"):
        Autocoder(
            client=mock_client,
            environment="not_an_environment",
            workspace=mock_workspace,
        )


def test_init_invalid_max_retries_raises(mock_client, mock_environment, mock_workspace):
    with pytest.raises(ValueError):
        Autocoder(
            client=mock_client,
            environment=mock_environment,
            workspace=mock_workspace,
            max_retries=0,
        )


def test_init_callbacks_stored(mock_client, mock_environment, mock_workspace):
    on_event = MagicMock()
    on_status = MagicMock()

    autocoder = Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        on_event=on_event,
        on_status_message=on_status,
    )

    assert autocoder._on_event is on_event
    assert autocoder._on_status_message is on_status


def test_init_system_prompt_seeded_in_history(autocoder):
    system_messages = [m for m in autocoder._message_history if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert "execution environment" in system_messages[0]["content"].lower()
