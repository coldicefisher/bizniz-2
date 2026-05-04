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


class DebuggerTier(BaseModel):
    """One tier of the AgenticDebugger escalation chain.

    The chain runs sequentially: cheap-and-many at the bottom,
    expensive-and-few at the top. Each tier gets ``repair_attempts``
    independent debug sessions; each session may use up to
    ``max_turns`` agent steps (tool calls + diagnose) before being
    forced to commit.

    Sticky repair log: every attempt at every tier reads the full
    history of prior attempts (across QuickDebugger,
    AgenticDebugger, and every prior tier in this chain) so the
    debugger never repeats a fix the previous tier already tried.
    """
    model: str
    max_turns: int = 12
    repair_attempts: int = 2


class BiznizConfig(BaseModel):
    default_model: str = "gpt-4o-mini"
    engineer_model: str = "gpt-4o"
    architect_model: str = "gpt-4o"
    # Top-tier model for the Planner agent (multi-week project sequencing).
    # See bizniz/planner/. One call per project — top tier is justified.
    planner_model: str = "gemini-pro"
    models: List[str] = [
        "gpt-4o-mini", "gpt-4o", "gpt-5",
        "claude-sonnet", "claude-opus",
    ]
    # Per-agent model progressions (override `models` when set)
    coder_models: Optional[List[str]] = None
    tester_models: Optional[List[str]] = None
    repair_models: Optional[List[str]] = None
    # Three-phase strategy (used when Engineer.run_three_phase is dispatched):
    #   debugger_model           — top-tier model for Phase 3 agentic debugging
    #                              (full context + discovery tools + run_command + run_tests)
    #   debugger_max_iterations  — per-ticket cap for the agentic debugger
    debugger_model: str = "gemini-pro"
    # Escalation chain for the AgenticDebugger. Run cheap-and-many
    # first (flash-top with 2 attempts), escalate to pro on failure.
    # Each tier sees the full prior repair log so it doesn't repeat
    # fixes the previous tier already tried.
    debugger_escalation: List[DebuggerTier] = [
        DebuggerTier(model="gemini-flash-top", max_turns=12, repair_attempts=2),
        DebuggerTier(model="gemini-pro", max_turns=8, repair_attempts=1),
    ]
    # Model used by HTTPApiTester and WebUITester for generating
    # integration tests. Test generation is once-per-service-per-run
    # so a top-tier model is justified for hallucination resistance.
    integration_tester_model: str = "gemini-pro"
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

    def make_planner_client(self) -> BaseAIClient:
        """Create a client configured with the planner model (top tier)."""
        return self.make_client(model=self.planner_model)

    def make_integration_tester_client(self) -> BaseAIClient:
        """Client for HTTPApiTester / WebUITester. Top tier — these
        generate the integration tests that gate every milestone, so
        hallucinations have outsized cost (debugger amplifies them
        into real code corruption)."""
        return self.make_client(model=self.integration_tester_model)

    def make_model_progression(self) -> ModelProgression:
        return ModelProgression(models=self.models)

    def make_autocoder_progression(self) -> ModelProgression:
        """Model progression for code generation (coder)."""
        return ModelProgression(models=self.coder_models or self.models)

    def make_autotester_progression(self) -> ModelProgression:
        """Model progression for test generation (tester)."""
        return ModelProgression(models=self.tester_models or self.models)

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
