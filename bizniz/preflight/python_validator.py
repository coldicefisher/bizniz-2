"""Pre-flight validator for Python projects."""

import ast
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List, Set, Tuple

from bizniz.preflight.base_validator import BasePreflightValidator
from bizniz.preflight.types import AutoStub, ImportIssue, PreflightResult


# Python stdlib modules (available since 3.10)
_STDLIB_MODULES: Set[str] = set(sys.stdlib_module_names)

# Common packages that are often sub-imported but whose top-level
# is the pip install name (e.g. "from pydantic import BaseModel")
_COMMON_ALIASES: Dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "gi": "PyGObject",
    "attr": "attrs",
    "dotenv": "python-dotenv",
}

# Well-known third-party packages whose import name matches their pip name.
# These should never be auto-stubbed — they just need to be pip-installed.
_WELL_KNOWN_PACKAGES: Set[str] = {
    "pydantic", "fastapi", "flask", "django", "sqlalchemy", "celery",
    "redis", "requests", "httpx", "uvicorn", "gunicorn", "starlette",
    "pytest", "numpy", "pandas", "scipy", "matplotlib", "torch",
    "tensorflow", "alembic", "jinja2", "click", "typer", "rich",
    "boto3", "stripe", "jwt", "passlib", "bcrypt", "cryptography",
    "aiohttp", "websockets", "motor", "pymongo", "psycopg2", "asyncpg",
}


