"""
Functional tests: verify each supported model can produce a basic response.

Run with:
    pytest bizniz/clients/chatgpt/tests/functional/ -m functional -v
"""
import pytest

from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat4GPTClient
from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat


HELLO_SCHEMA = {
    "name": "hello_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "greeting": {
                "type": "string",
                "description": "A short greeting message.",
            },
        },
        "required": ["greeting"],
        "additionalProperties": False,
    },
}


def _make_client(api_key: str, model: str) -> OpenAIChat4GPTClient:
    config = ChatGPTClientConfig(default_model=model)
    return OpenAIChat4GPTClient(config=config, api_key=api_key)


# ── Hello world (plain text via JSON schema) ─────────────────────────────────

@pytest.mark.functional
def test_hello_gpt4o_mini(api_key):
    client = _make_client(api_key, "gpt-4o-mini")
    text, job_id, messages = client.get_text(
        messages=[Message(role="user", content="Say hello in one sentence.")],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=HELLO_SCHEMA,
    )
    assert text and len(text.strip()) > 0
    assert "greeting" in text


@pytest.mark.functional
def test_hello_gpt4o(api_key):
    client = _make_client(api_key, "gpt-4o")
    text, job_id, messages = client.get_text(
        messages=[Message(role="user", content="Say hello in one sentence.")],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=HELLO_SCHEMA,
    )
    assert text and len(text.strip()) > 0
    assert "greeting" in text


@pytest.mark.functional
def test_hello_gpt5(api_key):
    client = _make_client(api_key, "gpt-5")
    text, job_id, messages = client.get_text(
        messages=[Message(role="user", content="Say hello in one sentence.")],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=HELLO_SCHEMA,
    )
    assert text and len(text.strip()) > 0
    assert "greeting" in text
