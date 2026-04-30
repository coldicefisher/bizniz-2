# Autocoder

`bizniz/agents/autocoder/autocoder.py` (with a backward-compat shim at `bizniz/autocoder/autocoder.py`). The agent that writes code.

## Purpose

Given a prompt — possibly with tests as constraints — the autocoder produces source files. It supports:

- **Single-file** modes (`generate_only`, `generate`, `repair`) for the legacy single-file orchestrator.
- **Multi-file tool-loop** modes (`generate_multi`, `repair_multi`) that use the agentic discovery tools to read existing files on demand.
- **Inline multi-file repair** (`repair_multi_inline`) that sends all relevant files inline in one prompt with no tool loop, used by the orchestrator when the relevant set of files is small.

It inherits from `BaseAIAgent` (see [base_ai_agent.md](base_ai_agent.md)).

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `client` | `BaseAIClient` | the AI provider |
| `environment` | `BaseExecutionEnvironment` | for `describe()` injected into the system prompt |
| `workspace` | `BaseWorkspace` | required for file I/O |
| `max_retries` | `int = 5` | retry budget for AI calls |
| `on_event`, `on_status_message` | callbacks | standard callbacks |

## System prompt

`_process_system_prompt` returns:

```
GENERATE_SYSTEM_INSTRUCTIONS_PROMPT.format(
    evaluation_environment=self._environment.describe()
) + GENERATE_RETURN_FORMAT_PROMPT
```

So the prompt includes a `describe()`-formatted block listing allowed globals/builtins/modules and the timeout. The orchestrator may overwrite this via `set_system_prompt_override(...)` based on the `LanguageStrategy`.

## Public API

### Single-file modes

| Method | Behavior |
|--------|----------|
| `generate_only(prompt, filename) → AutocoderProcessResult` | One AI call, save the code, NO execution. Caller validates externally. |
| `generate(prompt, filename) → AutocoderProcessResult` | Generate, run via `environment.execute`, repair-on-failure up to `max_retries`. |
| `repair(previous_code, error_message, filename) → AutocoderProcessResult` | One repair pass — used by the orchestrator's collection-error fallback. |

### Multi-file modes

| Method | Behavior |
|--------|----------|
| `generate_multi(issue_description, target_files, architecture_context="", existing_code=None, test_files=None) → AutocoderProcessResult` | Tool loop with `AutocoderGenerateActionSchema` and terminal action `submit_code`. The LLM uses `view_file` / `list_directory` / `search_files` to explore the workspace, then submits multi-file changes. Detects language from extensions to swap system prompts. |
| `repair_multi(current_files, error_message, architecture_context="") → AutocoderProcessResult` | Same shape but with the repair schema; the LLM only sees a list of failing file paths. |
| `repair_multi_inline(source_files, test_files, error_message, readonly_context=None) → AutocoderProcessResult` | NO tool loop — all source/test/readonly files inlined in the prompt. Two-shot: system + user → analysis + changes. Used by the orchestrator when the file set is small enough. `readonly_context` files are reference-only and must not be modified. |

All multi-file methods return `AutocoderProcessResult` containing `changes: List[FileChange]`, optional `dependencies: List[str]`, and `test_scaffold: str` (carried from the LLM if present so the autotester can pick up scaffolding hints).

## Result type

```python
class AutocoderProcessResult(BaseModel):
    changes: List[FileChange]      # filepath, code, action
    dependencies: List[str] = []   # pip / npm packages declared by the LLM
    test_scaffold: str = ""        # unified gen+test scaffold hint
    output: Optional[Any] = None   # only set by single-file `generate`
```

## Helpers

| Helper | Purpose |
|--------|---------|
| `_normalize_call_spec(data)` | Coerces the LLM's `call_spec` to a real dict, defaults `symbol="main"` |
| `_extract_code_from_response(json_response)` | Handles both old format (`{"code": ...}`) and new (`{"changes": [...]}`) |
| `_parse_changes(json_response, known_files=None)` | Extracts `FileChange` objects, fixes double-escaped newlines, recovers hallucinated path prefixes by matching `endswith` against known files |
| `_generate_code(messages)` | 3-attempt LLM call with `GeneratePromptSchema` |
| `_repair_code(prev, err)` | 3-attempt LLM call with `RepairPromptSchema` |

## Example

```python
from bizniz.agents.autocoder.autocoder import Autocoder

autocoder = Autocoder(
    client=client,
    environment=docker_env,
    workspace=workspace,
    on_status_message=print,
)

result = autocoder.generate_multi(
    issue_description="Add a Calculator class with add/subtract/multiply/divide.",
    target_files=[
        {"filepath": "calc/calculator.py", "action": "create"},
    ],
    test_files=["tests/test_calculator.py"],
)

for change in result.changes:
    print(change.filepath, change.action)
```

## Interactions

- **Calls into:** `BaseAIClient.get_text` (with `JSON_SCHEMA` response format), `bizniz.tools.tool_loop.run_tool_loop`, `BaseWorkspace.{exists, read_file, write_file}`, `bizniz.utils.json.clean_llm_json`.
- **Called by:** `CodingOrchestrator.run` and `run_multi`. Also reachable directly for one-off generation.

## Gotchas

- **`generate` re-uses message history across retries.** That's why a JSON-parse error during retry triggers `clear_message_history()` to prevent token bloat (see `_generate_multi_code`).
- **`generate_only` does NOT execute.** It's the safe "let the orchestrator decide how to validate" mode. `generate` still exists for the legacy single-file flow.
- **Hallucinated paths get auto-recovered.** If the LLM emits `absolute/path/to/pet_groomer_backend/foo.py`, `_parse_changes(known_files=...)` looks for any `kf` such that the LLM's path ends with `/kf`. The first match wins.
- **Test scaffold pass-through.** When the autocoder's response includes a `test_scaffold`, the orchestrator caches it and feeds it into the autotester's regen step — keeps fixture / import shape consistent across iterations.
- **`describe()` lists allowed modules.** If your environment has nothing exposed (e.g. `DockerPytestEnvironment`), the prompt will say `Allowed modules: None`, which can confuse the LLM into thinking it can't import `pytest`. The orchestrator overrides the system prompt for non-Python languages, but for Python the `describe()` text is included verbatim.
- **`call_spec` is largely vestigial in multi-file mode.** It's only used by `generate` (single-file) — the orchestrator runs pytest directly and ignores any `call_spec` fields the LLM emits in multi-file mode.
