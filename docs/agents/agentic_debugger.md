# AgenticDebugger

`bizniz/agents/debugger/agentic.py` (with shim at `bizniz/agentic_debugger/agentic_debugger.py`).

## Purpose

The AgenticDebugger is the deep / expensive diagnosis path. Instead of one shot like the QuickDebugger, this agent runs an iterative tool-use conversation: it reads files (`view_file`), explores directories (`list_directory`), greps for patterns (`search_files`), runs commands (`run_command`), and re-runs tests (`run_tests`) until it understands the failure. It can also produce direct code fixes inline.

Unlike most other agents, this one does NOT inherit from `BaseAIAgent` — it manages its own message list because each diagnose session is fully self-contained (no cross-call history).

## Constructor

| Parameter | Type | Notes |
|-----------|------|-------|
| `client` | `BaseAIClient` | should be a dedicated client to avoid history contamination |
| `workspace` | `BaseWorkspace` | required |
| `environment` | `BaseExecutionEnvironment` | used for `run_tests` |
| `max_turns` | `int = 15` | hard cap on tool-use turns before forcing submission |
| `timeout_seconds` | `int = 600` | wall-clock cap |
| `on_status_message` | callback | standard |

## Public API

### `diagnose(error_output, source_files, test_files, architecture_context="", repair_history=None) → AgenticDiagnosis`

| Param | Type | Notes |
|-------|------|-------|
| `error_output` | `str` | full failure output |
| `source_files` | `Dict[str, str]` | filepath → content of files under test |
| `test_files` | `Dict[str, str]` | filepath → content of test files |
| `architecture_context` | `str` | optional plan summary |
| `repair_history` | `List[str]` | summaries of prior repair attempts |

Inner loop:

1. Build initial context message (workspace tree, source/test paths, error output, repair history).
2. Each turn, call the LLM with `AgenticDebuggerActionSchema`. Parse the action JSON. Dispatch on `action`:
   - `view_file` / `list_directory` / `search_files` — handled via `bizniz.tools.discovery_tools`.
   - `run_command` — `subprocess.run(command, shell=True, cwd=workspace.root, timeout=60)`. Output truncated to 10k chars.
   - `run_tests` — runs pytest on the supplied paths via `BaseExecutionEnvironment.execute`.
   - `submit_fix` — terminal action; returns `AgenticDiagnosis`.
   - Anything else — error string injected as a user message.
3. On timeout / max_turns / repeated parse failures, force a final `submit_fix` request.
4. Absolute fallback: a "could not determine" `AgenticDiagnosis` with `confidence=low`.

## Result type

```python
class CodeFix(BaseModel):
    filepath: str
    new_content: str

class AgenticDiagnosis(BaseModel):
    diagnosis: str = ""
    root_cause_category: str = ""
    fix_target: Literal["code", "tests", "both"] = "code"
    affected_files: List[str] = []
    fix_plan: List[str] = []
    suggested_approach: str = ""
    missing_packages: List[str] = []
    confidence: Literal["high", "medium", "low"] = "medium"
    code_fixes: List[CodeFix] = []
```

## Tool implementations (private)

| Method | Implementation |
|--------|----------------|
| `_tool_view_file(path)` | wraps `bizniz.tools.discovery_tools.tool_view_file` |
| `_tool_list_directory(path)` | wraps `tool_list_directory` |
| `_tool_search_files(pattern)` | wraps `tool_search_files` |
| `_tool_run_command(command)` | shell exec with 60s timeout, output capped at 10k chars |
| `_tool_run_tests(path)` | turns space-separated paths into absolute paths, runs `ExecutionCallSpec(symbol="pytest", args=...)` |

## Example

```python
from bizniz.agents.debugger.agentic import AgenticDebugger

debugger = AgenticDebugger(
    client=client,
    workspace=workspace,
    environment=docker_env,
    max_turns=12,
    timeout_seconds=300,
    on_status_message=print,
)

diagnosis = debugger.diagnose(
    error_output=pytest_failure,
    source_files={"calc.py": workspace.read_file("calc.py")},
    test_files={"tests/test_calc.py": workspace.read_file("tests/test_calc.py")},
    repair_history=["Attempt 1: fixed division-by-zero", "Attempt 2: still fails on negative inputs"],
)

if diagnosis.code_fixes:
    for fix in diagnosis.code_fixes:
        workspace.write_file(fix.filepath, fix.new_content)
```

## Interactions

- **Calls into:** `BaseAIClient.get_text` (`AgenticDebuggerActionSchema`), `bizniz.tools.discovery_tools.tool_*`, `subprocess.run`, `BaseExecutionEnvironment.execute`, `bizniz.utils.json.clean_llm_json`.
- **Called by:** `CodingOrchestrator._handle_failure_with_debugger` after the QuickDebugger has been called `agentic_debug_threshold` times (default 2) consecutively. The orchestrator typically uses the `debugger_factory` to construct one per session.

## Gotchas

- **Use a dedicated client.** The docstring is explicit: do not share with autocoder/autotester. The agent uses `use_message_history=False` on every call so the client's history doesn't contaminate, but the converse can also bite — long debugger conversations can poison a shared history if accidentally reused.
- **`run_command` is unrestricted shell access.** It runs with `shell=True` in the workspace root with a 60-second timeout. There's no allowlist. Don't expose this debugger to untrusted prompts.
- **Final submission is a forced single call.** When `max_turns` is exhausted, the loop appends "you must submit now" and makes one more LLM call. If THAT response isn't a `submit_fix`, the absolute fallback returns a low-confidence stub diagnosis.
- **Initial context excludes file contents.** The initial user message tells the LLM what files exist (paths only) and the error output. The LLM is expected to use `view_file` to read what it needs. This keeps the prompt small.
- **`workspace_root` for `run_command` is the SERVICE workspace.** If the agent needs to read sibling services' files, it can't get to them this way — they're in different workspaces.
- **`run_tests` accepts space-separated paths.** Pass `path="tests/test_a.py tests/test_b.py"` to run multiple, or just one path.
