"""Deterministic anti-pattern scanner for the Refactorer agent.

Walks Python source (via ``ast``) and TypeScript/JavaScript source
(via regex — full TS parsing is overkill for the pattern set we
care about) and surfaces findings the Refactorer can then ask the
why-classifier (Phase D) about.

Patterns detected (Python):

- ``Base.metadata.drop_all`` calls in test files (the crm_v1 2026-
  05-16 incident — destructive teardown leaves prod DB tables-less)
- Bare ``except:`` and broad ``except Exception: pass`` (swallows
  every error including ``KeyboardInterrupt`` historically; ``pass``
  hides the cause)
- Hard-coded credentials (string literals matching ``password=``,
  ``api_key=``, ``secret=`` followed by a non-env-var literal)
- ``os.environ.clear()`` in tests (nukes other tests' env)
- ``shell=True`` in ``subprocess.run`` calls (command injection
  surface) — flagged for review, not auto-rewrite

Patterns detected (TypeScript/JavaScript):

- ``console.log`` in non-test source files (should use a real logger)
- Hard-coded credentials (same shape as Python)
- ``eval(...)`` calls (always smell)

This module produces a structured ``AntiPatternReport``; the
Refactorer's why-classifier (Phase D) consumes each finding and
decides whether to rewrite, surface, or leave alone.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Callable, Iterable, List, Literal, Optional, Set

from pydantic import BaseModel, Field


# ── Output schema ────────────────────────────────────────────────


Severity = Literal["critical", "warning", "info"]


class AntiPatternFinding(BaseModel):
    """One pattern detection."""
    pattern: str         # short tag, e.g. "drop_all_in_test"
    severity: Severity
    path: str            # absolute file path
    line: int            # 1-based line number
    snippet: str         # the offending line (truncated to ~200 chars)
    description: str     # human-readable "what + why this is bad"
    suggested_fix: Optional[str] = None


class AntiPatternReport(BaseModel):
    """All findings across the analyzed file set."""
    findings: List[AntiPatternFinding] = Field(default_factory=list)
    files_scanned: int = 0
    files_skipped: List[str] = Field(default_factory=list)

    def by_severity(self, severity: Severity) -> List[AntiPatternFinding]:
        return [f for f in self.findings if f.severity == severity]

    def by_pattern(self, pattern: str) -> List[AntiPatternFinding]:
        return [f for f in self.findings if f.pattern == pattern]


# ── Python AST visitor ───────────────────────────────────────────


_TEST_PATH_MARKERS: Set[str] = {"test", "tests", "conftest"}


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    if any(seg in _TEST_PATH_MARKERS for seg in parts):
        return True
    name = Path(path).name
    return name.startswith("test_") or name == "conftest.py"


def _snippet(text: str, line: int, max_chars: int = 200) -> str:
    lines = text.splitlines()
    if 0 < line <= len(lines):
        s = lines[line - 1].strip()
        return s[:max_chars]
    return ""


class _PythonVisitor(ast.NodeVisitor):
    """AST walker that records findings against the file."""

    def __init__(self, path: str, text: str):
        self.path = path
        self.text = text
        self.findings: List[AntiPatternFinding] = []
        self._is_test = _is_test_path(path)

    # ── Helpers ──────────────────────────────────────────────

    def _emit(
        self, node: ast.AST, pattern: str, severity: Severity,
        description: str, suggested_fix: Optional[str] = None,
    ) -> None:
        self.findings.append(AntiPatternFinding(
            pattern=pattern, severity=severity, path=self.path,
            line=getattr(node, "lineno", 0),
            snippet=_snippet(self.text, getattr(node, "lineno", 0)),
            description=description,
            suggested_fix=suggested_fix,
        ))

    @staticmethod
    def _attr_chain(node: ast.AST) -> List[str]:
        """Walk a chain like ``a.b.c`` → ``["a", "b", "c"]``."""
        chain: List[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            chain.append(cur.id)
        return list(reversed(chain))

    # ── Visitors ─────────────────────────────────────────────

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Catch references like ``Base.metadata.drop_all`` even when
        # they're passed as a callable (``conn.run_sync(
        # Base.metadata.drop_all)``) rather than called inline.
        chain = self._attr_chain(node)
        if len(chain) >= 2 and chain[-1] == "drop_all" and (
            "metadata" in chain or self._is_test
        ):
            self._emit(
                node, "drop_all_in_test"
                if self._is_test else "drop_all_call",
                "critical" if self._is_test else "warning",
                description=(
                    "``drop_all`` on a live database is destructive. "
                    "In test fixtures, this leaves the production DB "
                    "tables-less after the suite ends — see the "
                    "2026-05-16 crm_v1 M5 incident."
                ),
                suggested_fix=(
                    "Use transactional rollback (BEGIN/ROLLBACK per "
                    "test) via the skeleton's ``live_postgres_session`` "
                    "fixture instead of dropping tables."
                ),
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        chain = self._attr_chain(node.func)
        if not chain:
            # ``func`` was e.g. a Call result (chained call), Lambda,
            # Subscript — nothing to match against. Recurse children.
            self.generic_visit(node)
            return
        tail = chain[-2:] if len(chain) >= 2 else chain

        # os.environ.clear() — nukes everyone else's env, common
        # test-isolation mistake.
        if tail == ["environ", "clear"]:
            self._emit(
                node, "environ_clear_in_test",
                "critical" if self._is_test else "warning",
                description=(
                    "``os.environ.clear()`` mutates global state every "
                    "subsequent test inherits. Use ``monkeypatch.delenv`` "
                    "/ ``setenv`` (pytest) or ``unittest.mock.patch.dict("
                    "os.environ, ...)`` instead."
                ),
                suggested_fix=(
                    "Replace with scoped env mutation: monkeypatch in "
                    "pytest, or ``with patch.dict(os.environ, ..., "
                    "clear=True): ...``"
                ),
            )

        # subprocess shell=True — command injection surface.
        if chain[-1] in ("run", "Popen", "call", "check_call",
                         "check_output"):
            if "subprocess" in chain or (
                len(chain) >= 2 and chain[-2] == "subprocess"
            ):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(
                        kw.value, ast.Constant,
                    ) and kw.value.value is True:
                        self._emit(
                            node, "subprocess_shell_true",
                            "warning",
                            description=(
                                "``subprocess`` with ``shell=True`` is a "
                                "command-injection surface. Pass a list "
                                "of args instead so the shell never sees "
                                "user data."
                            ),
                            suggested_fix=(
                                "Replace with a list-of-args form: "
                                "``subprocess.run([cmd, arg1, arg2], "
                                "shell=False)``."
                            ),
                        )

        # eval() — almost always wrong.
        if chain == ["eval"]:
            self._emit(
                node, "eval_call", "warning",
                description=(
                    "``eval`` executes arbitrary strings as code. Almost "
                    "always replaceable with a safer alternative "
                    "(``ast.literal_eval`` for data, explicit parser for "
                    "expressions)."
                ),
                suggested_fix=(
                    "If you're parsing data: ``ast.literal_eval``. If "
                    "you're dispatching by name: an explicit dict or "
                    "match statement."
                ),
            )

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # Bare ``except:``  → catches everything including
        # KeyboardInterrupt / SystemExit.
        if node.type is None:
            self._emit(
                node, "bare_except", "critical",
                description=(
                    "Bare ``except:`` catches ``KeyboardInterrupt`` and "
                    "``SystemExit`` along with real errors. Almost "
                    "certainly not what you want."
                ),
                suggested_fix=(
                    "Catch ``Exception`` (or a more specific type) "
                    "instead: ``except Exception as e: ...``."
                ),
            )
        # Broad ``except Exception: pass`` → silently swallows.
        body_pass_only = (
            len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
        )
        if body_pass_only:
            # Identify the exception type. Bare except already flagged.
            if node.type is not None:
                type_str = ast.unparse(node.type) if hasattr(ast, "unparse") else "Exception"
                if type_str in ("Exception", "BaseException"):
                    self._emit(
                        node, "swallow_exception", "warning",
                        description=(
                            f"``except {type_str}: pass`` swallows every "
                            f"error without logging or remediation. Hides "
                            f"the root cause when something breaks."
                        ),
                        suggested_fix=(
                            "Log the exception (``log.exception(...)``) "
                            "and either re-raise or take a documented "
                            "recovery action."
                        ),
                    )
        self.generic_visit(node)


# ── Python file scanner ──────────────────────────────────────────


_CREDENTIAL_KEYWORDS = ("password", "api_key", "apikey", "secret",
                        "token", "private_key")
_ENV_GUARD = re.compile(r"os\.environ\b|getenv|env_file|settings\.|cfg\.|config\.")


def _scan_hardcoded_credentials(
    text: str, path: str,
) -> List[AntiPatternFinding]:
    """Regex-based credential-literal scan. Applies to Python and TS
    alike since the pattern shape is similar."""
    out: List[AntiPatternFinding] = []
    for ln, line in enumerate(text.splitlines(), 1):
        lower = line.lower()
        if not any(kw in lower for kw in _CREDENTIAL_KEYWORDS):
            continue
        # Skip if the line references an env-var lookup — usually
        # ``api_key = os.environ.get("API_KEY")`` etc.
        if _ENV_GUARD.search(line):
            continue
        # Match ``password = "abc123"`` style assignments.
        m = re.search(
            r"(?i)\b(" + "|".join(_CREDENTIAL_KEYWORDS) +
            r")\s*[=:]\s*[\"']([^\"']{4,})[\"']",
            line,
        )
        if m is None:
            continue
        secret = m.group(2)
        # Heuristic: skip obvious test-placeholder strings like
        # "password", "secret", "changeme" — those are intentional
        # placeholders not real credentials.
        if secret.lower() in {
            "password", "secret", "changeme", "test", "example",
            "your-key-here", "todo", "fill-in",
        }:
            continue
        out.append(AntiPatternFinding(
            pattern="hardcoded_credential",
            severity="critical",
            path=path, line=ln,
            snippet=line.strip()[:200],
            description=(
                f"Line appears to embed a literal {m.group(1)} value. "
                f"Credentials must come from env / a secrets manager."
            ),
            suggested_fix=(
                "Replace with an env-var lookup: "
                "``os.environ['<NAME>']`` or ``settings.<name>``."
            ),
        ))
    return out


def scan_python_file(path: str, text: Optional[str] = None) -> List[AntiPatternFinding]:
    """Scan one Python file, returning findings. ``text`` overrides
    disk read for tests."""
    if text is None:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return []
    visitor = _PythonVisitor(path=path, text=text)
    visitor.visit(tree)
    return visitor.findings + _scan_hardcoded_credentials(text, path)


# ── TypeScript/JavaScript scanner (regex-based) ──────────────────


_TS_CONSOLE_LOG = re.compile(r"\bconsole\.(log|debug|info|warn|error)\s*\(")
_TS_EVAL = re.compile(r"\beval\s*\(")


def scan_typescript_file(path: str, text: Optional[str] = None) -> List[AntiPatternFinding]:
    """Scan one TS/JS file, returning findings. Skips files in
    test directories (console output is normal there)."""
    if text is None:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    findings: List[AntiPatternFinding] = []
    is_test = _is_test_path(path)
    for ln, line in enumerate(text.splitlines(), 1):
        if not is_test and _TS_CONSOLE_LOG.search(line):
            findings.append(AntiPatternFinding(
                pattern="console_log_in_source",
                severity="info",
                path=path, line=ln,
                snippet=line.strip()[:200],
                description=(
                    "``console.log`` (or sibling levels) in production "
                    "source code. Use a structured logger instead so "
                    "output can be filtered, leveled, and shipped."
                ),
                suggested_fix=(
                    "Import the project's logger (``import { log } from "
                    "'ts_core/logger'`` once the shared logger is "
                    "extracted) and replace ``console.log`` with "
                    "``log.info`` / ``log.error``."
                ),
            ))
        if _TS_EVAL.search(line):
            findings.append(AntiPatternFinding(
                pattern="eval_call",
                severity="warning",
                path=path, line=ln,
                snippet=line.strip()[:200],
                description=(
                    "``eval`` executes arbitrary strings as code. "
                    "Almost always wrong; introduces a code-injection "
                    "surface."
                ),
                suggested_fix=(
                    "Replace with explicit parsing (``JSON.parse``, a "
                    "real expression evaluator) or a dispatch table."
                ),
            ))
    findings.extend(_scan_hardcoded_credentials(text, path))
    return findings


# ── Public entry point ───────────────────────────────────────────


def scan_files(
    paths: Iterable[str],
    file_reader: Optional[Callable[[str], str]] = None,
) -> AntiPatternReport:
    """Scan an iterable of file paths, dispatching to the language-
    appropriate scanner by extension. Unknown extensions land in
    ``files_skipped``.

    ``file_reader`` is injectable for tests — bypass disk I/O by
    passing a callable that maps path → text.
    """
    report = AntiPatternReport()
    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            text = file_reader(path) if file_reader else None
            report.findings.extend(scan_python_file(path, text=text))
            report.files_scanned += 1
        elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            text = file_reader(path) if file_reader else None
            report.findings.extend(scan_typescript_file(path, text=text))
            report.files_scanned += 1
        else:
            report.files_skipped.append(path)
    return report
