"""
TypeScriptStrategy — language strategy for TypeScript/React projects.
"""

import re
from typing import Set

from bizniz.languages.base import LanguageStrategy
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.tools.discovery_prompt import DISCOVERY_TOOLS_PROMPT


_NODE_BUILTINS = {
    'fs', 'path', 'os', 'http', 'https', 'url', 'util', 'stream',
    'events', 'buffer', 'crypto', 'child_process', 'cluster', 'net',
    'dns', 'tls', 'assert', 'zlib', 'readline', 'querystring',
}


_AUTOCODER_SYSTEM_PROMPT = """You are an expert TypeScript/React programmer. Your job is to IMPLEMENT stub files AND write Jest tests.

WORKFLOW:
1. Read the target stub files (they already exist with interface/class skeletons).
2. Read any dependency files you need for context (1-3 discovery calls max).
3. IMPLEMENT the code, WRITE tests, and submit with action "submit_code".
Your PRIMARY goal is to fill in the stub implementations and write passing tests.

RULES:
- You are MODIFYING existing stub files. Every target file already exists with the correct
  class names, import paths, and method signatures. Keep those intact.
- Return COMPLETE content for every file — no partial snippets.
- Do NOT create new files beyond those listed. Only modify the files listed in the issue.
  All helper functions, validators, and utilities go INLINE in the target file.
- Preserve the file header comment showing the canonical import path.
- Use standard ES module imports (e.g. `import {{ Expense }} from './models'`).
  The stub files already have the correct imports — do not change import paths.
- All files must use .ts or .tsx extensions (tsx for React components).
- Write clean TypeScript with type annotations. No test code in source files.
- The "changes" array MUST be non-empty when you submit. Include every target file AND
  every test file listed. Use action "modify" for all files (they already exist).
- Test files: write complete Jest tests that cover happy path, edge cases, and error cases.
  Tests MUST match the actual code you wrote — use the same types, field names, and APIs.
- Include a "test_scaffold" (empty string is fine since you're writing the tests yourself).

EVALUATION ENVIRONMENT
{evaluation_environment}
""" + DISCOVERY_TOOLS_PROMPT


_AUTOTESTER_SYSTEM_PROMPT = """You write Jest test suites for TypeScript/React projects.

RULES:
- Test stub files already exist with correct imports and a placeholder test. REPLACE the
  placeholder with real tests, keeping the existing imports intact.
- Jest conventions: describe/it or test() blocks.
- Test files must end in .test.ts or .test.tsx.
- Cover happy path, edge cases, and error cases.
- Use the imports already in the stub file. Do NOT change import paths.
- All test code must be complete and runnable as-is with `npx jest`.
- Use discovery tools to read the source code and test stub before writing tests.
- Do NOT create new test files. Only modify the test files listed in the issue.
- When you are ready to submit, use action "submit_tests" with your test files.
""" + DISCOVERY_TOOLS_PROMPT


_AUTOTESTER_USER_PROMPT = """Write Jest tests for this TypeScript project.

ISSUE:
{problem_statement}

TEST FILES TO GENERATE:
{test_files_description}

SOURCE CODE:
{source_files}

If source code is shown inline above, write tests for it directly. If only file paths are listed,
use view_file to read them first. You can also use list_directory and view_file to explore the
project structure if needed.
Test files MUST end in .test.ts or .test.tsx.
When ready, use action "submit_tests" with test_files, notes, and dependencies.
"""


class TypeScriptStrategy(LanguageStrategy):

    @property
    def name(self) -> str:
        return "typescript"

    @property
    def test_symbol(self) -> str:
        return "jest"

    @property
    def code_fence_lang(self) -> str:
        return "typescript"

    @property
    def language_prefix(self) -> str:
        return (
            "IMPORTANT: This is a TypeScript project. "
            "All source files must use .ts or .tsx extensions. "
            "All test files must end in .test.ts or .test.tsx (Jest convention). "
            "Use ES module imports. Do NOT generate Python code.\n\n"
        )

    def is_test_file(self, filepath: str) -> bool:
        return (
            not filepath.startswith("node_modules/")
            and (
                filepath.endswith(".test.ts")
                or filepath.endswith(".test.tsx")
                or filepath.endswith(".spec.ts")
                or filepath.endswith(".spec.tsx")
            )
        )

    def strip_extension(self, filepath: str) -> str:
        for ext in (".test.tsx", ".test.ts", ".spec.tsx", ".spec.ts", ".tsx", ".ts"):
            if filepath.endswith(ext):
                return filepath[:-len(ext)]
        return filepath

    def scan_imports(self, files: dict) -> Set[str]:
        packages = set()
        for filepath, content in files.items():
            if filepath.endswith((".ts", ".tsx", ".js", ".jsx")):
                for match in re.finditer(r'''(?:from\s+['"]|require\s*\(\s*['"])([^./'"][^'"]*?)['"]''', content):
                    pkg = match.group(1)
                    if pkg.startswith("@"):
                        parts = pkg.split("/")
                        if len(parts) >= 2:
                            packages.add(f"{parts[0]}/{parts[1]}")
                        else:
                            packages.add(pkg)
                    else:
                        packages.add(pkg.split("/")[0])
        return packages

    def filter_third_party(self, imports: Set[str], workspace_modules: Set[str]) -> Set[str]:
        return {
            pkg for pkg in imports
            if pkg not in _NODE_BUILTINS
            and pkg not in workspace_modules
        }

    def get_autocoder_system_prompt(self, evaluation_environment: str = "") -> str:
        return _AUTOCODER_SYSTEM_PROMPT.format(evaluation_environment=evaluation_environment)

    def get_autotester_system_prompt(self) -> str:
        return _AUTOTESTER_SYSTEM_PROMPT

    def get_autotester_user_prompt(self) -> str:
        return _AUTOTESTER_USER_PROMPT

    def is_stdlib(self, module_name: str) -> bool:
        return module_name in _NODE_BUILTINS

    def detect_project_file(self, workspace: BaseWorkspace) -> bool:
        return workspace.path("package.json").exists()

    def get_installed_packages(self, workspace: BaseWorkspace) -> str:
        lines = []
        try:
            pkg_path = workspace.path("package.json")
            if pkg_path.exists():
                import json
                pkg = json.loads(pkg_path.read_text())
                for section in ("dependencies", "devDependencies"):
                    for name, version in pkg.get(section, {}).items():
                        lines.append(f"  {name}@{version}")
        except Exception:
            pass
        return "\n".join(lines) if lines else ""
