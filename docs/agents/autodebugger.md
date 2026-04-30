# QuickDebugger / QuickDebugger

`bizniz/agents/debugger/quick.py` (with backward-compat shim at `bizniz/quick_debugger/quick_debugger.py` exposing `QuickDebugger` as an alias for `QuickDebugger`).

## Purpose

The QuickDebugger is the cheap, fast diagnosis path for test failures. One LLM call, no tool loop. It scans the workspace for files that look related to the failure (via static import/regex analysis), then asks the AI to produce a structured diagnosis.

The result tells the orchestrator:

- What the root cause is (`diagnosis`).
- Whether to fix code or tests (`fix_target`).
- Which other files are relevant (`relevant_files`).
- A suggested approach (`suggested_approach`).

It inherits from both `BaseDebugger` (the abstract debugger interface) and `BaseAIAgent`.

## Constructor

| Parameter | Notes |
|-----------|-------|
| `client` | the AI provider |
| `environment` | for `BaseAIAgent`; not invoked here |
| `workspace` | required for file scanning |
| `max_retries` | `int = 5` |
| `on_event`, `on_status_message` | standard callbacks |

## Public API

### `diagnose(error_output, code, code_filename, test_code, test_filename) → QuickDebuggerDiagnosis`

Parameters:

| Name | Type | Notes |
|------|------|-------|
| `error_output` | `str` | full pytest output (stdout + stderr + traceback) |
| `code`, `code_filename` | `str`, `str` | the code module under test |
| `test_code`, `test_filename` | `str`, `str` | the failing test file |

Workflow:

1. List workspace files (excluding noisy dirs: `node_modules`, `__pycache__`, `.git`, `.bizniz`, `dist`, `build`, `.next`).
2. `_find_related_files(...)` extracts:
   - Modules referenced in `from X import Y` / `import X` statements (recursively followed).
   - File paths mentioned in the error output (`.py` regex).
   - Every `__init__.py` for any package referenced.
3. Truncate the error output to ~3000 chars (head + tail with `[truncated]` separator).
4. Single LLM call with `QuickDebuggerSchema`.
5. Parse `relevant_files` (handles both the array-of-objects and the dict-shape responses) and return `QuickDebuggerDiagnosis`.

## Result type

```python
class QuickDebuggerDiagnosis(BaseModel):
    diagnosis: str
    fix_target: Literal["code", "tests"]
    relevant_files: Dict[str, str] = {}   # filename → one-line summary
    suggested_approach: str
    affected_files: List[str] = []
```

## Helpers

| Helper | Purpose |
|--------|---------|
| `_find_related_files(error_output, code, test_code, code_filename, test_filename, workspace_files)` | Recursive import graph walk + traceback-path extraction |
| `_read_related_files(filenames)` | Reads each file from the workspace, swallows errors |
| `_get_diagnosis(user_prompt)` | 3-attempt LLM call |

## Example

```python
from bizniz.agents.debugger.quick import QuickDebugger
# Or: from bizniz.agents.debugger.quick import QuickDebugger

debugger = QuickDebugger(client=client, environment=env, workspace=ws)

diagnosis = debugger.diagnose(
    error_output=pytest_output,
    code=workspace.read_file("calc.py"),
    code_filename="calc.py",
    test_code=workspace.read_file("tests/test_calc.py"),
    test_filename="tests/test_calc.py",
)

print(diagnosis.fix_target)        # "code" or "tests"
print(diagnosis.suggested_approach)
```

## Interactions

- **Calls into:** `BaseAIClient.get_text` (`QuickDebuggerSchema`), `BaseWorkspace.{list_relative_files, read_file}`.
- **Called by:** `CodingOrchestrator._handle_failure_with_debugger` once per iteration to decide whether to repair code or tests.

## Gotchas

- **It's "quick" because it's one shot.** No tool calls, no iteration. For deep dives use the [AgenticDebugger](agentic_debugger.md).
- **Import scanning is regex-based.** `re.finditer(r'(?:from|import)\s+([\w.]+)', source)` — it's tolerant of weird formatting but won't catch dynamic imports (`importlib.import_module(...)`).
- **The path-from-traceback heuristic includes basename matching.** If the trace mentions `foo.py`, ANY workspace file ending in `/foo.py` is added to `related`. This can over-include in monorepos.
- **`QuickDebugger` is just an alias** kept for backward compatibility — new code should import `QuickDebugger`.
- **Diamond inheritance.** Both `BaseDebugger.__init__` and `BaseAIAgent.__init__` are called explicitly to satisfy both contracts.
