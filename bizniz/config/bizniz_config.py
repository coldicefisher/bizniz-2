from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel
import yaml
import os

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient
from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
from bizniz.orchestrator.model_progression import ModelProgression


CLAUDE_MODEL_PREFIXES = ("claude-",)


def _is_claude_model(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in CLAUDE_MODEL_PREFIXES)


class BiznizConfig(BaseModel):
    default_model: str = "gpt-4o-mini"
    engineer_model: str = "gpt-4o"
    models: List[str] = [
        "gpt-4o-mini", "gpt-4o", "gpt-5",
        "claude-sonnet", "claude-opus",
    ]
    api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    is_azure: bool = False
    api_base: Optional[str] = None
    max_iterations: int = 20

    @classmethod
    def from_yaml(cls, path: str) -> "BiznizConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    @classmethod
    def find_and_load(cls) -> "BiznizConfig":
        """Search CWD and parent directories for bizniz.yaml."""
        current = Path.cwd()
        while True:
            candidate = current / "bizniz.yaml"
            if candidate.exists():
                return cls.from_yaml(str(candidate))
            parent = current.parent
            if parent == current:
                break
            current = parent
        return cls()  # defaults

    def make_client(self, model: Optional[str] = None) -> BaseAIClient:
        """Create an AI client for the given model.

        Automatically selects Claude or OpenAI based on the model name prefix.
        """
        resolved_model = model or self.default_model

        if _is_claude_model(resolved_model):
            return self._make_claude_client(resolved_model)
        return self._make_openai_client(resolved_model)

    def make_engineer_client(self) -> BaseAIClient:
        """Create a client configured with the engineer model (best available)."""
        return self.make_client(model=self.engineer_model)

    def make_model_progression(self) -> ModelProgression:
        return ModelProgression(models=self.models)

    def _make_openai_client(self, model: str) -> ChatGPTClient:
        api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
        config = ChatGPTClientConfig(
            default_model=model,
            is_azure=self.is_azure,
            api_base=self.api_base,
        )
        return ChatGPTClient(config=config, api_key=api_key)

    def _make_claude_client(self, model: str) -> BaseAIClient:
        from bizniz.clients.claude.claude_client import ClaudeClient
        api_key = self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        return ClaudeClient(api_key=api_key, model_name=model)
