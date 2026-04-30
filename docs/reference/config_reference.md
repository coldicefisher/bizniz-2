# `bizniz.yaml` Configuration Reference

Every key supported by `BiznizConfig` (defined in `bizniz/config/bizniz_config.py`).

`BiznizConfig.find_and_load()` walks from the current working directory up the directory tree looking for the first `bizniz.yaml` it finds. If none exists, defaults are used.

## Top-level keys

### Model selection

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `default_model` | str | `"gpt-4o-mini"` | Used when no agent has its own override |
| `engineer_model` | str | `"gpt-4o"` | Used by `make_engineer_client()` |
| `architect_model` | str | `"gpt-4o"` | Used by the architect (read by user code, not auto) |

### Model progressions

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `models` | list[str] | `["gpt-4o-mini", "gpt-4o", "gpt-5", "claude-sonnet", "claude-opus"]` | Default progression for stall escalation |
| `autocoder_models` | list[str] \| null | null | Override for autocoder; falls back to `models` |
| `autotester_models` | list[str] \| null | null | Override for autotester; falls back to `models` |
| `repair_models` | list[str] \| null | null | Override for repair; falls back to `models` |

Provider routing happens automatically by name prefix:

- `claude-*` → Anthropic
- `gemini-*` → Google
- everything else → OpenAI / Azure

### Stall and debugging

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `stall_threshold` | int | `3` | Consecutive failures before stall is declared |
| `agentic_debug_threshold` | int | `5` | Consecutive failures before the AgenticDebugger is invoked |
| `enable_agentic_debug` | bool | `true` | Master switch for deep diagnosis |
| `stall_recovery` | str | `"full"` | `"full"` (rebuild from scratch), `"regenerate"` (one side only), `"none"` |

### Pipeline mode

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `layered_generation` | bool | `true` | Use `AutoEngineer.run_layered`. False uses `run` (sequential) |
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
default_model: gemini-flash-lite
engineer_model: gemini-flash
architect_model: gemini-flash
models:
  - gemini-flash-lite
  - gemini-flash
  - gemini-pro
autocoder_models:
  - gemini-flash-lite
  - gemini-flash
  - gemini-pro
autotester_models:
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

## Example: mixed providers

```yaml
default_model: gpt-4o-mini
engineer_model: gpt-4o
architect_model: claude-sonnet
models:
  - gpt-4o-mini
  - gpt-4o
  - claude-sonnet
  - claude-opus
autocoder_models:
  - gpt-4o-mini
  - gpt-4o
  - gpt-5
repair_models:
  - claude-sonnet
  - claude-opus
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
default_model: gpt-4o-mini
engineer_model: gpt-4o
```

(Note: Azure also requires `available_models` mapping inside a `ChatGPTClientConfig` if you bypass `BiznizConfig` — but the standard path through `BiznizConfig.make_client` constructs that for you.)
