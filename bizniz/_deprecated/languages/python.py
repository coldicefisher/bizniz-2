"""
PythonStrategy — language strategy for Python projects.
"""

import re
import sys
from typing import Set

from bizniz._deprecated.languages.base import LanguageStrategy
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.tools.discovery_prompt import DISCOVERY_TOOLS_PROMPT


_STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else {
    'abc', 'aifc', 'argparse', 'array', 'ast', 'asynchat', 'asyncio', 'asyncore',
    'atexit', 'audioop', 'base64', 'bdb', 'binascii', 'binhex', 'bisect',
    'builtins', 'bz2', 'calendar', 'cgi', 'cgitb', 'chunk', 'cmath', 'cmd',
    'code', 'codecs', 'codeop', 'collections', 'colorsys', 'compileall',
    'concurrent', 'configparser', 'contextlib', 'contextvars', 'copy', 'copyreg',
    'cProfile', 'crypt', 'csv', 'ctypes', 'curses', 'dataclasses', 'datetime',
    'dbm', 'decimal', 'difflib', 'dis', 'distutils', 'doctest', 'email',
    'encodings', 'enum', 'errno', 'faulthandler', 'fcntl', 'filecmp', 'fileinput',
    'fnmatch', 'formatter', 'fractions', 'ftplib', 'functools', 'gc', 'getopt',
    'getpass', 'gettext', 'glob', 'grp', 'gzip', 'hashlib', 'heapq', 'hmac',
    'html', 'http', 'idlelib', 'imaplib', 'imghdr', 'imp', 'importlib', 'inspect',
    'io', 'ipaddress', 'itertools', 'json', 'keyword', 'lib2to3', 'linecache',
    'locale', 'logging', 'lzma', 'mailbox', 'mailcap', 'marshal', 'math',
    'mimetypes', 'mmap', 'modulefinder', 'multiprocessing', 'netrc', 'nis',
    'nntplib', 'numbers', 'operator', 'optparse', 'os', 'ossaudiodev',
    'pathlib', 'pdb', 'pickle', 'pickletools', 'pipes', 'pkgutil', 'platform',
    'plistlib', 'poplib', 'posix', 'posixpath', 'pprint', 'profile', 'pstats',
    'pty', 'pwd', 'py_compile', 'pyclbr', 'pydoc', 'queue', 'quopri', 'random',
    're', 'readline', 'reprlib', 'resource', 'rlcompleter', 'runpy', 'sched',
    'secrets', 'select', 'selectors', 'shelve', 'shlex', 'shutil', 'signal',
    'site', 'smtpd', 'smtplib', 'sndhdr', 'socket', 'socketserver', 'sqlite3',
    'sre_compile', 'sre_constants', 'sre_parse', 'ssl', 'stat', 'statistics',
    'string', 'stringprep', 'struct', 'subprocess', 'sunau', 'symtable', 'sys',
    'sysconfig', 'syslog', 'tabnanny', 'tarfile', 'telnetlib', 'tempfile',
    'termios', 'test', 'textwrap', 'threading', 'time', 'timeit', 'tkinter',
    'token', 'tokenize', 'trace', 'traceback', 'tracemalloc', 'tty', 'turtle',
    'turtledemo', 'types', 'typing', 'unicodedata', 'unittest', 'urllib', 'uu',
    'uuid', 'venv', 'warnings', 'wave', 'weakref', 'webbrowser', 'winreg',
    'winsound', 'wsgiref', 'xdrlib', 'xml', 'xmlrpc', 'zipapp', 'zipfile',
    'zipimport', 'zlib', '_thread',
}

_TEST_BUILTINS = {'pytest', 'unittest', 'doctest', 'mock'}


