# Tools

`bizniz/tools/`. Discovery tools and the agentic tool-use conversation loop.

## Files

| File | Purpose |
|------|---------|
| `discovery_tools.py` | `tool_view_file`, `tool_list_directory`, `tool_search_files`, plus `build_filtered_file_tree` |
| `discovery_prompt.py` | The text appendix that tells the LLM how to use the discovery tools |
| `schemas.py` | `build_tool_action_schema(...)` helper that constructs strict JSON schemas |
| `tool_loop.py` | `run_tool_loop(...)` — the iterative agent loop |

## Discovery tools

Three primitives shared by every agentic agent (autocoder, autotester, agentic debugger):

```python
def tool_view_file(workspace, path) -> str
def tool_list_directory(workspace, path) -> str
def tool_search_files(workspace, pattern) -> str
```

| Tool | Behavior |
|------|----------|
| `view_file` | Read up to 500 lines; append `... (truncated, N total lines)` if larger |
| `list_directory` | List files under a prefix, or the full tree (with exclusions) when no path given |
| `search_files` | Grep-like search across the workspace |

Constants in `discovery_tools.py`:

- `TREE_EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git", ".bizniz", "dist", "build", ".next"}`
- `TREE_MAX_FILES = 50`
- `build_filtered_file_tree(workspace)` — produces the workspace tree string used in agent initial-context messages.

## Schema builder

`build_tool_action_schema(name, terminal_action, terminal_properties, terminal_required, extra_actions=None)` builds an OpenAI strict-mode JSON schema with:

- A `thinking` field (the model's chain-of-thought).
- An `action` enum (always includes `view_file`, `list_directory`, `search_files`, plus `terminal_action`, plus any `extra_actions`).
- A `path` field used by all discovery tools.
- The terminal action's properties merged in.
- All properties required, `additionalProperties: false` (OpenAI strict-mode requirements).

This is how `AutocoderGenerateActionSchema`, `AutotesterGenerateActionSchema`, `AgenticDebuggerActionSchema` are all built.

## `run_tool_loop`

`bizniz/tools/tool_loop.py:run_tool_loop(...)` — the shared conversation loop used by every agentic agent.

| Parameter | Purpose |
|-----------|---------|
| `client` | `BaseAIClient` |
| `workspace` | for the discovery tools |
| `system_prompt` | full system prompt (agent-specific + discovery appendix) |
| `initial_user_message` | the task |
| `action_schema` | the JSON schema (from `build_tool_action_schema(...)`) |
| `terminal_action` | name that signals "I'm done" — e.g. `submit_code`, `submit_tests`, `submit_fix` |
| `max_turns` | default 10 |
| `timeout_seconds` | default 300 |
| `extra_tool_handlers` | dict of additional action name → handler `(action_dict, messages) -> str` |
| `agent_name` | for log prefixing |

Returns: the parsed action dict from the terminal-action turn.

Loop semantics:

1. Each turn, call the LLM with `JSON_SCHEMA` mode and the action schema.
2. If the response is the terminal action, return it.
3. If it's a discovery action, call the matching `tool_*` and append the result.
4. If it's an `extra_tool_handlers` action, dispatch.
5. Inject "turn budget" warnings when ≤4 / ≤2 turns remain.
6. On timeout / max turns, force a final submission attempt.
7. Up to 3 final-submission retries on context-length errors (trim oldest tool turns and retry).

Errors:

| Class | When |
|-------|------|
| `ToolLoopError` | base class |
| `ToolLoopTimeoutError` | exhausted retries, never got terminal action |
| `ToolLoopBadResponseError` | repeated parse failures or context-too-large with no trim possible |

## Robustness features

- **Rate-limit handling.** Catches `OpenAIRateLimit`, parses `try again in Xs`, sleeps, retries. Escalating backoff if still rate-limited.
- **Context-length trimming.** Catches `AIContextLengthExceeded`, drops the oldest tool-use pair, retries (helper: `_trim_messages_for_context`).
- **JSON parse retries.** On bad JSON, appends a "your response was not valid JSON" user message and tries again.
- **Final forced submission.** When turns are exhausted, the loop appends "you must submit now" and tries up to 3 more times, trimming context on overflow.

## Example

```python
from bizniz.tools.tool_loop import run_tool_loop
from bizniz.tools.schemas import build_tool_action_schema

schema = build_tool_action_schema(
    name="my_agent_action",
    terminal_action="submit_answer",
    terminal_properties={
        "answer": {"type": "string"}
    },
    terminal_required=["answer"],
)

action = run_tool_loop(
    client=client,
    workspace=workspace,
    system_prompt="You are a code reader. Use tools to find the answer.",
    initial_user_message="What does the function `foo` in src/foo.py do?",
    action_schema=schema,
    terminal_action="submit_answer",
    max_turns=8,
    on_status_message=print,
    agent_name="MyAgent",
)
print(action["answer"])
```

## Interactions

- **Used by:** `Autocoder.generate_multi`, `Autocoder.repair_multi`, `Autotester.generate_multi`, and (for its own loop, but with the same discovery primitives) `AgenticDebugger.diagnose`.
- **Calls into:** `BaseAIClient.get_text`, the workspace.

## Gotchas

- **`use_message_history=False` is always passed to the client.** The tool loop manages its own message list and doesn't want the client to inject history.
- **Discovery tools are read-only.** `view_file` / `list_directory` / `search_files` never write. The terminal action is the only way the loop produces side effects.
- **Turn budget warnings can confuse weak models.** Cheap models sometimes interpret the warning as "you must submit now" even with 4 turns left, and skip exploration. If you see this, raise `agentic_debug_threshold` or `max_turns`.
- **Trimming preserves the system + initial user.** `_trim_messages_for_context` only drops middle pairs. If your system + initial user is already too long, the loop fails with `ToolLoopBadResponseError`.
- **Extra tool handlers receive `(action_dict, messages)` and return a `str`.** The string becomes the next user message body. They can mutate `messages` in place if they need to inject extra context.
- **`build_filtered_file_tree` caps at `TREE_MAX_FILES`.** Big workspaces are truncated; if the LLM needs to see more, it has to use `list_directory` with a prefix.
