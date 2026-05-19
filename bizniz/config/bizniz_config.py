from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field
import yaml
import os

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient
from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
# ModelProgression is a v1 vestige; the v2 ServiceImplementer uses
# a single model per agent (no in-agent escalation). Keeping the
# import + factory methods working until v2 entry points land so
# the config module itself doesn't fail to load.
from bizniz.lib.model_progression import ModelProgression


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
    ``tool_iterations`` agent steps (tool calls + diagnose) before
    being forced to commit.

    ``tool_iterations`` = LLM round-trips within a single attempt.
    ``repair_attempts`` = independent debug sessions before escalating
    to the next tier.

    Sticky repair log: every attempt at every tier reads the full
    history of prior attempts (across QuickDebugger,
    AgenticDebugger, and every prior tier in this chain) so the
    debugger never repeats a fix the previous tier already tried.
    """
    model: str
    tool_iterations: int = 12
    repair_attempts: int = 2


class BiznizConfig(BaseModel):
    # Per-agent model fields below are required (no shared
    # ``default_model`` fallback). Either the config names a specific
    # model for the role or the app refuses to run.
    engineer_model: str = "gpt-4o"
    architect_model: str = "gpt-4o"
    # Top-tier model for the Planner agent (multi-week project sequencing).
    # See bizniz/planner/. One call per project — top tier is justified.
    planner_model: str = "gemini-pro"
    # Per-agent stall-escalation progressions. REQUIRED — no shared
    # fallback. Each list MUST have at least one model. The previous
    # ``models`` array was a silent fallback that hid mis-config; any
    # bizniz.yaml without these three lists must hard-fail at load
    # rather than silently route to a generic default.
    coder_models: List[str] = Field(min_length=1)
    tester_models: List[str] = Field(min_length=1)
    repair_models: List[str] = Field(min_length=1)
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
        DebuggerTier(model="gemini-flash-top", tool_iterations=12, repair_attempts=2),
        DebuggerTier(model="gemini-pro", tool_iterations=8, repair_attempts=1),
    ]
    # Model used by HTTPApiTester and WebUITester for generating
    # integration tests. Test generation is once-per-service-per-run
    # so a top-tier model is justified for hallucination resistance.
    integration_tester_model: str = "gemini-pro"
    debugger_max_iterations: int = 12
    # Progress-based stopping (2026-05-17, D2). Replaces hard
    # iteration caps in the debug loop. The agentic debugger keeps
    # running as long as failures are decreasing; stops only after
    # this many consecutive no-progress iterations (stalled OR
    # regression). Default 5 — gives the agent runway to diagnose
    # without burning forever on genuinely stuck cases. Applies to
    # integration debug + smoke recovery + post-refactor test repair
    # + final-test recovery (one source of truth across all live-
    # stack debugging surfaces).
    debugger_stall_threshold: int = 5
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
    # v4 pipeline (2026-05-19): max concurrent CoderTesterAgent
    # subprocesses dispatched by PIRunner within a single service's
    # topological level. 6 = Anthropic Max-plan realistic ceiling
    # (start lower at 4 if rate-limits bite; raise to 8+ if not).
    max_parallel_coders: int = 6
    # v4 repair tier list (2026-05-19): repair issues are by definition
    # the harder case (IMPLEMENT already missed there). Going Opus-only
    # skips the Haiku→Opus escalation chain that doubled cost on stuck
    # issues in tonight's v3.1 run (BA-fix1-1: Haiku 7m + Opus 11m = 18m
    # vs Opus-direct ~11m, -39%). IMPLEMENT keeps the Haiku-default.
    use_v4_repair_tiers: List[str] = ["claude-cli:claude-opus-4-7"]

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

    def make_client(self, model: str) -> BaseAIClient:
        """Create an AI client for the given model.

        ``model`` is REQUIRED — there's no shared ``default_model``
        fallback. Caller must pass a specific model name. The router
        selects Claude / Gemini / OpenAI by name prefix.
        """
        if not model:
            raise ValueError(
                "make_client() requires an explicit model name. "
                "There is no default_model fallback — pick one of the "
                "configured model fields (engineer_model, architect_model, "
                "planner_model, integration_tester_model, debugger_model) "
                "or supply a model name directly."
            )
        resolved_model = model

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
        """Shared progression for callers that don't differentiate per-
        agent. Returns the Coder's progression, which is the most
        central in CodingOrchestrator (the Coder generates the actual
        files; Tester + repair derive from its output). Prefer the
        per-agent factories below in new code; this is for legacy
        callers that pass a single ``model_progression`` to the
        orchestrator instead of per-agent ones.
        """
        return ModelProgression(models=list(self.coder_models))

    def make_autocoder_progression(self) -> ModelProgression:
        """Model progression for code generation (Coder agent).
        Drives stall-escalation when the Coder's generated code keeps
        failing tests. Required in config — no fallback."""
        return ModelProgression(models=list(self.coder_models))

    def make_autotester_progression(self) -> ModelProgression:
        """Model progression for test generation (Tester agent).
        Drives stall-escalation when the Tester's generated tests are
        malformed. Required in config — no fallback."""
        return ModelProgression(models=list(self.tester_models))

    def make_repair_progression(self) -> ModelProgression:
        """Model progression for inline repair (QuickDebugger).
        Drives stall-escalation when single-call repair stalls.
        Required in config — no fallback."""
        return ModelProgression(models=list(self.repair_models))

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
        # ``claude-cli*`` routes to the subprocess client (Max plan,
        # $0 marginal). Anything else under the claude-* prefix hits
        # the (paid) Anthropic API client.
        if model.startswith("claude-cli"):
            from bizniz.clients.claude_cli import ClaudeCliClient
            return ClaudeCliClient(model_name=model)
        from bizniz.clients.claude.claude_client import ClaudeClient
        api_key = self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        return ClaudeClient(api_key=api_key, model_name=model)

    def _make_gemini_client(self, model: str) -> BaseAIClient:
        from bizniz.clients.gemini.gemini_client import GeminiClient
        api_key = self.gemini_api_key or os.environ.get("GEMINI_API_KEY")
        return GeminiClient(api_key=api_key, model_name=model)
