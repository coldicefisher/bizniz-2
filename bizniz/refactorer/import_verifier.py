"""Deterministic import-resolution check (D19 / v3 refactorer Step 4a).

After an extraction lands, before paying for the full test run,
verify that every import in the affected files actually resolves.
This catches the most common "extraction broke the codebase"
failure mode (import path wrong, missing __init__.py, typo in
module name) in seconds instead of in the much slower test-run
cycle.

**Scope:** Python only. AST-walk imports, resolve each against the
project's sys.path layout.

**Approach:** static — we don't actually import the modules
(import-time side effects could lock files, spawn subprocesses,
hit the DB). We resolve module names to file paths using
sys.path-style rules:

  - For ``from foo.bar import baz``: find ``foo/bar.py`` or
    ``foo/bar/__init__.py``, then check that ``baz`` is defined
    inside.
  - For ``import foo.bar``: same resolution but no symbol check.
  - For ``from .foo import bar``: relative — resolve against the
    importing module's package.

**Search paths** (in order):

  1. The directory CONTAINING the importing file (for relative
     and local-package imports).
  2. Every directory in ``additional_search_paths`` — typically
     ``<project>/core/python/`` and ``<project>/<service>/`` for
     the service-rooted layout the skeleton uses.
  3. Standard library / installed packages — NOT checked. We
     can't tell at this layer whether ``import fastapi`` would
     succeed in the container; that's what the test run is for.
     Anything not under the project's controlled paths is
     assumed-OK.

**Output:** ``ImportVerifierReport`` with one ``ImportProblem``
per failing import. Empty problems list = "all imports look OK
within the project surface; proceed to test run."
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Set, Tuple

from pydantic import BaseModel, Field


class ImportProblem(BaseModel):
    """One import that couldn't be resolved."""
    file_path: str = Field(..., description="The file containing the broken import.")
    line: int = Field(..., description="Line of the import statement.")
    statement: str = Field(..., description="The import as written.")
    reason: str = Field(..., description="Why it failed to resolve.")


class ImportVerifierReport(BaseModel):
    """End-of-check summary."""
    problems: List[ImportProblem] = Field(default_factory=list)
    files_checked: int = 0
    imports_checked: int = 0

    @property
    def passed(self) -> bool:
        return not self.problems


