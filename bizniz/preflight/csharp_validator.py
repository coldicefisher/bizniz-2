"""Pre-flight validator for C# projects."""

import re
from pathlib import PurePosixPath
from typing import Dict, List, Set

from bizniz.preflight.base_validator import BasePreflightValidator
from bizniz.preflight.types import AutoStub, ImportIssue, PreflightResult


# Regex for C# using directives
_USING_RE = re.compile(r"^\s*using\s+(?:static\s+)?([A-Za-z][\w.]*)\s*;", re.MULTILINE)

# Regex for namespace declaration
_NAMESPACE_RE = re.compile(
    r"^\s*namespace\s+([\w.]+)", re.MULTILINE
)

# .NET BCL / System namespaces (top-level)
_SYSTEM_NAMESPACES = {
    "System", "Microsoft", "NUnit", "Xunit",
}

# Common NuGet packages mapped to their namespaces
_NUGET_NAMESPACE_MAP = {
    "Newtonsoft": "Newtonsoft.Json",
    "AutoMapper": "AutoMapper",
    "FluentValidation": "FluentValidation",
    "MediatR": "MediatR",
    "Serilog": "Serilog",
    "Dapper": "Dapper",
    "Moq": "Moq",
    "Bogus": "Bogus",
    "Polly": "Polly",
    "Swashbuckle": "Swashbuckle.AspNetCore",
}


