"""``WorkspaceContextBuilder`` — builds a WorkspaceContext snapshot
from the live workspace state.

Inputs: workspace + issue (gives us the file paths we care about).
Output: WorkspaceContext that the agent prompt renders inline.

Cheap to run (file IO + manifest parsing). Built fresh per agent
call — file state changes between calls and we want the agent to
see current truth, not stale cache.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.coder.types import Issue
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace_context.aliases import (
    npm_import_for, python_import_for,
)
from bizniz.workspace_context.types import (
    DeclaredPackage, WorkspaceContext,
)


class WorkspaceContextBuilder:
    """Stateless builder. Construct once per V4 dispatcher; call
    ``build(issue)`` per agent invocation."""

    def __init__(
        self,
        *,
        workspace: BaseWorkspace,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._workspace = workspace
        self._on_status = on_status

    def build(self, issue: Issue) -> WorkspaceContext:
        """Build a WorkspaceContext snapshot for one issue."""
        target_content, test_content, missing = self._read_files(issue)
        py_pkgs = self._parse_python_deps()
        node_pkgs = self._parse_node_deps()
        ws_root = self._workspace_root_str()
        return WorkspaceContext(
            target_files_content=target_content,
            test_files_content=test_content,
            missing_paths=missing,
            declared_python_packages=py_pkgs,
            declared_node_packages=node_pkgs,
            workspace_root=ws_root,
        )

    # ── File reading ─────────────────────────────────────────────

    def _read_files(self, issue: Issue) -> tuple:
        target_content: dict = {}
        test_content: dict = {}
        missing: list = []
        for path in (issue.target_files or []):
            content = self._safe_read(path)
            if content is None:
                missing.append(path)
            else:
                target_content[path] = content
        for path in (issue.test_files or []):
            content = self._safe_read(path)
            if content is None:
                missing.append(path)
            else:
                test_content[path] = content
        return target_content, test_content, missing

    def _safe_read(self, rel: str) -> Optional[str]:
        try:
            p = self._workspace.path(rel)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        return None

    def _workspace_root_str(self) -> str:
        try:
            root = getattr(self._workspace, "root", None)
            return str(root) if root else ""
        except Exception:
            return ""

    # ── Python deps ──────────────────────────────────────────────

    def _parse_python_deps(self) -> List[DeclaredPackage]:
        pkgs: List[DeclaredPackage] = []
        seen_names: set = set()

        req = self._safe_read("requirements.txt")
        if req:
            for line in req.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                # Strip extras + version specifier.
                name = line.split("[", 1)[0]
                version = ""
                for sep in ("==", ">=", "<=", "!=", "~=", ">", "<", ";"):
                    if sep in name:
                        name_part, ver_part = name.split(sep, 1)
                        name = name_part
                        version = (sep + ver_part.split(";")[0]).strip()
                        break
                name = name.strip()
                if not name or name.lower() in seen_names:
                    continue
                seen_names.add(name.lower())
                pkgs.append(DeclaredPackage(
                    name=name,
                    version=version,
                    import_name=python_import_for(name),
                    language="python",
                ))

        # Also parse pyproject.toml [project.dependencies] if present.
        pyproject = self._safe_read("pyproject.toml")
        if pyproject:
            try:
                import tomllib
                data = tomllib.loads(pyproject)
                deps = (data.get("project") or {}).get("dependencies") or []
                for d in deps:
                    s = str(d).split("[", 1)[0]
                    name = s
                    version = ""
                    for sep in ("==", ">=", "<=", "!=", "~=", ">", "<", ";"):
                        if sep in name:
                            np, vp = name.split(sep, 1)
                            name = np
                            version = (sep + vp.split(";")[0]).strip()
                            break
                    name = name.strip()
                    if not name or name.lower() in seen_names:
                        continue
                    seen_names.add(name.lower())
                    pkgs.append(DeclaredPackage(
                        name=name, version=version,
                        import_name=python_import_for(name),
                        language="python",
                    ))
            except Exception:
                pass

        return pkgs

    # ── Node deps ────────────────────────────────────────────────

    def _parse_node_deps(self) -> List[DeclaredPackage]:
        pkgs: List[DeclaredPackage] = []
        seen: set = set()
        pj = self._safe_read("package.json")
        if not pj:
            return pkgs
        try:
            data = json.loads(pj)
        except Exception:
            return pkgs
        for kind in ("dependencies", "devDependencies"):
            deps = data.get(kind) or {}
            for name, version in deps.items():
                if name.lower() in seen:
                    continue
                seen.add(name.lower())
                pkgs.append(DeclaredPackage(
                    name=name,
                    version=str(version),
                    import_name=npm_import_for(name),
                    language="typescript",
                ))
        return pkgs
