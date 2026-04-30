# Autotester

`bizniz/autotester/autotester.py`. Generates and reviews test suites.

## Purpose

The autotester writes test files for code the autocoder produces. It supports three single-file modes plus a multi-file tool-loop mode that mirrors the autocoder's `generate_multi`.

It inherits from `BaseAIAgent`.

## Constructor

| Parameter | Notes |
|-----------|-------|
| `client` | the AI provider |
| `environment` | not actually invoked by this agent; required for `BaseAIAgent` |
| `workspace` | required |
| `max_retries` | `int = 5` |
| `on_event`, `on_status_message` | standard callbacks |

## Public API

### Single-file modes

| Mode | Method | Description |
|------|--------|-------------|
| 1 | `process_from_code(code_path, output_path)` | Read existing code at `code_path`, look up its problem statement (workspace DB → file metadata), ask AI for tests. Used after the autocoder has already produced source. |
| 2 | `process_from_prompt(prompt, output_path, code_filename=None)` | TDD mode. Given only a problem statement, generate contract tests that any correct implementation must pass. `code_filename` is used to derive the import statement. |
| 3 | `review_tests(code_path, test_path, output_path)` | Read both the existing code and existing tests, ask the AI to strengthen the tests with edge cases / better assertions. |

All three return `AutotesterResult(test_files=[GeneratedTestFile(filepath, tests)], mode, success)`.

### Multi-file mode

`generate_multi(problem_statement, test_files, source_code=None, architecture_context="") → AutotesterResult`

Uses `bizniz.tools.tool_loop.run_tool_loop` with `AutotesterGenerateActionSchema` and terminal action `submit_tests`. Small source files (under 4000 chars) are inlined in the prompt; larger ones are listed as "use `view_file` to read".

The system prompt is selected by file extension: TypeScript test paths (`.test.ts`, `.spec.tsx`, etc) get the TS system prompt, otherwise Python.

## Result type

```python
class GeneratedTestFile(BaseModel):
    filepath: str
    tests: str

class AutotesterResult(BaseModel):
    test_files: List[GeneratedTestFile] = []
    dependencies: List[str] = []
    mode: Literal["from_code", "from_prompt", "review"]
    success: bool
    error: Optional[str] = None
```

## Helpers

| Helper | Purpose |
|--------|---------|
| `_lookup_problem_statement(code_path)` | First tries `workspace.db.get_context_for_code_file(code_path)`, then falls back to file metadata via `read_code_metadata` |
| `_generate_tests(user_prompt, mode)` | 3-attempt LLM call with `AutotesterSchema`. Handles both new (`test_files: [{filepath, tests}]`) and old (`tests: "..."`) response shapes. Fixes double-escaped newlines. |
| `_save_tests(tests, output_path, on_save_tests=None)` | Writes via `workspace.write_file` |
| `_update_callbacks(on_event, on_status_message)` | Refresh callbacks per public-method call |
| `_generate_multi_tests(user_prompt)` | Underlying multi-file LLM call (unused by `generate_multi` — replaced by tool loop, but still present as a fallback path) |

## Example

```python
from bizniz.autotester.autotester import Autotester

tester = Autotester(client=client, environment=env, workspace=workspace)

# Mode 2: contract tests from a spec only
result = tester.process_from_prompt(
    prompt="Convert roman numerals to integers (1..3999).",
    output_path="tests/test_roman.py",
    code_filename="roman.py",
)
print(result.test_files[0].filepath, result.test_files[0].tests[:120])
```

## Interactions

- **Calls into:** `BaseAIClient.get_text` (`AutotesterSchema`), `run_tool_loop` (`AutotesterGenerateActionSchema`), `workspace.read_file/write_file/db.get_context_for_code_file`, `bizniz.utils.code_metadata.read_code_metadata`.
- **Called by:** `CodingOrchestrator` in both TDD (`process_from_prompt` first) and CODE_FIRST (`process_from_prompt` after code) flows. Also called when a pytest collection error triggers a regeneration in the orchestrator.

## Gotchas

- **Mode 2's import line depends on `code_filename`.** If you pass `code_filename=None`, the prompt template defaults `module_name` to `"solution"` and tests will say `from solution import ...`. The orchestrator always passes the real code filename to avoid this.
- **The DB lookup wins over file metadata.** This matters when a code file was rewritten by a later issue; the metadata block at the top of the file may carry the original problem statement, but `WorkspaceDB.get_context_for_code_file` returns the current issue's context.
- **`process_from_prompt` doesn't read the source.** It writes contract tests against an interface only. If you want the autotester to look at existing code, use `process_from_code` or `review_tests`.
- **`generate_multi` may inline OR defer.** Small source files are inlined; large ones become "view_file" hints. The choice is per-file and based on `len(content) < 4000`.
- **The orchestrator regenerates tests on collection errors.** When pytest exits with code 2 or 4 in the orchestrator's loop, `process_from_prompt` is called with a regen prompt that includes the current source code and the parse error.
- **`success=True` in the result is set unconditionally if no exception is raised.** A successful AI response with empty tests will fail at `_save_tests` (and the autotester still raises). But "success" doesn't mean the tests pass — only that a file was generated and saved.