class CSharpPreflightValidator(BasePreflightValidator):
    """Validates C# using/namespace resolution and auto-stubs missing files."""

    language = "csharp"

    def validate(
        self,
        generated_files: Dict[str, str],
        declared_dependencies: List[str],
    ) -> PreflightResult:
        result = PreflightResult(language=self.language)

        workspace_files = self._get_workspace_files()
        all_files = {**workspace_files, **generated_files}

        # Build namespace → file mapping from all .cs files
        namespace_map = self._build_namespace_map(all_files)
        dep_names = self._normalize_deps(declared_dependencies)

        for filepath, content in generated_files.items():
            if not filepath.endswith(".cs"):
                continue
            result.files_checked += 1

            usings = self._extract_usings(content)
            file_namespace = self._extract_namespace(content)

            for using_ns in usings:
                issue = self._check_using(
                    filepath, using_ns, file_namespace,
                    namespace_map, dep_names, all_files,
                )
                if issue:
                    stub = self._auto_stub(issue, file_namespace, all_files)
                    if stub:
                        result.stubs_created.append(stub)
                        all_files[stub.filepath] = stub.content
                        # Update namespace map
                        ns = self._extract_namespace(stub.content)
                        if ns:
                            namespace_map.setdefault(ns, []).append(stub.filepath)
                    else:
                        result.issues.append(issue)

        return result

    def _get_workspace_files(self) -> Dict[str, str]:
        files = {}
        for rel_path in self._workspace.list_relative_files():
            rel = str(rel_path)
            if rel.endswith(".cs") or rel.endswith(".csproj"):
                try:
                    files[rel] = self._workspace.read_file(path=rel)
                except Exception:
                    files[rel] = ""
        return files

    def _normalize_deps(self, deps: List[str]) -> Set[str]:
        """Normalize NuGet package names to possible namespace roots."""
        names = set()
        for dep in deps:
            # NuGet packages often match their root namespace
            parts = dep.split(".")
            names.add(parts[0])
            names.add(dep)
        # Add known mappings
        for ns_root, pkg in _NUGET_NAMESPACE_MAP.items():
            if pkg in names or ns_root in names:
                names.add(ns_root)
        return names

    def _extract_usings(self, content: str) -> List[str]:
        """Extract using directives from C# source."""
        return _USING_RE.findall(content)

    def _extract_namespace(self, content: str) -> str | None:
        """Extract the namespace declaration from C# source."""
        match = _NAMESPACE_RE.search(content)
        return match.group(1) if match else None

    def _build_namespace_map(self, all_files: Dict[str, str]) -> Dict[str, List[str]]:
        """Map namespace → list of file paths that declare it."""
        ns_map: Dict[str, List[str]] = {}
        for filepath, content in all_files.items():
            if not filepath.endswith(".cs"):
                continue
            ns = self._extract_namespace(content)
            if ns:
                ns_map.setdefault(ns, []).append(filepath)
        return ns_map

    def _check_using(
        self,
        filepath: str,
        using_ns: str,
        file_namespace: str | None,
        namespace_map: Dict[str, List[str]],
        dep_names: Set[str],
        all_files: Dict[str, str],
    ) -> ImportIssue | None:
        """Check if a using directive resolves."""
        top_level = using_ns.split(".")[0]

        # System/Microsoft namespaces (BCL)
        if top_level in _SYSTEM_NAMESPACES:
            return None

        # Known NuGet namespace
        if top_level in dep_names:
            return None

        # Same project namespace — check if any prefix matches
        # e.g. using MyApp.Models should resolve if MyApp.Models exists
        # or if MyApp exists and Models is a class within it
        for ns in namespace_map:
            if using_ns == ns or using_ns.startswith(ns + ".") or ns.startswith(using_ns + "."):
                return None

        # Check if using is a sub-namespace of the file's own namespace
        if file_namespace and (
            using_ns.startswith(file_namespace + ".")
            or file_namespace.startswith(using_ns + ".")
            or using_ns == file_namespace
        ):
            return None

        return ImportIssue(
            filepath=filepath,
            import_name=using_ns,
            issue="missing_module",
            detail=f"Namespace '{using_ns}' not found in project, BCL, or dependencies",
        )

    def _auto_stub(
        self,
        issue: ImportIssue,
        file_namespace: str | None,
        all_files: Dict[str, str],
    ) -> AutoStub | None:
        """Auto-stub a missing C# namespace by creating a file."""
        if issue.issue != "missing_module":
            return None

        using_ns = issue.import_name

        # Convert namespace to file path: MyApp.Domain.Models → MyApp/Domain/Models.cs
        # But we want it relative to the project structure
        parts = using_ns.split(".")
        target_file = "/".join(parts) + ".cs"

        # If we have a project root namespace, try to place it relative
        if file_namespace:
            root_parts = file_namespace.split(".")
            # If they share a common prefix, use relative placement
            common = 0
            for i, (a, b) in enumerate(zip(root_parts, parts)):
                if a == b:
                    common = i + 1
                else:
                    break
            if common > 0:
                relative_parts = parts[common:]
                if relative_parts:
                    # Place alongside existing files with same root
                    base = "/".join(root_parts[:common])
                    target_file = base + "/" + "/".join(relative_parts) + ".cs"

        if target_file in all_files:
            return None

        # Find what names are used from this namespace
        names = self._find_used_names(using_ns, all_files)

        stub_lines = [
            f"// Auto-generated stub for {using_ns}",
            f"namespace {using_ns}",
            "{",
        ]
        for name in names:
            if "Exception" in name or "Error" in name:
                stub_lines.append(f"    public class {name} : System.Exception")
                stub_lines.append("    {")
                stub_lines.append(f"        public {name}(string message) : base(message) {{ }}")
                stub_lines.append("    }")
            else:
                stub_lines.append(f"    public class {name}")
                stub_lines.append("    {")
                stub_lines.append("    }")
            stub_lines.append("")
        if not names:
            stub_lines.append("    // Stub — no specific types detected")
        stub_lines.append("}")
        stub_lines.append("")

        return AutoStub(
            filepath=target_file,
            content="\n".join(stub_lines),
            reason=f"auto-stub for using in {issue.filepath}",
        )

    def _find_used_names(
        self, namespace: str, all_files: Dict[str, str]
    ) -> List[str]:
        """Find type names used from a namespace across all files."""
        names = []
        # Look for patterns like: new TypeName, : TypeName, TypeName.Something
        # after a using for this namespace
        ns_parts = namespace.split(".")
        short_name = ns_parts[-1] if ns_parts else ""

        for filepath, content in all_files.items():
            if not filepath.endswith(".cs"):
                continue
            if f"using {namespace};" not in content:
                continue
            # Find capitalized identifiers that could be types from this namespace
            # Simple heuristic: new Foo( or : Foo or <Foo> or Foo.
            type_re = re.compile(r"(?:new\s+|:\s*|<)(" + re.escape(short_name) + r")\b")
            for match in type_re.finditer(content):
                name = match.group(1)
                if name not in names:
                    names.append(name)

        return names
