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
GEMINI_MODEL_PREFIXES = ("gemini-",)


def _is_claude_model(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in CLAUDE_MODEL_PREFIXES)


def _is_gemini_model(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in GEMINI_MODEL_PREFIXES)


class BiznizConfig(BaseModel):
    default_model: str = "gpt-4o-mini"
    engineer_model: str = "gpt-4o"
    architect_model: str = "gpt-4o"
    models: List[str] = [
        "gpt-4o-mini", "gpt-4o", "gpt-5",
        "claude-sonnet", "claude-opus",
    ]
    # Per-agent model progressions (override `models` when set)
    autocoder_models: Optional[List[str]] = None
    autotester_models: Optional[List[str]] = None
    repair_models: Optional[List[str]] = None
    # Three-phase strategy (used when AutoEngineer.run_three_phase is dispatched):
    #   debugger_model           — top-tier model for Phase 3 agentic debugging
    #                              (full context + discovery tools + run_command + run_tests)
    #   debugger_max_iterations  — per-ticket cap for the agentic debugger
    debugger_model: str = "gemini-pro"
    debugger_max_iterations: int = 12
    # Escalation thresholds (consecutive failures before escalating model)
    stall_threshold: int = 3
    agentic_debug_threshold: int = 5
    enable_agentic_debug: bool = True
    stall_recovery: str = "full"  # "full", "regenerate", or "none"
    # Pipeline execution mode
    layered_generation: bool = True
    parallel_services: bool = True
    max_service_workers: int = 4
    api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    is_azure: bool = False
    api_base: Optional[str] = None
    max_iterations: int = 20
    database_url: Optional[str] = None

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

        Automatically selects Claude, Gemini, or OpenAI based on the model name prefix.
        """
        resolved_model = model or self.default_model

        if _is_claude_model(resolved_model):
            return self._make_claude_client(resolved_model)
        if _is_gemini_model(resolved_model):
            return self._make_gemini_client(resolved_model)
        return self._make_openai_client(resolved_model)

    def make_engineer_client(self) -> BaseAIClient:
        """Create a client configured with the engineer model (best available)."""
        return self.make_client(model=self.engineer_model)

    def make_model_progression(self) -> ModelProgression:
        return ModelProgression(models=self.models)

    def make_autocoder_progression(self) -> ModelProgression:
        """Model progression for code generation (autocoder)."""
        return ModelProgression(models=self.autocoder_models or self.models)

    def make_autotester_progression(self) -> ModelProgression:
        """Model progression for test generation (autotester)."""
        return ModelProgression(models=self.autotester_models or self.models)

    def make_repair_progression(self) -> ModelProgression:
        """Model progression for code repair."""
        return ModelProgression(models=self.repair_models or self.models)

    def make_db(self) -> "BiznizDB":
        """Create a BiznizDB from the configured database_url."""
        from bizniz.db.bizniz_db import BiznizDB
        url = self.database_url or os.environ.get("BIZNIZ_DATABASE_URL")
        if not url:
            return None
        return BiznizDB(url)

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

    def _make_gemini_client(self, model: str) -> BaseAIClient:
        from bizniz.clients.gemini.gemini_client import GeminiClient
        api_key = self.gemini_api_key or os.environ.get("GEMINI_API_KEY")
        return GeminiClient(api_key=api_key, model_name=model)
