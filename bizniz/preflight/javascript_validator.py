"""Pre-flight validator for JavaScript projects."""

import re
from pathlib import PurePosixPath
from typing import Dict, List, Set

from bizniz.preflight.base_validator import BasePreflightValidator
from bizniz.preflight.types import AutoStub, ImportIssue, PreflightResult


# Regex patterns for JS imports
_IMPORT_FROM_RE = re.compile(
    r"""(?:import|export)\s+"""
    r"""(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?|\*\s+as\s+\w+)"""
    r"""\s+from\s+)?"""
    r"""['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_IMPORT_RE = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# JS file extensions in resolution order
_JS_EXTENSIONS = [".js", ".jsx", ".mjs", ".cjs"]
_INDEX_FILES = ["index.js", "index.jsx", "index.mjs"]

# Re-use Node.js builtins from TS validator
from bizniz.preflight.typescript_validator import _NODE_BUILTINS


class JavaScriptPreflightValidator(BasePreflightValidator):
    """Validates JavaScript import/require resolution and auto-stubs missing modules."""

    language = "javascript"

    def validate(
        self,
        generated_files: Dict[str, str],
        declared_dependencies: List[str],
    ) -> PreflightResult:
        result = PreflightResult(language=self.language)

        workspace_files = self._get_workspace_files()
        all_files = {**workspace_files, **generated_files}
        dep_names = set(declared_dependencies)

        for filepath, content in generated_files.items():
            if not self._is_js_file(filepath):
                continue
            result.files_checked += 1

            imports = self._extract_imports(content)
            for import_path in imports:
                issue = self._check_import(filepath, import_path, all_files, dep_names)
                if issue:
                    stub = self._auto_stub(issue, all_files)
                    if stub:
                        result.stubs_created.append(stub)
                        all_files[stub.filepath] = stub.content
                    else:
                        result.issues.append(issue)

        return result

    def _is_js_file(self, filepath: str) -> bool:
        return any(filepath.endswith(ext) for ext in _JS_EXTENSIONS)

    def _get_workspace_files(self) -> Dict[str, str]:
        files = {}
        for rel_path in self._workspace.list_relative_files():
            rel = str(rel_path)
            if self._is_js_file(rel) or rel.endswith(".json"):
                files[rel] = ""
        return files

    def _extract_imports(self, content: str) -> List[str]:
        """Extract all import/require paths from JavaScript source."""
        imports = []
        for match in _IMPORT_FROM_RE.finditer(content):
            imports.append(match.group(1))
        for match in _REQUIRE_RE.finditer(content):
            imports.append(match.group(1))
        for match in _DYNAMIC_IMPORT_RE.finditer(content):
            imports.append(match.group(1))
        return imports

    def _check_import(
        self,
        filepath: str,
        import_path: str,
        all_files: Dict[str, str],
        dep_names: Set[str],
    ) -> ImportIssue | None:
        # Node builtin
        if import_path in _NODE_BUILTINS:
            return None

        # Non-relative = npm package
        if not import_path.startswith("."):
            if import_path.startswith("@"):
                parts = import_path.split("/")
                pkg_name = "/".join(parts[:2]) if len(parts) >= 2 else import_path
            else:
                pkg_name = import_path.split("/")[0]

            if pkg_name in dep_names:
                return None

            return ImportIssue(
                filepath=filepath,
                import_name=import_path,
                issue="missing_dependency",
                detail=f"Package '{pkg_name}' not in declared dependencies",
            )

        # Relative import
        resolved = self._resolve_relative(filepath, import_path, all_files)
        if resolved:
            return None

        return ImportIssue(
            filepath=filepath,
            import_name=import_path,
            issue="missing_module",
            detail=f"Cannot resolve '{import_path}' from {filepath}",
        )

    def _resolve_relative(
        self, from_file: str, import_path: str, all_files: Dict[str, str]
    ) -> str | None:
        """Resolve a relative import to an actual file path."""
        base_dir = str(PurePosixPath(from_file).parent)
        if base_dir == ".":
            target = import_path.lstrip("./")
        else:
            target = str(PurePosixPath(base_dir) / import_path)

        # Normalize .. references
        parts = []
        for p in target.split("/"):
            if p == "..":
                if parts:
                    parts.pop()
            elif p != ".":
                parts.append(p)
        target = "/".join(parts)

        # Exact match
        if target in all_files:
            return target

        # Try extensions
        for ext in _JS_EXTENSIONS:
            if (target + ext) in all_files:
                return target + ext

        # Try as directory with index
        for index in _INDEX_FILES:
            if f"{target}/{index}" in all_files:
                return f"{target}/{index}"

        return None

    def _auto_stub(
        self, issue: ImportIssue, all_files: Dict[str, str]
    ) -> AutoStub | None:
        if issue.issue != "missing_module":
            return None

        base_dir = str(PurePosixPath(issue.filepath).parent)
        import_path = issue.import_name
        if base_dir == ".":
            target = import_path.lstrip("./")
        else:
            target = str(PurePosixPath(base_dir) / import_path)

        parts = []
        for p in target.split("/"):
            if p == "..":
                if parts:
                    parts.pop()
            elif p != ".":
                parts.append(p)
        target = "/".join(parts)

        if not any(target.endswith(ext) for ext in _JS_EXTENSIONS):
            target += ".js"

        stub_content = (
            "// Auto-generated stub\n"
            "module.exports = {};\n"
        )
        return AutoStub(
            filepath=target,
            content=stub_content,
            reason=f"auto-stub for import in {issue.filepath}",
        )
