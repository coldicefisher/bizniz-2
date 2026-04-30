# Clients

`bizniz/clients/` contains AI provider clients. Every client implements the abstract `BaseAIClient` and is selected automatically by `BiznizConfig.make_client(model)` based on the model-name prefix.

## Architecture

```
BaseAIClient   (bizniz/core/client.py — shim at bizniz/clients/base_ai_client.py)
   │
   ├── ChatGPTClient        (bizniz/clients/openai/chatgpt_client.py)
   │     supports OpenAI Responses API + Azure Chat Completions API
   │
   ├── ClaudeClient         (bizniz/clients/claude/claude_client.py)
   │     Anthropic SDK; embeds JSON schema in system prompt for JSON_SCHEMA mode
   │
   └── GeminiClient         (bizniz/clients/gemini/gemini_client.py)
         google-genai SDK; same schema-in-system-prompt approach
```

`bizniz/clients/openai/` holds the actual OpenAI implementation. `bizniz/clients/chatgpt/` is a backward-compatibility re-export of every public name from `openai/` so older imports (`from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient`) keep working.

## `BaseAIClient`

Abstract methods every provider must implement:

| Method | Returns | Purpose |
|--------|---------|---------|
| `get_text(messages, ...) → (text, job_id, output_messages)` | tuple | Single completion call, returns model output text |
| `set_model(model_name)` | None | Switch the active model (used by orchestrator escalation) |
| `ai_agent` | property | Underlying SDK client object |

Common keyword arguments accepted by every provider's `get_text`:

- `messages` — list of dicts or `MessageList`
- `response_format` — `ResponseFormat.TEXT` | `JSON` | `JSON_SCHEMA`
- `schema` — required when `response_format=JSON_SCHEMA`; the agent passes the schema dict
- `use_message_history` — defaults to True; the agentic tool loop sets False
- `max_tokens`, `temperature` — generation knobs

## OpenAI / Azure

`bizniz/clients/openai/chatgpt_client.py:ChatGPTClient`:

- Reads `OPENAI_API_KEY` from env if no `api_key` is passed.
- Selects between `OpenAI()` and `AzureOpenAI()` based on `ChatGPTClientConfig.is_azure`.
- For OpenAI direct, uses the **Responses API** (`responses.create(input=..., text={"format": ...})`).
- For Azure, uses **Chat Completions** (`chat.completions.create(messages=...)`).
- Rate-limit handling: parses "try again in Xs" hints, sleeps, retries up to 3 times. Detects `insufficient_quota` and `billing_hard_limit_reached` to raise `OpenAIInsufficientFunds` (a subclass of `AIInsufficientFunds`) so the pipeline can stop immediately.

Errors (`bizniz/clients/openai/errors.py` and shim `bizniz/clients/chatgpt/errors.py`):

| Class | Inherits from | Purpose |
|-------|--------------|---------|
| `OpenAIClientError` | `AIClientError` | catch-all |
| `OpenAIRateLimit` | `OpenAIClientError` | handled by tool loop with backoff |
| `OpenAIInsufficientFunds` | `AIInsufficientFunds` | terminal — pipeline aborts |
| `OpenAIAuthError` | `OpenAIClientError` | bad API key |
| `OpenAIInvalidRequest` | `OpenAIClientError` | malformed call |

## Claude

`bizniz/clients/claude/claude_client.py:ClaudeClient`:

- Reads `ANTHROPIC_API_KEY`.
- Resolves short names via `CLAUDE_MODELS` map: `claude-sonnet` → `claude-sonnet-4-20250514`, `claude-opus` → `claude-opus-4-20250514`, `claude-haiku` → `claude-haiku-4-5-20251001`. Unknown names pass through unchanged.
- For `JSON_SCHEMA` mode, the schema is appended to the system prompt and the model is asked to emit only valid JSON. Parsing happens at the agent level (not in the client).

Errors: `ClaudeClientError`, `ClaudeRateLimit`, `ClaudeInsufficientFunds` (inherits `AIInsufficientFunds`), `ClaudeAuthError`, `ClaudeInvalidRequest`.

