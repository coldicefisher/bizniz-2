# `bizniz.yaml` Configuration Reference

Every key supported by `BiznizConfig` (defined in `bizniz/config/bizniz_config.py`).

`BiznizConfig.find_and_load()` walks from the current working directory up the directory tree looking for the first `bizniz.yaml` it finds. If none exists, defaults are used — but the required progression lists below mean a fully-empty config will still hard-fail at construction.

## Top-level keys

### Model selection

There is **no** shared `default_model` fallback. Every role names its model explicitly so a typo or omission hard-fails at config load rather than silently routing to a generic default.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `engineer_model` | str | `"gpt-4o"` | Used by `make_engineer_client()` (Engineer's analysis + plan + governance + enrichment calls) |
| `architect_model` | str | `"gpt-4o"` | Used by the Architect (decomposition + milestone walk) |
| `planner_model` | str | `"gemini-pro"` | Used by `make_planner_client()` (one-shot project planning) |
| `integration_tester_model` | str | `"gemini-pro"` | Used by `make_integration_tester_client()` (HTTPApiTester + WebUITester) |
| `debugger_model` | str | `"gemini-pro"` | Single-tier fallback for debugger callers — see `debugger_escalation` below for the active chain |

`make_client(model)` itself **requires** an explicit model name; passing an empty/missing value raises `ValueError`. Use the per-role factories above, or pick one of the role fields directly.

### Model progressions (required)

Every list **must** have at least one entry — pydantic enforces `Field(min_length=1)`. There's no shared `models` fallback; each list must be defined explicitly. A `bizniz.yaml` missing any of these hard-fails at load.

| Key | Type | Notes |
|-----|------|-------|
| `coder_models` | list[str] | Stall-escalation progression for the Coder agent |
| `tester_models` | list[str] | Stall-escalation progression for the Tester agent |
| `repair_models` | list[str] | Stall-escalation progression for the QuickDebugger (inline repair) |

Provider routing happens automatically by name prefix:

- `claude-*` → Anthropic
- `gemini-*` → Google
- everything else → OpenAI / Azure

### Debugger escalation chain

`debugger_escalation` is an array-of-hashes (NOT a flat list). Each tier has its own model + per-attempt turn budget + retry count. Used by the integration debugger, the post-flight repair loop, and the FA debugger. Tiers run in order, sticky repair log compounds across tiers.

```yaml
debugger_escalation:
  - model: gemini-flash-lite
    max_turns: 1
    repair_attempts: 1
  - model: gemini-flash-lite
    max_turns: 20
    repair_attempts: 20
  - model: gemini-flash-top
    max_turns: 12
    repair_attempts: 3
```

Pro tier is deliberately absent — if mid-tier (`gemini-flash-top`) can't grind a fix in 3×12 attempts after the cheap tier has already burned 20×20, the system stops and surfaces for human review rather than burning pro budget.

### Stall and debugging

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `stall_threshold` | int | `3` | Consecutive failures before stall is declared |
| `agentic_debug_threshold` | int | `5` | Consecutive failures before the AgenticDebugger is invoked |
| `enable_agentic_debug` | bool | `true` | Master switch for deep diagnosis |
| `stall_recovery` | str | `"full"` | `"full"` (rebuild from scratch), `"regenerate"` (one side only), `"none"` |
| `debugger_max_iterations` | int | `12` | Per-ticket cap for the legacy single-tier agentic debugger |

### Pipeline mode

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `layered_generation` | bool | `true` | Use `Engineer.run_layered`. False uses `run` (sequential) |
| `parallel_services` | bool | `true` | Architect dispatches services within a layer in parallel |
| `max_service_workers` | int | `4` | Thread pool size for parallel service dispatch |
| `max_iterations` | int | `20` | Per-issue inner loop cap in the orchestrator |

### API keys + provider config

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `api_key` | str \| null | null | OpenAI key. Falls back to `OPENAI_API_KEY` env var |
| `anthropic_api_key` | str \| null | null | Anthropic key. Falls back to `ANTHROPIC_API_KEY` env var |
| `gemini_api_key` | str \| null | null | Gemini key. Falls back to `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `is_azure` | bool | `false` | Use Azure OpenAI Chat Completions API instead of the OpenAI Responses API |
| `api_base` | str \| null | null | Azure resource endpoint (required when `is_azure: true`) |

### Database

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `database_url` | str \| null | null | Unified DB URL. `mysql://...` or `sqlite:///path` or `:memory:`. Falls back to env var `BIZNIZ_DATABASE_URL`. If neither is set, per-workspace SQLite is used. |

## Environment variables

Bizniz reads these at runtime. Most are fallbacks for keys that can also live in YAML.

| Env var | Purpose |
|---------|---------|
| `OPENAI_API_KEY` | OpenAI / Azure key |
| `ANTHROPIC_API_KEY` | Claude key |
| `GEMINI_API_KEY` | Gemini key |
| `GOOGLE_API_KEY` | Alternate name accepted by Gemini client |
| `BIZNIZ_DATABASE_URL` | Override for `database_url` |
| `BIZNIZ_SKELETONS_DIR` | Where the architect looks for skeleton repos. Defaults to `~`. Used by `bizniz/architect/skeletons.py:skeletons_root()` |

## Example: Gemini-only setup

```yaml
engineer_model: gemini-flash-top
architect_model: gemini-flash-top
planner_model: gemini-flash-top
integration_tester_model: gemini-pro
debugger_model: gemini-flash-lite

coder_models:
  - gemini-flash-lite
  - gemini-flash
  - gemini-flash-top
tester_models:
  - gemini-flash-lite
  - gemini-flash
  - gemini-flash-top
repair_models:
  - gemini-flash
  - gemini-flash-top

debugger_escalation:
  - model: gemini-flash-lite
    max_turns: 1
    repair_attempts: 1
  - model: gemini-flash-lite
    max_turns: 20
    repair_attempts: 20
  - model: gemini-flash-top
    max_turns: 12
    repair_attempts: 3

stall_threshold: 3
agentic_debug_threshold: 2
enable_agentic_debug: false
layered_generation: true
parallel_services: true
max_service_workers: 4
max_iterations: 20
```

## Example: mixed providers

```yaml
engineer_model: gpt-4o
architect_model: claude-sonnet
planner_model: claude-opus
integration_tester_model: gpt-4o
debugger_model: claude-opus

coder_models:
  - gpt-4o-mini
  - gpt-4o
  - gpt-5
tester_models:
  - gpt-4o-mini
  - gpt-4o
repair_models:
  - claude-sonnet
  - claude-opus

debugger_escalation:
  - model: gpt-4o-mini
    max_turns: 1
    repair_attempts: 1
  - model: gpt-4o
    max_turns: 12
    repair_attempts: 3

api_key: sk-...
anthropic_api_key: sk-ant-...
stall_threshold: 2
agentic_debug_threshold: 3
enable_agentic_debug: true
stall_recovery: full
layered_generation: true
parallel_services: true
max_service_workers: 4
max_iterations: 20
database_url: mysql://bizniz:secret@localhost/bizniz
```

## Example: Azure OpenAI

```yaml
is_azure: true
api_base: https://my-resource.openai.azure.com/
api_key: <azure-key>
engineer_model: gpt-4o
architect_model: gpt-4o
planner_model: gpt-4o
integration_tester_model: gpt-4o
debugger_model: gpt-4o

coder_models: [gpt-4o-mini, gpt-4o]
tester_models: [gpt-4o-mini, gpt-4o]
repair_models: [gpt-4o-mini, gpt-4o]
```

(Note: Azure also requires `available_models` mapping inside a `ChatGPTClientConfig` if you bypass `BiznizConfig` — but the standard path through `BiznizConfig.make_client` constructs that for you.)