class ImportVerifier:
    """Resolves Python imports statically against project paths.

    ``search_roots`` should include any directory that's on
    ``PYTHONPATH`` inside the running container. For bizniz
    projects that's typically::

        <project>/core/python    → mounted as /python_core
        <project>/<service>      → mounted as /app

    Imports that resolve to anything outside ``search_roots``
    (third-party packages, stdlib) are skipped — we can't verify
    those from here.
    """

    def __init__(
        self,
        search_roots: List[Path],
        package_roots: Optional[List[Path]] = None,
    ) -> None:
        # Project-controlled roots — we'll try to resolve module
        # names against these.
        self._search_roots = [Path(p) for p in search_roots]
        # Subset of search_roots that are also Python packages
        # (have __init__.py). Used to bound relative-import
        # walks. Defaults to ``search_roots``.
        self._package_roots = (
            [Path(p) for p in package_roots]
            if package_roots is not None
            else list(self._search_roots)
        )

    # ── Public ─────────────────────────────────────────────────────

    def verify_files(
        self, file_paths: List[Path],
    ) -> ImportVerifierReport:
        """Check imports in the given files. Each file walked once."""
        report = ImportVerifierReport()
        for path in file_paths:
            path = Path(path)
            if not path.exists() or path.suffix != ".py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            report.files_checked += 1
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as e:
                report.problems.append(ImportProblem(
                    file_path=str(path),
                    line=e.lineno or 0,
                    statement="<parse error>",
                    reason=f"SyntaxError: {e.msg}",
                ))
                continue
            for problem in self._walk_imports(path, tree):
                report.imports_checked += 1
                if problem is not None:
                    report.problems.append(problem)
            # Count successful imports too (so imports_checked is
            # the total, not just failures).
            report.imports_checked += sum(
                1 for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))
                and not self._is_irrelevant(n)
            )
        # _walk_imports counted only failures; add successes.
        # (Simpler — recompute total below.)
        report.imports_checked = self._count_relevant_imports(file_paths)
        return report

    # ── Internals ──────────────────────────────────────────────────

    def _walk_imports(
        self, file_path: Path, tree: ast.AST,
    ):
        """Yield an ``ImportProblem`` per failing import; None
        skipped imports."""
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if self._is_irrelevant(node):
                continue
            for problem in self._check_import_node(file_path, node):
                yield problem

    def _check_import_node(
        self,
        file_path: Path,
        node,
    ):
        """Resolve a single Import / ImportFrom node."""
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not self._resolve_module(alias.name):
                    # Could be third-party — only flag if it LOOKS
                    # like a project-local module (path-shaped).
                    if self._looks_project_local(alias.name):
                        yield ImportProblem(
                            file_path=str(file_path),
                            line=node.lineno,
                            statement=f"import {alias.name}",
                            reason=(
                                f"module {alias.name!r} not found "
                                f"under any project search root"
                            ),
                        )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level
            if level > 0:
                # Relative import — resolve against file's package.
                resolved_module = self._resolve_relative(
                    file_path, level, module,
                )
                if resolved_module is None:
                    yield ImportProblem(
                        file_path=str(file_path),
                        line=node.lineno,
                        statement=(
                            f"from {'.' * level}{module} import ..."
                        ),
                        reason=(
                            f"relative import resolves outside any "
                            f"package root"
                        ),
                    )
                    return
                module = resolved_module
            if not module:
                return
            target_path = self._resolve_module(module)
            if target_path is None:
                if self._looks_project_local(module):
                    yield ImportProblem(
                        file_path=str(file_path),
                        line=node.lineno,
                        statement=(
                            f"from {module} import "
                            f"{', '.join(a.name for a in node.names)}"
                        ),
                        reason=(
                            f"module {module!r} not found under any "
                            f"project search root"
                        ),
                    )
                return
            # Module resolved. Verify each imported symbol exists
            # in it (best-effort — ImportFrom targets can also
            # name submodules, which we accept without checking
            # contents).
            target_symbols = self._symbols_in_file(target_path)
            for alias in node.names:
                name = alias.name
                if name == "*":
                    continue
                # Symbol exists? Submodule exists?
                if name in target_symbols:
                    continue
                # Submodule check: target_path is foo/__init__.py
                # and ``name`` is foo/<name>.py or foo/<name>/.
                if target_path.name == "__init__.py":
                    pkg_dir = target_path.parent
                    if (pkg_dir / f"{name}.py").exists():
                        continue
                    if (pkg_dir / name / "__init__.py").exists():
                        continue
                yield ImportProblem(
                    file_path=str(file_path),
                    line=node.lineno,
                    statement=f"from {module} import {name}",
                    reason=(
                        f"name {name!r} not defined in module "
                        f"{module!r}"
                    ),
                )

    def _resolve_module(self, dotted_name: str) -> Optional[Path]:
        """Return the .py / __init__.py for ``dotted_name`` under
        any search root, or None if not found."""
        parts = dotted_name.split(".")
        for root in self._search_roots:
            # Try root/parts[0]/parts[1]/.../<last>.py
            module_file = root.joinpath(*parts).with_suffix(".py")
            if module_file.exists():
                return module_file
            # Try root/parts[0]/.../<last>/__init__.py
            pkg_file = root.joinpath(*parts) / "__init__.py"
            if pkg_file.exists():
                return pkg_file
        return None

    def _resolve_relative(
        self, file_path: Path, level: int, module: str,
    ) -> Optional[str]:
        """Convert ``from ..foo.bar import x`` (level=2, module="foo.bar")
        into an absolute dotted name based on ``file_path``'s package.

        Returns None when the relative walk escapes every known
        package root.
        """
        # Find which search root contains file_path; the dotted
        # package path is the path-from-root.
        for root in self._package_roots:
            try:
                rel = file_path.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.parts[:-1])  # drop filename
            # Walk up `level - 1` for relative imports (level=1 means
            # current package).
            if level - 1 > len(parts):
                return None  # escapes the root
            if level - 1 > 0:
                parts = parts[: -(level - 1)]
            if module:
                parts.extend(module.split("."))
            return ".".join(parts) if parts else None
        return None

    def _symbols_in_file(self, path: Path) -> Set[str]:
        """Return the set of top-level names defined in ``path``."""
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError):
            return set()
        names: Set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    names.add(alias.asname or alias.name)
        return names

    @staticmethod
    def _is_irrelevant(node) -> bool:
        """Skip imports we explicitly don't check. Conditional /
        nested imports often guard for runtime version differences
        — flagging them is noisy."""
        return False  # placeholder for future filters

    def _looks_project_local(self, dotted_name: str) -> bool:
        """Heuristic: an unresolved name is a project-local import
        if its top-level segment matches one of the well-known
        bizniz core/service prefixes. Conservative — when in
        doubt, don't flag (the test run will catch genuine bugs)."""
        head = dotted_name.split(".", 1)[0]
        return head in {
            "app", "python_core", "ts_core",
        }

    def _count_relevant_imports(self, file_paths: List[Path]) -> int:
        """Total import statements across all files, for the
        report's counter. Cheap walk."""
        total = 0
        for path in file_paths:
            try:
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    total += 1
        return total
