# BaseAIAgent

The shared base class for every AI agent in bizniz. Lives at `bizniz/core/agent.py`; `bizniz/base_ai_agent.py` is a backward-compatible shim that re-exports it.

## Purpose

`BaseAIAgent` is the "what every agent has" contract. It owns the AI client, the workspace, the execution environment, the message-history book-keeping, and a couple of utility helpers (saving generated code to disk with a metadata header, stripping markdown code fences, normalizing JSON output). Concrete agents subclass it and supply a system prompt via the abstract `_process_system_prompt` property.

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `client` | `BaseAIClient` | The AI provider client (validated to inherit from `BaseAIClient`) |
| `environment` | `BaseExecutionEnvironment` | Validated to inherit from `BaseExecutionEnvironment` |
| `workspace` | `BaseWorkspace` | Required — file I/O always goes through this |
| `max_message_history_length` | `int = 40` | Hard cap on history length before truncation |
| `max_retries` | `int = 5` | Default retry budget for AI calls |
| `on_event` | `Optional[Callable]` | Generic event hook (for UIs / websockets) |
| `on_status_message` | `Optional[Callable[[str], None]]` | Human-readable status updates |

The constructor immediately seeds the message history with one `system` message taken from `self._process_system_prompt`.

## Public surface

| Name | Type | Notes |
|------|------|-------|
| `message_history` | property | Returns history with truncation: keeps the system prompt + latest `(max-1)` messages |
| `clear_message_history()` | method | Resets history to just the system prompt (or override) |
| `set_system_prompt_override(prompt)` | method | Swaps in a different system prompt and rebuilds history. Used by the orchestrator for language-conditional prompts. |
| `add_messages_to_history(messages)` | method | Normalizes & appends; only allows ONE system message at the front |
| `emit(event)` | method | Calls `on_event` callback if configured |
| `get_metadata(prompt)` | method | Returns `{"problem_statement": prompt}` (override to add more) |
| `clean_llm_json(text)` | method | Delegates to `bizniz.utils.json.clean_llm_json` |

## Protected helpers (used by subclasses)

| Method | What it does |
|--------|--------------|
| `_save_code_to_file(code, filename, prompt=None, metadata=None)` | Writes code to the workspace at `filename`. If a cached copy exists, it's timestamped and rotated into `<dir>/cached/`. A `BIZNIZ_METADATA_START`/`END` block is prepended with `problem_statement` and `saved_at`. |
| `_strip_code_block(text)` | Strips a single set of ` ``` ` fences (and a leading `python` tag) |
| `_process_system_prompt` | Abstract property each subclass implements |

## Typical subclass shape

```python
from bizniz.base_ai_agent import BaseAIAgent

class MyAgent(BaseAIAgent):
    @property
    def _process_system_prompt(self) -> str:
        return "You are an example agent..."

    def do_thing(self, prompt: str) -> Result:
        self.add_messages_to_history([{"role": "user", "content": prompt}])
        text, _, output_msgs = self._client.get_text(
            messages=self.message_history,
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=MyAgentSchema,
        )
        self.add_messages_to_history(output_msgs)
        text = self.clean_llm_json(text)
        return Result(**json.loads(text))
```

## Interactions

- **Calls into:** `BaseAIClient.get_text`, `BaseExecutionEnvironment.execute`, `BaseWorkspace.read_file/write_file`, `bizniz.utils.code_metadata.build_metadata_block`, `bizniz.utils.json.clean_llm_json`.
- **Subclassed by:** `Autocoder`, `Autotester`, `QuickDebugger` (formerly `Autodebugger`), `AutoEngineer`, `AutoArchitect`. The `AgenticDebugger` deliberately does NOT subclass this — it uses its own message list (no history accumulation) since each tool-loop turn is a fresh self-contained call.

## Gotchas

- The system prompt is pulled in the constructor *before* a subclass has run any of its own `__init__` body. If your subclass's `_process_system_prompt` reads instance attributes, set them BEFORE `super().__init__(...)`. `AutoEngineer` does exactly this for `self._language`.
- `max_message_history_length` is a soft cap — `add_messages_to_history` always appends, but the `message_history` property returns the truncated view (system + last N-1). The on-disk history is unbounded.
- `add_messages_to_history` rejects extra system messages once one is present. To swap the system prompt, call `set_system_prompt_override(new_prompt)`.
- `_save_code_to_file` will append `.py` to the cached filename if the input lacks one — the workspace path itself is written verbatim. This matters if you save TypeScript: the workspace gets `App.tsx`, but the rotation copy under `cached/` is `App.tsx.py`. It's harmless but surprising.
