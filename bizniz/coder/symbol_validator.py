"""Deterministic symbol + import validator for newly-written code.

The "real win" of v2.5 — we run this AFTER the Coder writes code and
BEFORE it writes tests. Catches hallucinated imports cheaply (AST
walk, no LLM call) so the Coder can fix them before we waste tokens
generating tests against fake symbols.

This is NOT a full type-checker. It only verifies that imported names
resolve to one of:
  - Python stdlib modules
  - Third-party packages declared in requirements.txt / package.json
  - Local project modules (any .py file in the workspace)

If an import doesn't resolve to any of these, it's flagged. The Coder
gets the report back as a tool result and is forced to fix.

For TypeScript/Angular/React: deferred to a later pass. The TS
ecosystem has too many dynamic resolution paths (path aliases,
auto-imports) for a simple AST check; we'll need a real ts-morph
shell-out for that. Python is the immediate need (FastAPI backends).
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


# Python stdlib module names (3.10+). Approximation; missing modules
# will be flagged as "unresolved" — easy to add when first hit.
_STDLIB_MODULES: Set[str] = set(getattr(sys, "stdlib_module_names", set())) or {
    # Fallback for older Python; will be incomplete but covers basics.
    "os", "sys", "re", "json", "time", "datetime", "pathlib", "typing",
    "collections", "itertools", "functools", "io", "math", "random",
    "logging", "subprocess", "shutil", "tempfile", "urllib", "http",
    "asyncio", "concurrent", "threading", "multiprocessing", "socket",
    "ssl", "hashlib", "hmac", "base64", "uuid", "enum", "dataclasses",
    "abc", "contextlib", "warnings", "traceback", "inspect", "types",
    "ast", "csv", "argparse", "configparser", "copy", "decimal",
    "fractions", "statistics", "operator", "weakref", "gc", "atexit",
    "signal", "select", "struct", "binascii", "zipfile", "tarfile",
    "gzip", "bz2", "lzma", "pickle", "shelve", "sqlite3", "xml", "html",
    "email", "smtplib", "imaplib", "poplib", "ftplib", "telnetlib",
    "calendar", "locale", "string", "textwrap", "unicodedata", "codecs",
    "dis", "symtable", "tokenize", "keyword", "linecache", "imp",
    "importlib", "pkgutil", "modulefinder", "runpy",
}


@dataclass
class UnresolvedSymbol:
    """One unresolved import the validator flagged."""
    file: str
    line: int
    symbol: str
    kind: str  # "import" or "from-import"
    reason: str  # human-readable why it didn't resolve


@dataclass
class SymbolValidationReport:
    """Result of validating one or more files."""
    file_count: int = 0
    unresolved: List[UnresolvedSymbol] = field(default_factory=list)
    resolved_count: int = 0
    syntax_errors: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.unresolved and not self.syntax_errors

    def render(self) -> str:
        """Markdown for piping back into the Coder's tool-loop."""
        if self.passed:
            return (
                f"SYMBOL VALIDATION PASSED  "
                f"({self.file_count} file(s), "
                f"{self.resolved_count} import(s) resolved)"
            )
        lines = [
            f"SYMBOL VALIDATION FAILED  "
            f"({self.file_count} file(s), "
            f"{len(self.unresolved)} unresolved, "
            f"{len(self.syntax_errors)} syntax error(s))",
            "",
        ]
        if self.syntax_errors:
            lines.append("## Syntax errors")
            for s in self.syntax_errors:
                lines.append(f"  - {s}")
            lines.append("")
        if self.unresolved:
            lines.append("## Unresolved symbols")
            for u in self.unresolved:
                lines.append(
                    f"  - {u.file}:{u.line} [{u.kind}] `{u.symbol}` — {u.reason}"
                )
        return "\n".join(lines)


def _collect_third_party_packages(workspace_root: Path) -> Set[str]:
    """Read requirements.txt + pyproject.toml [project.dependencies]
    and return declared package names (lowercase, normalized)."""
    pkgs: Set[str] = set()
    req = workspace_root / "requirements.txt"
    if req.exists():
        for line in req.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            # Strip version specifiers + extras.
            name = line.split("[", 1)[0]
            for sep in ("==", ">=", "<=", "!=", "~=", ">", "<", ";"):
                name = name.split(sep, 1)[0]
            name = name.strip().lower().replace("-", "_")
            if name:
                pkgs.add(name)
    pyproject = workspace_root / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib
        except Exception:
            tomllib = None
        if tomllib is not None:
            try:
                data = tomllib.loads(pyproject.read_text())
                deps = (data.get("project") or {}).get("dependencies") or []
                for d in deps:
                    name = str(d).split("[", 1)[0]
                    for sep in ("==", ">=", "<=", "!=", "~=", ">", "<", ";"):
                        name = name.split(sep, 1)[0]
                    name = name.strip().lower().replace("-", "_")
                    if name:
                        pkgs.add(name)
            except Exception:
                pass
    return pkgs