_CODER_SYSTEM_PROMPT = """You are an expert Python programmer. Your job is to IMPLEMENT stub files AND write pytest tests.

WORKFLOW:
1. Read the target stub files (they already exist with class/function skeletons).
2. Read any dependency files you need for context (1-3 discovery calls max).
3. IMPLEMENT the code, WRITE tests, and submit with action "submit_code".
Your PRIMARY goal is to fill in the stub implementations and write passing tests.

RULES:
- You are MODIFYING existing stub files. Every target file already exists with the correct
  class names, import paths, and method signatures. Keep those intact.
- Return COMPLETE content for every target file — no partial snippets.
- Do NOT create new files beyond those listed. Only modify the files listed in the issue.
  All helper functions, validators, and utilities go INLINE in the target file.
- Preserve the module docstring showing the canonical import path.
- Use ABSOLUTE imports (e.g. `from pet_groomer.models import Expense`), never relative imports.
  The stub files already have the correct imports — do not change import paths.
- Ensure __init__.py files export the public API.
- Write clean Python with type hints. No test code in source files.
- The "changes" array MUST be non-empty when you submit. Include every target file AND
  every test file listed. Use action "modify" for all files (they already exist).
- Test files: write complete pytest tests that cover happy path, edge cases, and error cases.
  Tests MUST match the actual code you wrote — use the same types, field names, and APIs.
- Include a "test_scaffold" (empty string is fine since you're writing the tests yourself).

EVALUATION ENVIRONMENT
{evaluation_environment}
""" + DISCOVERY_TOOLS_PROMPT


_TESTER_SYSTEM_PROMPT = """You write pytest test suites for multi-file Python projects.

RULES:
- Test stub files already exist with correct imports and a placeholder test. REPLACE the
  placeholder with real tests, keeping the existing imports intact.
- pytest conventions: test functions named test_*, fixtures where appropriate.
- Cover happy path, edge cases, and error cases.
- Use the imports already in the stub file. Do NOT change import paths.
- All test code must be complete and runnable as-is with `pytest`.
- Always include `import pytest` at the top.
- Use discovery tools to read the source code and test stub before writing tests.
- Do NOT create new test files. Only modify the test files listed in the issue.
- When you are ready to submit, use action "submit_tests" with your test files.
""" + DISCOVERY_TOOLS_PROMPT


_TESTER_USER_PROMPT = """Write pytest tests for this project.

ISSUE:
{problem_statement}

TEST FILES TO GENERATE:
{test_files_description}

SOURCE CODE:
{source_files}

If source code is shown inline above, write tests for it directly. If only file paths are listed,
use view_file to read them first. You can also use list_directory and view_file to explore the
project structure if needed.
When ready, use action "submit_tests" with test_files, notes, and dependencies.
"""


class PythonStrategy(LanguageStrategy):

    @property
    def name(self) -> str:
        return "python"

    @property
    def test_symbol(self) -> str:
        return "pytest"

    @property
    def code_fence_lang(self) -> str:
        return "python"

    @property
    def language_prefix(self) -> str:
        return ""

    def is_test_file(self, filepath: str) -> bool:
        return (
            filepath.startswith("tests/")
            and filepath.endswith(".py")
            and filepath != "tests/__init__.py"
        )

    def strip_extension(self, filepath: str) -> str:
        return filepath.replace(".py", "")

    def scan_imports(self, files: dict) -> Set[str]:
        packages = set()
        for filepath, content in files.items():
            if filepath.endswith(".py"):
                for match in re.finditer(r'^(?:from|import)\s+(\w+)', content, re.MULTILINE):
                    packages.add(match.group(1))
        return packages

    def filter_third_party(self, imports: Set[str], workspace_modules: Set[str]) -> Set[str]:
        return {
            pkg for pkg in imports
            if pkg not in _STDLIB_MODULES
            and pkg not in _TEST_BUILTINS
            and pkg not in workspace_modules
            and not pkg.startswith("_")
        }

    def get_coder_system_prompt(self, evaluation_environment: str = "") -> str:
        return _CODER_SYSTEM_PROMPT.format(evaluation_environment=evaluation_environment)

    def get_tester_system_prompt(self) -> str:
        return _TESTER_SYSTEM_PROMPT

    def get_tester_user_prompt(self) -> str:
        return _TESTER_USER_PROMPT

    def is_stdlib(self, module_name: str) -> bool:
        return module_name in _STDLIB_MODULES

    def detect_project_file(self, workspace: BaseWorkspace) -> bool:
        return workspace.path("requirements.txt").exists()

    def get_installed_packages(self, workspace: BaseWorkspace) -> str:
        lines = []
        try:
            req_path = workspace.path("requirements.txt")
            if req_path.exists():
                for line in req_path.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("-"):
                        lines.append(f"  {line}")
        except Exception:
            pass
        return "\n".join(lines) if lines else ""
