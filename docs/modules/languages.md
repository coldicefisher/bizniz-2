# Languages

`bizniz/languages/`. The strategy pattern that makes the orchestrator language-agnostic.

## Why a strategy

The orchestrator runs the same loop for Python, TypeScript, and (eventually) other languages. The differences live in the strategy:

- Test command (`pytest` vs `jest`).
- File-extension checks (`.py` vs `.ts`/`.tsx`).
- Markdown code-fence tag.
- System prompts (the right tool to use, the right import style, the right test framework).
- Standard-library knowledge (so we don't try to pip-install `os`).
- Project file detection (`pyproject.toml` vs `package.json`).
- Installed-package readout.

## Files

| File | Class | Notes |
|------|-------|-------|
| `base.py` | `LanguageStrategy` | Abstract base — every strategy must satisfy this contract |
| `python.py` | `PythonStrategy` | The default; system prompts are tuned for FastAPI / pytest |
| `typescript.py` | `TypeScriptStrategy` | Jest + tsconfig; system prompts tuned for ts-jest |
| `__init__.py` | `get_language_strategy(name)` | Falls back to `PythonStrategy` for unknown languages |

## `LanguageStrategy` contract

```python
class LanguageStrategy(ABC):
    @property name -> str
    @property test_symbol -> str           # "pytest" / "jest"
    @property code_fence_lang -> str       # "python" / "typescript"
    @property language_prefix -> str       # "" for default; "TypeScript:" otherwise

    def is_test_file(self, filepath) -> bool
    def strip_extension(self, filepath) -> str
    def scan_imports(self, files: dict) -> Set[str]
    def filter_third_party(self, imports, workspace_modules) -> Set[str]
    def get_coder_system_prompt(self, evaluation_environment="") -> str
    def get_tester_system_prompt(self) -> str
    def get_tester_user_prompt(self) -> str
    def is_stdlib(self, module_name) -> bool
    def detect_project_file(self, workspace) -> bool
    def get_installed_packages(self, workspace) -> str
```

## `PythonStrategy`

| Property / method | Returns |
|-------------------|---------|
| `name` | `"python"` |
| `test_symbol` | `"pytest"` |
| `code_fence_lang` | `"python"` |
| `language_prefix` | `""` |
| `is_test_file(fp)` | `fp.startswith("tests/")` or `fp.endswith("_test.py")` |
| `strip_extension(fp)` | strips `.py` |
| `is_stdlib(name)` | matches `sys.stdlib_module_names` (with a hardcoded fallback list for older Pythons) |
| `detect_project_file(ws)` | `ws.exists("pyproject.toml")` or `setup.py` |
| `get_installed_packages(ws)` | reads `requirements.txt` if present, else returns the dependencies block from `pyproject.toml` |

`get_coder_system_prompt` returns the Python coder prompt template — emphasizing absolute imports, modifying stub files (not creating new ones), and exporting via `__init__.py`. The template has a `{evaluation_environment}` placeholder that the orchestrator fills with the env's `describe()` output.

`get_tester_system_prompt` returns the pytest-flavored test-writing prompt.

## `TypeScriptStrategy`

| Property / method | Returns |
|-------------------|---------|
| `name` | `"typescript"` |
| `test_symbol` | `"jest"` |
| `code_fence_lang` | `"typescript"` |
| `language_prefix` | `"TypeScript:"` |
| `is_test_file(fp)` | matches `.test.ts`, `.spec.tsx`, etc. |
| `strip_extension(fp)` | strips `.ts`, `.tsx`, `.test.ts`, etc. |
| `is_stdlib(name)` | Node.js builtins (`fs`, `path`, `crypto`, ...) |
| `detect_project_file(ws)` | `ws.exists("package.json")` |
| `get_installed_packages(ws)` | reads `dependencies` + `devDependencies` from `package.json` |

System prompts target Jest + ts-jest, ESM + CommonJS interop, and `tsconfig.json`-aware imports.

## `get_language_strategy`

```python
from bizniz.languages import get_language_strategy

py = get_language_strategy("python")
ts = get_language_strategy("typescript")
fallback = get_language_strategy("rust")  # Falls back to PythonStrategy
```

Used by the orchestrator (`bizniz/orchestrator/coding_orchestrator.py`):

```python
from bizniz.languages import get_language_strategy
self._lang = get_language_strategy(language)
```

## Example

```python
from bizniz.languages import get_language_strategy

strat = get_language_strategy("typescript")
strat.test_symbol     # "jest"
strat.is_test_file("src/tests/foo.test.ts")  # True
strat.strip_extension("src/tests/foo.test.ts")  # "src/tests/foo"
```

## Interactions

- **Used by:** `CodingOrchestrator` for everything language-conditional. `Coder.generate_multi` and `Tester.generate_multi` also pull system prompts via the language strategy.
- **Calls into:** `bizniz.tools.discovery_prompt.DISCOVERY_TOOLS_PROMPT` (appended to system prompts).

## Gotchas

- **Unknown languages fall back to Python.** If you spell `"rust"` and forget to register a strategy, the orchestrator silently uses Python, which will probably fail in confusing ways.
- **`language_prefix` is purely cosmetic.** It's prepended to some prompts so the model knows what language we're in. The actual behavior is determined by the system prompt and test command.
- **`is_test_file` is conservative.** A test in `src/util.test.ts` is a test, but `src/test_util.ts` (singular, no underscore prefix) is treated as source. Adjust your service's conventions accordingly.
- **`get_installed_packages` is a flat string.** It's injected into the orchestrator's prompt as installed-package context. It doesn't try to lock or reconcile versions.
- **The orchestrator overrides system prompts via `set_system_prompt_override(...)`.** That overrides whatever the coder/tester would default to. The strategy's `get_coder_system_prompt` is the source of truth for non-Python languages.
