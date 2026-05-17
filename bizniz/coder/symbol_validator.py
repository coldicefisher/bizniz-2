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
class UnresolvedAttribute:
    """One attribute access on a known class that referenced a
    non-existent field. v33 lesson: ``settings.fusionauth_application_id``
    when the real field is ``fusionauth_app_id``.
    """
    file: str
    line: int
    var: str        # the variable name (``settings``)
    class_name: str  # the class it resolves to (``Settings``)
    attribute: str  # the bad attribute (``fusionauth_application_id``)
    available: List[str]  # what the class actually has, for hints


@dataclass
class SymbolValidationReport:
    """Result of validating one or more files."""
    file_count: int = 0
    unresolved: List[UnresolvedSymbol] = field(default_factory=list)
    unresolved_attributes: List[UnresolvedAttribute] = field(default_factory=list)
    resolved_count: int = 0
    syntax_errors: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            not self.unresolved
            and not self.unresolved_attributes
            and not self.syntax_errors
        )

    def render(self) -> str:
        """Markdown for piping back into the Coder's tool-loop."""
        if self.passed:
            return (
                f"SYMBOL VALIDATION PASSED  "
                f"({self.file_count} file(s), "
                f"{self.resolved_count} import(s) resolved)"
            )
        problem_count = (
            len(self.unresolved)
            + len(self.unresolved_attributes)
            + len(self.syntax_errors)
        )
        lines = [
            f"SYMBOL VALIDATION FAILED  "
            f"({self.file_count} file(s), "
            f"{problem_count} problem(s))",
            "",
        ]
        if self.syntax_errors:
            lines.append("## Syntax errors")
            for s in self.syntax_errors:
                lines.append(f"  - {s}")
            lines.append("")
        if self.unresolved:
            lines.append("## Unresolved imports")
            for u in self.unresolved:
                lines.append(
                    f"  - {u.file}:{u.line} [{u.kind}] `{u.symbol}` — {u.reason}"
                )
            lines.append("")
        if self.unresolved_attributes:
            lines.append("## Unresolved attribute access")
            lines.append(
                "(Variable resolves to a known class; the attribute "
                "doesn't exist on it. v33 lesson — these are 500s "
                "waiting to happen.)"
            )
            for a in self.unresolved_attributes:
                avail = ", ".join(sorted(a.available)[:8])
                if len(a.available) > 8:
                    avail += f", ... ({len(a.available)} total)"
                lines.append(
                    f"  - {a.file}:{a.line} `{a.var}.{a.attribute}` — "
                    f"{a.class_name} has no field `{a.attribute}`. "
                    f"Available: {avail}"
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


@dataclass
class WorkspaceClassIndex:
    """Index of known classes in the workspace, used for attribute-
    access validation. Maps a fully-qualified class name to its
    declared field set (annotated assignments at class-body level)
    plus its declared base classes (for cross-class inheritance).

    Scope: catches the v33-class bug (``settings.fusionauth_application_id``
    when ``Settings`` only has ``fusionauth_app_id``). Doesn't try to
    be a type checker — only validates attribute access on Names that
    statically resolve to one of these classes.
    """
    # class_qualname (e.g. ``app.core.config.Settings``) → declared fields
    fields_by_class: Dict[str, Set[str]] = field(default_factory=dict)
    # class_qualname → list of base-class names (resolved at lookup time)
    bases_by_class: Dict[str, List[str]] = field(default_factory=dict)
    # short class name → list of qualnames that match (for import-without-qualname)
    qualnames_by_shortname: Dict[str, List[str]] = field(default_factory=dict)
    # (module, exported_name) → class_qualname (for ``settings = Settings()``
    # / ``settings: Settings = ...`` / ``def get_settings() -> Settings``)
    instance_class_by_module_export: Dict[tuple, str] = field(default_factory=dict)

    def fields_for(self, class_qualname: str) -> Set[str]:
        """Field set including inherited fields (best-effort; bases
        that aren't in the index just contribute nothing)."""
        seen: Set[str] = set()
        stack = [class_qualname]
        visited: Set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            seen |= self.fields_by_class.get(cur, set())
            for base in self.bases_by_class.get(cur, []):
                # Resolve short name to qualname best-effort.
                if base in self.fields_by_class:
                    stack.append(base)
                else:
                    qs = self.qualnames_by_shortname.get(base, [])
                    stack.extend(qs)
        return seen


def _build_class_index(workspace_root: Path) -> WorkspaceClassIndex:
    """Walk the workspace and index every class definition + its
    fields and base classes, plus module-level instances we can
    type-infer.

    Type-inference rules (intentionally narrow — catch the obvious
    cases, never guess):
      - ``name: ClassName = ...`` at module scope → name is ClassName
      - ``name = ClassName()`` at module scope → name is ClassName
      - ``name = some_function()`` at module scope where the
        function's return type is known → name is the return type
      - ``def name() -> ClassName: ...`` at module scope → calling
        the imported name returns ClassName

    Two-pass: first pass collects class defs + function return types
    so the second pass (which infers assignment types) has the full
    typing universe to chase through.
    """
    idx = WorkspaceClassIndex()
    # Pass 1: class defs + function return types only.
    parsed: List[tuple] = []  # (module, tree)
    for py in workspace_root.rglob("*.py"):
        rel = py.relative_to(workspace_root)
        parts = rel.parts
        if any(p in ("node_modules", ".venv", "__pycache__", ".git", "tests", "test")
               for p in parts):
            continue
        if py.name == "__init__.py":
            module = ".".join(parts[:-1])
        else:
            module = ".".join(parts[:-1] + (py.stem,))
        if not module:
            continue
        try:
            source = py.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py))
        except Exception:
            continue
        parsed.append((module, tree))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                qualname = f"{module}.{node.name}"
                fields: Set[str] = set()
                bases: List[str] = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        bases.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        bases.append(b.attr)
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        fields.add(item.target.id)
                    elif isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name) and not t.id.startswith("_"):
                                fields.add(t.id)
                idx.fields_by_class[qualname] = fields
                idx.bases_by_class[qualname] = bases
                idx.qualnames_by_shortname.setdefault(
                    node.name, [],
                ).append(qualname)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node.returns, ast.Name):
                    idx.instance_class_by_module_export[
                        (module, node.name + "()")
                    ] = node.returns.id

    # Pass 2: module-level assignments — now we can chase through
    # callables to their return types.
    for module, tree in parsed:
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if isinstance(node.annotation, ast.Name):
                    idx.instance_class_by_module_export[
                        (module, node.target.id)
                    ] = node.annotation.id
            elif isinstance(node, ast.Assign):
                if (
                    len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                ):
                    callee = node.value.func.id
                    target = node.targets[0].id
                    if callee in idx.qualnames_by_shortname:
                        # Direct class instantiation.
                        idx.instance_class_by_module_export[
                            (module, target)
                        ] = callee
                    else:
                        # Call to a function in same module — chase return type.
                        rt = idx.instance_class_by_module_export.get(
                            (module, callee + "()")
                        )
                        if rt:
                            idx.instance_class_by_module_export[
                                (module, target)
                            ] = rt
    return idx


