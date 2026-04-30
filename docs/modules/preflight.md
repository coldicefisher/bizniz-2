# Preflight Validation

`bizniz/preflight/`. Language-aware structural checks that run between code generation and test execution.

## Purpose

Generated code commonly has structural problems that a human can spot before running anything: imports that target a module the LLM forgot to write, missing `__init__.py` files, relative imports that won't resolve once the workspace is mounted at `/workspace`, single-word "package" names that are obviously workspace modules being mistaken for PyPI packages, etc.

The preflight validators walk newly generated files BEFORE pytest runs and either:

- Auto-stub missing modules (writing a placeholder file the coder can fill in later).
- Rewrite known-bad imports (e.g. `from .foo import bar` → `from package.foo import bar`).
- Remove "shadow" files that conflict with package directories.
- Surface `ImportIssue` records for problems they can't auto-fix.
- Hand back a list of pip/npm packages they think the test container should install (`packages_to_install`).

## Files

| File | Class | Notes |
|------|-------|-------|
| `base_validator.py` | `BasePreflightValidator` | Abstract `validate(generated_files, declared_dependencies) -> PreflightResult` |
| `python_validator.py` | `PythonPreflightValidator` | Python; the most thorough validator (594 lines) |
| `typescript_validator.py` | `TypeScriptPreflightValidator` | TypeScript |
| `javascript_validator.py` | `JavaScriptPreflightValidator` | JavaScript (re-uses TS's Node builtins set) |
| `csharp_validator.py` | `CSharpPreflightValidator` | C# / .NET |
| `registry.py` | `_VALIDATORS` map + `get_validator(...)` | Routes language → validator |
| `types.py` | `PreflightResult`, `ImportIssue`, `AutoStub`, `ImportRewrite` | Result types |

## Registry API

```python
from bizniz.preflight.registry import get_validator, is_validated_language

validator = get_validator("python", workspace)   # PythonPreflightValidator
validator = get_validator("ts", workspace)       # alias → TypeScriptPreflightValidator
validator = get_validator("rust", workspace)     # None — Rust isn't validated
```

Aliases recognized: `py`, `ts`, `tsx`, `js`, `jsx`, `c#`, `cs`, `.net`, `dotnet`.

## Result type

```python
@dataclass
class ImportIssue:
    filepath: str
    import_name: str
    issue: str       # "missing_module" | "missing_init" | "missing_dependency"
    detail: str

@dataclass
class AutoStub:
    filepath: str
    content: str
    reason: str

@dataclass
class ImportRewrite:
    filepath: str
    old_import: str
    new_import: str

@dataclass
class PreflightResult:
    language: str
    issues: List[ImportIssue] = []
    stubs_created: List[AutoStub] = []
    import_rewrites: List[ImportRewrite] = []
    shadow_files_removed: List[str] = []
    packages_to_install: List[str] = []
    files_checked: int = 0

    @property
    def passed(self) -> bool: ...
    @property
    def issues_fixed(self) -> int: ...
    def summary(self) -> str: ...
```

## Python validator

`PythonPreflightValidator` (`python_validator.py`) is the workhorse:

- Parses each generated `.py` with `ast`.
- Resolves `from X import Y` against:
  - `_STDLIB_MODULES = sys.stdlib_module_names`.
  - The workspace tree (existing files + planned files in `generated_files`).
  - PyPI (HEAD request to `https://pypi.org/pypi/<name>/json`, cached via `lru_cache`, 3-second timeout).
- Writes `AutoStub` files for missing workspace modules so test imports don't hard-fail.
- Filters out **ambiguous names** like `utils`, `models`, `config`, `helpers`, `core`, `base`, `app`, `db`, `api` — these are almost always local modules the LLM forgot to write, so we prefer to stub them rather than `pip install` something with the same name from PyPI.
- Recognizes common aliases (`cv2` → `opencv-python`, `PIL` → `Pillow`, `bs4` → `beautifulsoup4`, `yaml` → `PyYAML`).

## TypeScript / JavaScript validators

Same shape, different parser. Regex-based import detection (`import ... from "..."`, `require(...)`, `import("...")` dynamic imports). Resolves with `_JS_EXTENSIONS = [".js", ".jsx", ".mjs", ".cjs"]` (TS adds `.ts`, `.tsx`) and `_INDEX_FILES`. Re-uses the Node.js builtins set from TS in JS to avoid duplication.

## C# validator

Tracks `using` statements + namespaces; checks `.csproj` package references. Less critical for the pipeline since C# isn't a primary language — kept for future expansion.

## Example

```python
from bizniz.preflight.registry import get_validator

validator = get_validator("python", workspace)
result = validator.validate(
    generated_files={
        "calc.py": "from utils import helper\n...",
    },
    declared_dependencies=["pytest"],
)

print(result.summary())
# Preflight (python): 1 files checked
#   Auto-fixed 1 issue(s):
#     + utils.py (auto-stub: imported by calc.py but missing)
```

## Interactions

- **Used by:** `CodingOrchestrator` immediately after the coder writes files and before pytest runs.
- **Calls into:** the workspace (read existing files, write stubs), `urllib` (PyPI lookups), `ast` / regex parsers.

## Gotchas

- **PyPI HEAD lookups are slow.** 3-second timeout per request, cached via `lru_cache(256)`. Big plans hit dozens of imports; the validator runs them in a `ThreadPoolExecutor` to parallelize.
- **Ambiguous names are NEVER pip-installed.** A workspace file named `utils.py` that's missing → stub. The validator only emits `packages_to_install` for things that look like real package names.
- **Stubbed files are valid Python.** They contain `pass` or a placeholder docstring; the coder fills them in on subsequent iterations or on the next issue.
- **Rewriting relative imports is non-reversible.** Once the validator turns `from .foo import bar` into `from pkg.foo import bar`, the original is gone. The `ImportRewrite` record is the only audit trail.
- **`get_validator` returns `None` for unvalidated languages.** The orchestrator uses this — `if validator: validator.validate(...)` — so adding a new language doesn't immediately require a validator.
- **Shadow file removal.** If both `foo.py` and `foo/__init__.py` exist, Python prefers the package, but pytest's import behavior can flip-flop. The validator removes the file in favor of the package.
