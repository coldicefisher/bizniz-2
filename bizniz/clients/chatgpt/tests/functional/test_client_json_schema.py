"""
Functional tests: verify each supported model produces valid structured JSON output.

Run with:
    pytest bizniz/clients/chatgpt/tests/functional/ -m functional -v
"""
import json
import pytest

from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat4GPTClient
from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat


CODE_SCHEMA = {
    "name": "code_generation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code.",
            },
            "explanation": {
                "type": "string",
                "description": "Brief explanation of what the code does.",
            },
        },
        "required": ["code", "explanation"],
        "additionalProperties": False,
    },
}

PROMPT = "Write a Python function called `add(a, b)` that returns the sum of two numbers."


def _make_client(api_key: str, model: str) -> OpenAIChat4GPTClient:
    config = ChatGPTClientConfig(default_model=model)
    return OpenAIChat4GPTClient(config=config, api_key=api_key)


def _validate_response(text: str):
    """Parse the response and validate it matches the expected schema."""
    data = json.loads(text)
    assert "code" in data, f"Missing 'code' field in response: {data}"
    assert "explanation" in data, f"Missing 'explanation' field in response: {data}"
    assert isinstance(data["code"], str)
    assert isinstance(data["explanation"], str)
    assert "def add" in data["code"], f"Expected 'def add' in code: {data['code']}"
    return data


@pytest.mark.functional
def test_json_schema_gpt4o_mini(api_key):
    client = _make_client(api_key, "gpt-4o-mini")
    text, job_id, messages = client.get_text(
        messages=[Message(role="user", content=PROMPT)],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=CODE_SCHEMA,
    )
    _validate_response(text)


@pytest.mark.functional
def test_json_schema_gpt4o(api_key):
    client = _make_client(api_key, "gpt-4o")
    text, job_id, messages = client.get_text(
        messages=[Message(role="user", content=PROMPT)],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=CODE_SCHEMA,
    )
    _validate_response(text)


@pytest.mark.functional
def test_json_schema_gpt5(api_key):
    client = _make_client(api_key, "gpt-5")
    text, job_id, messages = client.get_text(
        messages=[Message(role="user", content=PROMPT)],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=CODE_SCHEMA,
    )
    _validate_response(text)