def _collect_local_modules(workspace_root: Path) -> Set[str]:
    """Walk the workspace and return every importable module path
    (dotted), e.g., ``app.api.routes.auth``. Includes packages
    (any dir with __init__.py) and individual .py files.
    """
    modules: Set[str] = set()
    for py in workspace_root.rglob("*.py"):
        # Skip noise paths.
        rel = py.relative_to(workspace_root)
        parts = rel.parts
        if any(p in ("node_modules", ".venv", "__pycache__", ".git", "tests", "test")
               for p in parts):
            continue
        # Drop .py suffix; if file is __init__.py, the package = parent dir.
        if py.name == "__init__.py":
            mod = ".".join(parts[:-1])
        else:
            mod = ".".join(parts[:-1] + (py.stem,))
        if mod:
            modules.add(mod)
            # Also register all package prefixes so ``app.api`` resolves
            # even if only ``app/api/routes/x.py`` is on disk.
            cur = mod
            while "." in cur:
                cur = cur.rsplit(".", 1)[0]
                modules.add(cur)
    return modules


def _resolve(
    module: str,
    stdlib: Set[str],
    third_party: Set[str],
    local: Set[str],
) -> Optional[str]:
    """Return None if resolved, otherwise a human-readable reason."""
    if not module:
        return None  # relative import, can't statically resolve here
    head = module.split(".", 1)[0]
    norm_head = head.lower().replace("-", "_")
    if head in stdlib or norm_head in stdlib:
        return None
    if head in third_party or norm_head in third_party:
        return None
    if module in local:
        return None
    if head in local:
        return None
    # Common third-party shorthand that ships under different package names.
    aliases = {
        "yaml": "pyyaml",
        "jose": "python_jose",
        "jwt": "pyjwt",
        "dotenv": "python_dotenv",
        "PIL": "pillow",
        "cv2": "opencv_python",
        "sklearn": "scikit_learn",
    }
    aliased = aliases.get(head, "")
    if aliased and aliased in third_party:
        return None
    return (
        f"unresolved — not in stdlib, not in declared dependencies, "
        f"not a local module"
    )


def validate_python_file(
    file_path: Path,
    workspace_root: Path,
    *,
    stdlib: Optional[Set[str]] = None,
    third_party: Optional[Set[str]] = None,
    local_modules: Optional[Set[str]] = None,
) -> SymbolValidationReport:
    """Validate imports in a single Python file.

    The optional caches let the dispatcher build the dependency
    universe once and reuse across files in the same workspace.
    """
    report = SymbolValidationReport(file_count=1)
    if stdlib is None:
        stdlib = _STDLIB_MODULES
    if third_party is None:
        third_party = _collect_third_party_packages(workspace_root)
    if local_modules is None:
        local_modules = _collect_local_modules(workspace_root)

    if not file_path.exists():
        report.syntax_errors.append(f"{file_path}: file not found")
        return report

    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception as e:
        report.syntax_errors.append(f"{file_path}: read error — {e}")
        return report

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        report.syntax_errors.append(
            f"{file_path}:{e.lineno or 0}: SyntaxError — {e.msg}"
        )
        return report

    rel = str(file_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                reason = _resolve(alias.name, stdlib, third_party, local_modules)
                if reason is not None:
                    report.unresolved.append(UnresolvedSymbol(
                        file=rel, line=node.lineno,
                        symbol=alias.name, kind="import", reason=reason,
                    ))
                else:
                    report.resolved_count += 1
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — skip; package context determines resolution.
                report.resolved_count += 1
                continue
            module = node.module or ""
            reason = _resolve(module, stdlib, third_party, local_modules)
            if reason is not None:
                names = ", ".join(a.name for a in node.names)
                report.unresolved.append(UnresolvedSymbol(
                    file=rel, line=node.lineno,
                    symbol=f"from {module} import {names}",
                    kind="from-import", reason=reason,
                ))
            else:
                report.resolved_count += 1
    return report


def validate_files(
    file_paths: List[Path],
    workspace_root: Path,
) -> SymbolValidationReport:
    """Validate every file in ``file_paths``, sharing the dependency
    universe across them so the walk is one-pass.
    """
    stdlib = _STDLIB_MODULES
    third_party = _collect_third_party_packages(workspace_root)
    local = _collect_local_modules(workspace_root)
    overall = SymbolValidationReport()
    for f in file_paths:
        if not str(f).endswith(".py"):
            continue
        rep = validate_python_file(
            f, workspace_root,
            stdlib=stdlib, third_party=third_party, local_modules=local,
        )
        overall.file_count += rep.file_count
        overall.unresolved.extend(rep.unresolved)
        overall.resolved_count += rep.resolved_count
        overall.syntax_errors.extend(rep.syntax_errors)
    return overall
