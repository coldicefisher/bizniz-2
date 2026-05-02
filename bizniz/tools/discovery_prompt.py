"""
Shared system prompt appendix for discovery tools.

Appended to every agent's system prompt (except dumb repair) to describe
the available discovery tools and where to find architecture/engineering docs.
"""

DISCOVERY_TOOLS_PROMPT = """

## Discovery Tools

You have access to discovery tools to explore the workspace before producing your final output. Use these to read files, search code, and understand the project structure. Do NOT guess file contents — use the tools.

### view_file
Read the contents of any file in the workspace.
Set `action` to `"view_file"` and `path` to the file path (relative to workspace root).

### list_directory
List files in a directory, or use `"."` for the full workspace tree.
Set `action` to `"list_directory"` and `path` to the directory path.

### search_files
Search for a regex pattern across all Python files in the workspace.
Set `action` to `"search_files"` and `path` to the regex pattern.

### search_imports
Find where a symbol (function, class, variable) is defined in the workspace.
Returns the full function signature, docstring, and correct import path.
Set `action` to `"search_imports"` and `path` to the symbol name (e.g. `"get_current_user"`).
Use this BEFORE guessing import paths — it tells you exactly where to import from.

### list_all_imports
List every importable symbol in a specific module with full signatures.
Set `action` to `"list_all_imports"` and `path` to the module path (e.g. `"app.core.auth"`).
Use this to discover what a module offers before importing from it.

## Discoverable Context

The following documentation is available in the workspace and can be read via `view_file`:
- `docs/engineering.md` — engineering analysis: requirements, use cases, architecture plan, issues
- `docs/architecture.md` — system-level architecture (if this is a multi-service project)
- `setup.py` or `pyproject.toml` — package configuration and dependencies
- `requirements.txt` — installed packages

Use `list_directory(".")` to see the full project structure before starting work.

## Turn Budget

You have a limited number of exploration turns. Use them wisely:
1. Start with `list_directory(".")` to understand the project layout
2. Use `view_file` to read specific files you need
3. Use `search_files` to find symbols, imports, or patterns
4. Submit your final output when ready

You may submit immediately on turn 1 if you have enough context. Do not explore unnecessarily.
"""
