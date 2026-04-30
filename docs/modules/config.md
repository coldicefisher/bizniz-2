# Config (`BiznizConfig`)

`bizniz/config/bizniz_config.py`. Single Pydantic model that drives every model selection, escalation list, and provider in the pipeline.

## Purpose

`BiznizConfig` is loaded once at the start of a run from `bizniz.yaml` (the project's), and passed wherever a client or model progression is needed. It centralizes:

- Default + per-role model names (`default_model`, `engineer_model`, `architect_model`).
- Per-agent escalation progressions (`coder_models`, `tester_models`, `repair_models`).
- Stall / debug thresholds.
- Pipeline-mode flags (`layered_generation`, `parallel_services`, `max_service_workers`).
- API keys and Azure config.
- Optional unified DB URL.

The full key reference is in [reference/config_reference.md](../reference/config_reference.md).

## Fields

```python
class BiznizConfig(BaseModel):
    # Model selection
    default_model: str = "gpt-4o-mini"
    engineer_model: str = "gpt-4o"
    architect_model: str = "gpt-4o"
    models: List[str] = ["gpt-4o-mini", "gpt-4o", "gpt-5", "claude-sonnet", "claude-opus"]

    # Per-agent progression overrides (None means use `models`)
    coder_models:  Optional[List[str]] = None
    tester_models: Optional[List[str]] = None
    repair_models:     Optional[List[str]] = None

    # Thresholds
    stall_threshold: int = 3
    agentic_debug_threshold: int = 5
    enable_agentic_debug: bool = True
    stall_recovery: str = "full"   # "full" | "regenerate" | "none"

    # Pipeline mode
    layered_generation: bool = True
    parallel_services: bool = True
    max_service_workers: int = 4
    max_iterations: int = 20

    # API keys & provider config
    api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    is_azure: bool = False
    api_base: Optional[str] = None

    # DB
    database_url: Optional[str] = None
```

## Public API

| Method | Returns | Purpose |
|--------|---------|---------|
| `BiznizConfig.from_yaml(path)` | `BiznizConfig` | Load from a specific YAML path |
| `BiznizConfig.find_and_load()` | `BiznizConfig` | Walk CWD upward looking for `bizniz.yaml`; falls back to defaults |
| `make_client(model=None)` | `BaseAIClient` | Provider-routed client (Claude / Gemini / OpenAI based on prefix) |
| `make_engineer_client()` | `BaseAIClient` | Shortcut: `make_client(self.engineer_model)` |
| `make_model_progression()` | `ModelProgression` | Built from `self.models` |
| `make_autocoder_progression()` | `ModelProgression` | `coder_models` if set, else `models` |
| `make_autotester_progression()` | `ModelProgression` | same pattern |
| `make_repair_progression()` | `ModelProgression` | same pattern |
| `make_db()` | `BiznizDB \| None` | Unified DB; None if neither `database_url` nor `BIZNIZ_DATABASE_URL` is set |

Provider routing (`make_client`) prefix-checks: `claude-` → `_make_claude_client`, `gemini-` → `_make_gemini_client`, anything else → `_make_openai_client`.

## API key resolution

For each provider, `make_client` falls back through:

1. `BiznizConfig.<provider>_api_key` (the YAML / Pydantic field).
2. The standard env var: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (Gemini also accepts `GOOGLE_API_KEY` at the client level).

If both are unset, the client raises an auth error on construction.

## Example `bizniz.yaml`

```yaml
default_model: gemini-flash-lite
engineer_model: gemini-flash
architect_model: gemini-flash
models:
  - gemini-flash-lite
  - gemini-flash
  - gemini-pro
coder_models:
  - gemini-flash-lite
  - gemini-flash
  - gemini-pro
repair_models:
  - gemini-flash
  - gemini-pro
stall_threshold: 3
agentic_debug_threshold: 2
enable_agentic_debug: false
layered_generation: true
parallel_services: true
max_service_workers: 4
max_iterations: 20
```

(That's a copy of the `bizniz.yaml` shipped at the repo root.)

## Example usage

```python
from bizniz.config.bizniz_config import BiznizConfig

cfg = BiznizConfig.find_and_load()

architect_client = cfg.make_client(cfg.architect_model)
progression = cfg.make_autocoder_progression()
db = cfg.make_db()  # None if no database_url
```

## Interactions

- **Calls into:** `ChatGPTClient`, `ClaudeClient` (lazy import), `GeminiClient` (lazy import), `ModelProgression`, `BiznizDB` (lazy import).
- **Called by:** application entrypoints + the `Architect`/`Engineer` factory closures.

## Gotchas

- **`find_and_load` walks PARENT directories.** Run bizniz from anywhere inside a project that contains a `bizniz.yaml` and it works. From outside the project, you get the model defaults.
- **`make_db` returns `None` when no DB URL is configured.** The pipeline degrades gracefully — workspaces fall back to per-workspace SQLite at `.bizniz/bizniz.db`.
- **Azure mode requires `api_base`** plus `available_models` in the `ChatGPTClientConfig` (not on `BiznizConfig` directly — see `bizniz/clients/chatgpt/chatgpt_client_config.py`).
- **Per-agent progressions only override the list, not the start position.** When the engineer suggests an `initial_model`, the orchestrator calls `set_start(name)` on every progression to align them. If the suggested name isn't in a progression, `set_start` silently does nothing.
- **`stall_recovery` is a tri-state.** "full" reruns generation from scratch; "regenerate" only regenerates one side (code or tests); "none" disables stall recovery and lets the orchestrator hit `OrchestratorMaxIterationsError`.
- **`max_iterations` here is the per-issue cap.** Per-service and per-project caps don't exist — use `max_service_workers` to limit concurrency, but each service still runs to its own completion or max-iter cap.
