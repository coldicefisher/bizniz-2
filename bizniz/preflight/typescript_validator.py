"""Pre-flight validator for TypeScript projects."""

import re
from pathlib import PurePosixPath
from typing import Dict, List, Set, Tuple

from bizniz.preflight.base_validator import BasePreflightValidator
from bizniz.preflight.types import AutoStub, ImportIssue, PreflightResult


# Regex patterns for TypeScript imports
_IMPORT_FROM_RE = re.compile(
    r"""(?:import|export)\s+"""
    r"""(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?|\*\s+as\s+\w+)"""
    r"""\s+from\s+)?"""
    r"""['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Dynamic import()
_DYNAMIC_IMPORT_RE = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# TypeScript file extensions in resolution order
_TS_EXTENSIONS = [".ts", ".tsx", ".d.ts"]
_INDEX_FILES = ["index.ts", "index.tsx"]

# Node.js built-in modules
_NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "dns", "domain", "events", "fs", "http", "http2",
    "https", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "sys", "timers", "tls", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
    # Node prefixed
    "node:assert", "node:buffer", "node:child_process", "node:crypto",
    "node:dns", "node:events", "node:fs", "node:http", "node:http2",
    "node:https", "node:net", "node:os", "node:path", "node:process",
    "node:querystring", "node:readline", "node:stream", "node:timers",
    "node:tls", "node:url", "node:util", "node:v8", "node:vm",
    "node:worker_threads", "node:zlib",
}


class TypeScriptPreflightValidator(BasePreflightValidator):
    """Validates TypeScript/TSX import resolution and auto-stubs missing modules."""

    language = "typescript"

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
            if not self._is_ts_file(filepath):
                continue
            result.files_checked += 1

            imports = self._extract_imports(content)
            for import_path in imports:
                issue = self._check_import(
                    filepath, import_path, all_files, dep_names
                )
                if issue:
                    stub = self._auto_stub(issue, all_files)
                    if stub:
                        result.stubs_created.append(stub)
                        all_files[stub.filepath] = stub.content
                    else:
                        result.issues.append(issue)

        return result

    def _is_ts_file(self, filepath: str) -> bool:
        return any(filepath.endswith(ext) for ext in [".ts", ".tsx"])

    def _get_workspace_files(self) -> Dict[str, str]:
        files = {}
        for rel_path in self._workspace.list_relative_files():
            rel = str(rel_path)
            if self._is_ts_file(rel) or rel.endswith(".js") or rel.endswith(".jsx"):
                files[rel] = ""  # Existence is enough
        return files

    def _extract_imports(self, content: str) -> List[str]:
        """Extract all import paths from TypeScript source."""
        imports = []
        for match in _IMPORT_FROM_RE.finditer(content):
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
        """Check if an import path resolves."""
        # Node builtin
        if import_path in _NODE_BUILTINS:
            return None

        # Non-relative = npm package
        if not import_path.startswith("."):
            # Get the package name (scoped: @scope/pkg, unscoped: pkg)
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

        # Relative import — resolve against filesystem
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
        # Normalize the path
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

        # Try exact match (import './foo.ts')
        if target in all_files:
            return target

        # Try adding extensions
        for ext in _TS_EXTENSIONS:
            candidate = target + ext
            if candidate in all_files:
                return candidate

        # Try as directory with index file
        for index in _INDEX_FILES:
            candidate = f"{target}/{index}"
            if candidate in all_files:
                return candidate

        return None

    def _auto_stub(
        self, issue: ImportIssue, all_files: Dict[str, str]
    ) -> AutoStub | None:
        """Auto-stub a missing TypeScript module."""
        if issue.issue != "missing_module":
            return None

        # Resolve the target path
        base_dir = str(PurePosixPath(issue.filepath).parent)
        import_path = issue.import_name
        if base_dir == ".":
            target = import_path.lstrip("./")
        else:
            target = str(PurePosixPath(base_dir) / import_path)

        # Normalize
        parts = []
        for p in target.split("/"):
            if p == "..":
                if parts:
                    parts.pop()
            elif p != ".":
                parts.append(p)
        target = "/".join(parts)

        # Default to .ts extension
        if not any(target.endswith(ext) for ext in _TS_EXTENSIONS):
            target += ".ts"

        # Extract imported names from the import statement
        names = self._find_imported_names(issue.import_name, issue.filepath, all_files)
        stub_content = self._generate_stub(names)

        return AutoStub(
            filepath=target,
            content=stub_content,
            reason=f"auto-stub for import in {issue.filepath}",
        )

    def _find_imported_names(
        self, import_path: str, filepath: str, all_files: Dict[str, str]
    ) -> List[str]:
        """Find what names are imported from a given path."""
        names = []
        content = all_files.get(filepath, "")
        # Match: import { Name1, Name2 } from './path'
        pattern = re.compile(
            r"""import\s*\{([^}]+)\}\s*from\s*['"]"""
            + re.escape(import_path)
            + r"""['"]"""
        )
        for match in pattern.finditer(content):
            for name in match.group(1).split(","):
                name = name.strip().split(" as ")[0].strip()
                if name and name not in names:
                    names.append(name)
        return names

    def _generate_stub(self, names: List[str]) -> str:
        """Generate a TypeScript stub with exported names."""
        lines = ["// Auto-generated stub", ""]
        for name in names:
            if name[0].isupper():
                # Looks like a class/type/interface
                if "Props" in name or "Config" in name or "Options" in name:
                    lines.append(f"export interface {name} {{}}")
                else:
                    lines.append(f"export class {name} {{}}")
            else:
                lines.append(f"export const {name} = undefined as any;")
            lines.append("")

        if not names:
            lines.append("// Stub — no specific exports detected")
            lines.append("export default {};")
            lines.append("")

        return "\n".join(lines)