## Gemini

`bizniz/clients/gemini/gemini_client.py:GeminiClient`:

- Reads `GEMINI_API_KEY` or `GOOGLE_API_KEY`.
- `GEMINI_MODELS` map: `gemini-flash-lite` → `gemini-2.5-flash-lite`, etc. The map is editable to track preview names.
- Same JSON-in-system-prompt strategy as Claude.

Errors: `GeminiClientError`, `GeminiRateLimit`, `GeminiInsufficientFunds` (inherits `AIInsufficientFunds`), `GeminiAuthError`, `GeminiInvalidRequest`, `GeminiContextLengthExceeded` (inherits `AIContextLengthExceeded` — caught by the tool loop to trim and retry).

## Selecting a client

Provider routing is done by `BiznizConfig.make_client(model)`:

```python
def _is_claude_model(name): return name.startswith("claude-")
def _is_gemini_model(name): return name.startswith("gemini-")

def make_client(self, model=None):
    name = model or self.default_model
    if _is_claude_model(name):  return self._make_claude_client(name)
    if _is_gemini_model(name):  return self._make_gemini_client(name)
    return self._make_openai_client(name)
```

So a model named `gpt-4o-mini`, `gpt-5`, etc. routes to OpenAI. `claude-sonnet` to Claude. `gemini-flash` to Gemini.

## Message types

`bizniz/core/types.py` defines provider-neutral types:

| Type | Notes |
|------|-------|
| `Role` enum | `SYSTEM`, `USER`, `ASSISTANT` |
| `Message` dataclass | `(role, content)` — `to_dict()` returns the OpenAI-shape dict |
| `MessageList` | wraps `List[Message]` with token counters |
| `ResponseFormat` enum | `TEXT`, `JSON`, `JSON_SCHEMA` |
| `parse_response_format(rf, schema)` | returns the OpenAI-API `text.format` payload |
| `normalize_messages(...)` | accepts `MessageList | List[Message] | List[dict]`, returns `List[dict]` |

`bizniz/clients/chatgpt/messages.py` and `bizniz/clients/chatgpt/types/response_format.py` exist as re-exports for older imports.

## Errors as a hierarchy

`bizniz/core/errors.py` defines the provider-neutral root errors. Provider errors inherit from these so the orchestrator can catch generically:

```
AIClientError
├── AIInsufficientFunds       (every provider's *InsufficientFunds inherits)
└── AIContextLengthExceeded   (the tool loop catches this to trim history)
```

Importing from `bizniz.clients.errors` is the recommended path; it re-exports the core error types.

## Example

```python
from bizniz.config.bizniz_config import BiznizConfig

cfg = BiznizConfig.find_and_load()
client = cfg.make_client(model="claude-sonnet")  # Claude
text, job_id, output_messages = client.get_text(
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 2+2?"},
    ],
    response_format=ResponseFormat.TEXT,
)
```

## Gotchas

- **Two layouts coexist.** The real implementation is in `bizniz/clients/openai/`; `bizniz/clients/chatgpt/` is a thin shim layer. New code should import from `openai/`.
- **The Responses API differs from Chat Completions.** OpenAI direct uses `responses.create(input=..., text={"format": ...})`; Azure uses `chat.completions.create(messages=..., response_format=...)`. The split is in the client; agents don't see it.
- **Insufficient funds is terminal.** Any subclass of `AIInsufficientFunds` is re-raised through every layer up to the architect, which logs and stops. Don't swallow it.
- **JSON mode for Claude/Gemini does NOT use a real schema parameter** — the schema is injected into the system prompt. This means malformed model output is more common with these providers, and the agent layer's `clean_llm_json` is essential.
- **Rate-limit retry is at the client level for OpenAI**, but the tool loop also has its own retry (in `bizniz/tools/tool_loop.py:run_tool_loop`). Double-retries don't usually compound badly because of the time gap, but be aware.