class PythonPreflightValidator(BasePreflightValidator):
    """Validates Python import resolution and auto-stubs missing modules."""

    language = "python"

    def validate(
        self,
        generated_files: Dict[str, str],
        declared_dependencies: List[str],
    ) -> PreflightResult:
        result = PreflightResult(language=self.language)

        # Build map of all files in workspace + generated
        workspace_files = self._get_workspace_files()
        all_files = {**workspace_files, **generated_files}

        # Normalize declared deps to top-level package names
        dep_names = self._normalize_dep_names(declared_dependencies)

        # Remove workspace files that shadow known packages (e.g. pydantic.py)
        self._remove_shadow_files(all_files, generated_files, dep_names, result)

        # Normalize all relative imports to absolute before validation
        self._normalize_relative_imports(generated_files, result)
        all_files.update(generated_files)

        for filepath, content in generated_files.items():
            if not filepath.endswith(".py"):
                continue
            result.files_checked += 1

            imports = self._extract_imports(filepath, content)
            for module, is_relative, level in imports:
                issue = self._check_import(
                    filepath, module, is_relative, level, all_files, dep_names
                )
                if issue:
                    stub = self._auto_stub(issue, all_files, dep_names)
                    if stub:
                        result.stubs_created.append(stub)
                        # Add stub to all_files so subsequent checks see it
                        all_files[stub.filepath] = stub.content
                    else:
                        result.issues.append(issue)

        # Check for missing __init__.py files
        init_stubs = self._check_init_files(all_files)
        for stub in init_stubs:
            if stub.filepath not in all_files:
                result.stubs_created.append(stub)
                all_files[stub.filepath] = stub.content

        return result

    def _remove_shadow_files(
        self,
        all_files: Dict[str, str],
        generated_files: Dict[str, str],
        dep_names: Set[str],
        result: "PreflightResult",
    ) -> None:
        """Detect and remove files that shadow known packages.

        A top-level file like ``pydantic.py`` shadows the real pydantic
        package and causes cascading ImportErrors.  This removes such files
        from both the file maps and the workspace on disk.
        """
        shadow_candidates = []
        for filepath in list(all_files.keys()):
            # Only top-level .py files can shadow packages
            if "/" in filepath or not filepath.endswith(".py"):
                continue
            stem = filepath[:-3]  # strip .py
            stem_lower = stem.lower().replace("-", "_")
            if (
                stem in _STDLIB_MODULES
                or stem in _COMMON_ALIASES
                or stem_lower in _WELL_KNOWN_PACKAGES
                or stem_lower in dep_names
            ):
                shadow_candidates.append(filepath)

        for filepath in shadow_candidates:
            all_files.pop(filepath, None)
            generated_files.pop(filepath, None)
            result.shadow_files_removed.append(filepath)
            # Remove from workspace on disk
            try:
                self._workspace.delete_file(filepath)
            except Exception:
                pass  # best effort — file may not exist on disk

    def _get_workspace_files(self) -> Dict[str, str]:
        """Get all existing files in the workspace as {path: content}."""
        files = {}
        for rel_path in self._workspace.list_relative_files():
            rel = str(rel_path)
            if rel.endswith(".py"):
                try:
                    files[rel] = self._workspace.read_file(path=rel)
                except Exception:
                    files[rel] = ""  # File exists but can't read — still counts
        return files

    def _normalize_relative_imports(
        self, generated_files: Dict[str, str], result: "PreflightResult"
    ) -> None:
        """Rewrite all relative imports to absolute in generated files.

        Modifies generated_files in-place. This eliminates the entire class
        of wrong-level relative import bugs (e.g. ``from ..models`` when
        ``from ...models`` was needed).
        """
        from bizniz.preflight.types import ImportRewrite

        for filepath, content in list(generated_files.items()):
            if not filepath.endswith(".py"):
                continue
            try:
                tree = ast.parse(content, filename=filepath)
            except SyntaxError:
                continue

            # Compute the package of this file (its parent directory as dotted path)
            fp_parts = PurePosixPath(filepath).parts
            if len(fp_parts) < 2:
                continue  # top-level file, no package context for relative imports
            pkg_parts = list(fp_parts[:-1])  # directory parts

            new_content = content
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                level = node.level or 0
                if level == 0:
                    continue  # already absolute

                module = node.module or ""

                # Resolve: go up (level - 1) from the package
                trim = level - 1
                if trim > len(pkg_parts):
                    continue  # invalid relative import, skip
                if trim == 0:
                    base_parts = pkg_parts
                else:
                    base_parts = pkg_parts[:-trim]

                absolute = ".".join(base_parts)
                if module:
                    absolute = f"{absolute}.{module}"

                # Build the old import text to replace
                dots = "." * level
                old_text = f"from {dots}{module} import"
                new_text = f"from {absolute} import"

                if old_text in new_content:
                    new_content = new_content.replace(old_text, new_text, 1)
                    result.import_rewrites.append(ImportRewrite(
                        filepath=filepath,
                        old_import=f"{dots}{module}",
                        new_import=absolute,
                    ))

            if new_content != content:
                generated_files[filepath] = new_content

    def _normalize_dep_names(self, deps: List[str]) -> Set[str]:
        """Normalize dependency names to importable top-level module names."""
        names = set()
        for dep in deps:
            # Strip version specifiers: "fastapi>=0.100" → "fastapi"
            name = dep.split(">=")[0].split("<=")[0].split("==")[0]
            name = name.split("[")[0]  # "uvicorn[standard]" → "uvicorn"
            name = name.strip().lower().replace("-", "_")
            names.add(name)
        # Add reverse aliases
        for alias, pip_name in _COMMON_ALIASES.items():
            pkg = pip_name.lower().replace("-", "_")
            if pkg in names:
                names.add(alias.lower())
        return names

    def _extract_imports(
        self, filepath: str, content: str
    ) -> List[Tuple[str, bool, int]]:
        """
        Extract imports from Python source.

        Returns list of (module_name, is_relative, level) tuples.
        """
        imports = []
        try:
            tree = ast.parse(content, filename=filepath)
        except SyntaxError:
            return imports

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append((alias.name, False, 0))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                level = node.level or 0
                imports.append((module, level > 0, level))

        return imports

    def _check_import(
        self,
        filepath: str,
        module: str,
        is_relative: bool,
        level: int,
        all_files: Dict[str, str],
        dep_names: Set[str],
    ) -> ImportIssue | None:
        """Check if a single import resolves. Returns an issue or None."""
        if is_relative:
            return self._check_relative_import(filepath, module, level, all_files)
        else:
            return self._check_absolute_import(filepath, module, all_files, dep_names)

    def _check_relative_import(
        self,
        filepath: str,
        module: str,
        level: int,
        all_files: Dict[str, str],
    ) -> ImportIssue | None:
        """Check relative import resolution (from .foo import bar)."""
        # Navigate up 'level' directories from the file
        parts = PurePosixPath(filepath).parts
        if level > len(parts) - 1:
            return ImportIssue(
                filepath=filepath,
                import_name=f"{'.' * level}{module}",
                issue="missing_module",
                detail=f"Relative import level {level} exceeds package depth",
            )

        base_parts = parts[: len(parts) - level]
        if module:
            target_parts = list(base_parts) + module.split(".")
        else:
            # "from . import something" — the package itself
            return None

        # Check if target exists as a module file or package
        target_file = "/".join(target_parts) + ".py"
        target_init = "/".join(target_parts) + "/__init__.py"

        if target_file in all_files or target_init in all_files:
            return None

        # For dotted relative imports like "from .sub.models import Foo",
        # check if "sub" is a .py file (not package) with "models" as an attr.
        # Only applies to multi-part modules, NOT single-part like "from .errors import X"
        if len(module.split(".")) > 1 and len(target_parts) > 1:
            parent_file = "/".join(target_parts[:-1]) + ".py"
            if parent_file in all_files:
                return None

        return ImportIssue(
            filepath=filepath,
            import_name=f"{'.' * level}{module}",
            issue="missing_module",
            detail=f"Module not found: {target_file}",
        )

    def _check_absolute_import(
        self,
        filepath: str,
        module: str,
        all_files: Dict[str, str],
        dep_names: Set[str],
    ) -> ImportIssue | None:
        """Check absolute import resolution."""
        if not module:
            return None

        top_level = module.split(".")[0]

        # stdlib
        if top_level in _STDLIB_MODULES:
            return None

        # Declared dependency
        if top_level.lower().replace("-", "_") in dep_names:
            return None

        # Common alias
        if top_level in _COMMON_ALIASES:
            return None

        # Well-known third-party package
        if top_level.lower().replace("-", "_") in _WELL_KNOWN_PACKAGES:
            return None

        # Workspace module — the exact module must resolve
        parts = module.split(".")

        # Check exact module: myapp/domain/errors.py or myapp/domain/errors/__init__.py
        exact_file = "/".join(parts) + ".py"
        exact_init = "/".join(parts) + "/__init__.py"
        if exact_file in all_files or exact_init in all_files:
            return None

        # For "from myapp.domain.errors import Foo", also accept if
        # myapp/domain.py exists (errors could be an attr of domain module)
        # but NOT if only myapp/__init__.py exists (too far up the chain)
        if len(parts) >= 2:
            parent_file = "/".join(parts[:-1]) + ".py"
            parent_init = "/".join(parts[:-1]) + "/__init__.py"
            if parent_file in all_files or parent_init in all_files:
                return None

        # Check if at least the top-level package exists (project module)
        # If it does, the import is probably valid but a submodule is missing
        top_file = parts[0] + ".py"
        top_init = parts[0] + "/__init__.py"
        if top_file in all_files or top_init in all_files:
            # Module root exists but specific submodule doesn't — flag it
            return ImportIssue(
                filepath=filepath,
                import_name=module,
                issue="missing_module",
                detail=f"Module not found: {exact_file}",
            )

        return ImportIssue(
            filepath=filepath,
            import_name=module,
            issue="missing_module",
            detail=f"Module '{module}' not found in workspace, stdlib, or declared dependencies",
        )

    def _auto_stub(
        self,
        issue: ImportIssue,
        all_files: Dict[str, str],
        dep_names: Set[str] | None = None,
    ) -> AutoStub | None:
        """Try to create an auto-stub for a missing module."""
        if issue.issue != "missing_module":
            return None

        # Extract target file path from the detail (set by _check_relative_import
        # and _check_absolute_import)
        target_file = None
        if "Module not found:" in issue.detail:
            # detail format: "Module not found: pkg/errors.py"
            target_file = issue.detail.split("Module not found:")[-1].strip()
        elif "not found in workspace" in issue.detail:
            # Absolute import: convert module path to file path
            import_name = issue.import_name.lstrip(".")
            if import_name:
                target_file = "/".join(import_name.split(".")) + ".py"

        if not target_file or target_file in all_files:
            return None

        # Never create a stub that shadows a known package (declared dep,
        # stdlib, or common alias).  e.g. never create "pydantic.py".
        import_name = issue.import_name.lstrip(".")
        top_level = import_name.split(".")[0] if import_name else ""
        if top_level:
            top_lower = top_level.lower().replace("-", "_")
            if (
                top_level in _STDLIB_MODULES
                or top_level in _COMMON_ALIASES
                or top_lower in _WELL_KNOWN_PACKAGES
                or (dep_names and top_lower in dep_names)
            ):
                return None

        # If the leaf module exists elsewhere in the workspace, the import
        # is just wrong (not truly missing). Skip stubbing — the orchestrator's
        # auto-fix will rewrite the import path instead.
        leaf = import_name.split(".")[-1] if import_name else ""
        if leaf:
            for existing_path in all_files:
                if existing_path.endswith(f"/{leaf}.py") or existing_path == f"{leaf}.py":
                    # A real module with this name exists elsewhere — don't stub
                    return None

        stub_content = self._generate_stub(import_name, issue.filepath, all_files)
        return AutoStub(
            filepath=target_file,
            content=stub_content,
            reason=f"auto-stub for import in {issue.filepath}",
        )

    def _generate_stub(
        self, module_name: str, importing_file: str, all_files: Dict[str, str]
    ) -> str:
        """Generate a minimal stub module."""
        # Try to infer what names are imported from this module
        names = self._find_imported_names(module_name, importing_file, all_files)

        lines = [f'"""Auto-generated stub for {module_name}."""', ""]

        for name in names:
            if name[0].isupper():
                # Looks like a class name
                if "error" in name.lower() or "exception" in name.lower():
                    lines.append(f"class {name}(Exception):")
                    lines.append("    pass")
                else:
                    lines.append(f"class {name}:")
                    lines.append("    pass")
                lines.append("")
            else:
                # Looks like a function or constant
                if name.isupper():
                    lines.append(f"{name} = None")
                else:
                    lines.append(f"def {name}(*args, **kwargs):")
                    lines.append("    pass")
                lines.append("")

        if not names:
            lines.append("# Stub — no specific imports detected")
            lines.append("")

        return "\n".join(lines)

    def _find_imported_names(
        self, module_name: str, filepath: str, all_files: Dict[str, str]
    ) -> List[str]:
        """Find specific names imported from a module across all files."""
        names = []
        mod_parts = module_name.split(".")

        for fpath, content in all_files.items():
            if not fpath.endswith(".py"):
                continue
            try:
                tree = ast.parse(content, filename=fpath)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    node_module = node.module or ""
                    # Match both absolute and relative imports to this module
                    if node_module == module_name or node_module.endswith("." + mod_parts[-1]):
                        for alias in node.names:
                            if alias.name != "*" and alias.name not in names:
                                names.append(alias.name)

        return names

    def _check_init_files(self, all_files: Dict[str, str]) -> List[AutoStub]:
        """Check that every package directory has an __init__.py."""
        stubs = []
        # Collect all directories that contain .py files
        dirs_with_py: Set[str] = set()
        for filepath in all_files:
            if filepath.endswith(".py"):
                parts = PurePosixPath(filepath).parts
                # Add all parent directories (excluding the filename)
                for i in range(1, len(parts)):
                    dirs_with_py.add("/".join(parts[:i]))

        for dir_path in sorted(dirs_with_py):
            init_path = f"{dir_path}/__init__.py"
            if init_path not in all_files:
                stubs.append(AutoStub(
                    filepath=init_path,
                    content="",
                    reason=f"missing __init__.py for package {dir_path}",
                ))

        return stubs
