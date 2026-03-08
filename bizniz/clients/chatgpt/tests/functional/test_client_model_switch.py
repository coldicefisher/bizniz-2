"""
Functional test: verify set_model() works — one client, multiple models.

Run with:
    pytest bizniz/clients/chatgpt/tests/functional/ -m functional -v
"""
import json
import pytest

from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat4GPTClient
from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat


SCHEMA = {
    "name": "model_info",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "The answer to the question.",
            },
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
}


@pytest.mark.functional
def test_model_switch_across_models(api_key):
    """Create one client, switch models, verify each responds."""
    config = ChatGPTClientConfig(default_model="gpt-4o-mini")
    client = OpenAIChat4GPTClient(config=config, api_key=api_key)

    models = ["gpt-4o-mini", "gpt-4o", "gpt-5"]

    for model in models:
        client.set_model(model)
        text, _, _ = client.get_text(
            messages=[Message(role="user", content="What is 2 + 2?")],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=SCHEMA,
        )
        data = json.loads(text)
        assert "answer" in data, f"Model {model} failed to produce 'answer' field"
        assert "4" in data["answer"], f"Model {model} gave wrong answer: {data['answer']}"