def _validate_attributes_in_file(
    tree: ast.AST,
    file_path: Path,
    workspace_root: Path,
    class_index: WorkspaceClassIndex,
) -> List[UnresolvedAttribute]:
    """Walk the file's AST and flag attribute access on Names that
    statically resolve to a known class but reference a non-existent
    attribute.

    Tracks a per-file ``var_to_class`` map built from imports +
    local assignments. Intentionally narrow — only flags when we
    can confidently say the var's class.
    """
    # Build the file's view of "what class does each name resolve to?"
    # Map of var_name → class_qualname (or short class name if qualname
    # isn't disambiguating).
    var_to_class: Dict[str, str] = {}
    # Also remember which (module, name) pairs were imported so we
    # can chase typed module-level exports.
    imported_modules: Dict[str, str] = {}  # local_name → source_module

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (node.level or 0) == 0:
            for alias in node.names:
                local_name = alias.asname or alias.name
                src_module = node.module
                imported_modules[local_name] = src_module
                # Look up typed module exports.
                key = (src_module, alias.name)
                cls = class_index.instance_class_by_module_export.get(key)
                if cls:
                    var_to_class[local_name] = cls
                # Also handle direct class imports — ``from x import
                # ClassName`` — instantiating it locally later.
                if alias.name in class_index.qualnames_by_shortname:
                    # Just record the short name; we'll handle
                    # instantiation in the Assign walk below.
                    pass

    # Walk module-level + function-body assignments. Limit to the
    # outermost statements + function bodies — class-body fields
    # are already captured by the index.
    def _visit_stmts(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if isinstance(stmt.annotation, ast.Name):
                    var_to_class[stmt.target.id] = stmt.annotation.id
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                rhs = stmt.value
                if isinstance(rhs, ast.Call) and isinstance(rhs.func, ast.Name):
                    callee = rhs.func.id
                    # ``var = ClassName()`` — directly instantiating.
                    if callee in class_index.qualnames_by_shortname:
                        var_to_class[stmt.targets[0].id] = callee
                    else:
                        # ``var = imported_callable()`` — check if
                        # the callable's source module declares a
                        # return type for that name.
                        src = imported_modules.get(callee)
                        if src:
                            cls = class_index.instance_class_by_module_export.get(
                                (src, callee + "()")
                            )
                            if cls:
                                var_to_class[stmt.targets[0].id] = cls
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _visit_stmts(stmt.body)

    _visit_stmts(tree.body if isinstance(tree, ast.Module) else [])

    # Now walk all Attribute accesses; flag ones we can prove are wrong.
    flagged: List[UnresolvedAttribute] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        var = node.value.id
        cls = var_to_class.get(var)
        if not cls:
            # ``ClassName.attr`` — direct class-level access. SQLAlchemy
            # query builders use this pattern (``select(User).where(
            # User.user_id == ...)``). The class lookup-by-shortname is
            # the same set of qualnames as ``fields_for`` reads.
            # Only consider names imported in this file (so we don't
            # flag random locals that happen to match a class name).
            if (
                var in imported_modules
                or any(
                    var == name
                    for name in class_index.qualnames_by_shortname
                )
            ) and var in class_index.qualnames_by_shortname:
                cls = var
            else:
                continue
        # Resolve cls to a qualname (or list of qualnames).
        qualnames = class_index.qualnames_by_shortname.get(cls, [])
        if not qualnames:
            # Class was imported but isn't in the index (probably an
            # external class like BaseSettings). Skip — we can't say.
            continue
        # If multiple classes share the short name, only flag when
        # ALL of them lack the attribute. Avoids false positives in
        # ambiguous cases.
        for qn in qualnames:
            fs = class_index.fields_for(qn)
            if node.attr in fs:
                break  # at least one match — fine
        else:
            # No match in any candidate class.
            # Skip if the class's field set is empty — likely an
            # external class with no introspectable fields (e.g.
            # we picked up the import but not the source).
            unioned: Set[str] = set()
            for qn in qualnames:
                unioned |= class_index.fields_for(qn)
            if not unioned:
                continue
            flagged.append(UnresolvedAttribute(
                file=str(file_path),
                line=node.lineno,
                var=var,
                class_name=cls,
                attribute=node.attr,
                available=sorted(unioned),
            ))
    return flagged


def validate_python_file(
    file_path: Path,
    workspace_root: Path,
    *,
    stdlib: Optional[Set[str]] = None,
    third_party: Optional[Set[str]] = None,
    local_modules: Optional[Set[str]] = None,
    class_index: Optional[WorkspaceClassIndex] = None,
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

    # Attribute-access validation — only run when the class index was
    # supplied (validate_files supplies it). Skipping here when None
    # keeps single-file callers cheap.
    if class_index is not None:
        report.unresolved_attributes.extend(
            _validate_attributes_in_file(tree, file_path, workspace_root, class_index)
        )
    return report


def validate_files(
    file_paths: List[Path],
    workspace_root: Path,
) -> SymbolValidationReport:
    """Validate every file in ``file_paths``, sharing the dependency
    universe + class index across them so the walks are one-pass.
    """
    stdlib = _STDLIB_MODULES
    third_party = _collect_third_party_packages(workspace_root)
    local = _collect_local_modules(workspace_root)
    class_index = _build_class_index(workspace_root)
    overall = SymbolValidationReport()
    for f in file_paths:
        if not str(f).endswith(".py"):
            continue
        rep = validate_python_file(
            f, workspace_root,
            stdlib=stdlib, third_party=third_party, local_modules=local,
            class_index=class_index,
        )
        overall.file_count += rep.file_count
        overall.unresolved.extend(rep.unresolved)
        overall.unresolved_attributes.extend(rep.unresolved_attributes)
        overall.resolved_count += rep.resolved_count
        overall.syntax_errors.extend(rep.syntax_errors)
    return overall
