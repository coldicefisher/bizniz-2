"""Workspace-context injector.

The coder, before writing files that import from elsewhere in the
service, needs to know what's actually exported by those other
files. Without this, two coders writing different files in the
same service produce internally-inconsistent code (the LoginPage /
authStore class of bug we keep hitting).

This module:

1. Detects the workspace's primary language (Python, TypeScript).
2. Dispatches the right documenter (in-process for Python, sidecar
   for TypeScript).
3. Formats the result as a concise, coder-readable WORKSPACE
   CONTEXT section that the coder's user prompt embeds.

Output is bounded by ``max_chars`` so a large workspace can't blow
the prompt budget. We prefer breadth (file coverage) over depth
(method bodies).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from bizniz.documenters.python_ast import PythonAstDocumenter
from bizniz.documenters.typescript_ast import TypeScriptAstDocumenter, DocumenterError


def detect_language(workspace_root: Path) -> Optional[str]:
    """Best-effort language sniff. Returns ``"python"`` or
    ``"typescript"`` or ``None``."""
    if any(workspace_root.glob("**/*.tsx")) or any(workspace_root.glob("**/*.ts")):
        # Skip if all matches are under node_modules/dist/etc.
        for p in workspace_root.rglob("*.ts"):
            parts = p.relative_to(workspace_root).parts
            if not any(seg in {"node_modules", "dist", "build", ".next"} for seg in parts):
                return "typescript"
    if any(workspace_root.glob("**/*.py")):
        for p in workspace_root.rglob("*.py"):
            parts = p.relative_to(workspace_root).parts
            if not any(seg in {"__pycache__", ".venv", "venv"} for seg in parts):
                return "python"
    return None


def extract_workspace_docs(
    workspace_root: Path,
    service_name: str = "",
    language_hint: Optional[str] = None,
) -> Optional[Dict]:
    """Run the appropriate documenter and return the extracted doc.

    Returns ``None`` if no language could be detected or the
    documenter failed. The caller should handle None gracefully —
    a missing context is preferable to a broken prompt.
    """
    lang = language_hint or detect_language(workspace_root)
    if lang == "python":
        return PythonAstDocumenter(
            workspace_root=workspace_root, service_name=service_name,
        ).extract()
    if lang == "typescript":
        try:
            return TypeScriptAstDocumenter(
                workspace_root=workspace_root, service_name=service_name,
            ).extract()
        except DocumenterError:
            # Sidecar might not be reachable in some test environments.
            # Fail soft — coder runs without context, integration tests
            # still catch regressions.
            return None
    return None


def format_for_prompt(docs: Optional[Dict], max_chars: int = 8000) -> str:
    """Format a documenter output as a section to embed in the coder's
    user prompt. Bounded by ``max_chars`` to keep prompts predictable.

    Returns an empty string if docs is None or has no useful content.
    """
    if not docs or not docs.get("files"):
        return ""

    lang = docs.get("language", "")
    if lang == "python":
        return _format_python(docs, max_chars)
    if lang == "typescript":
        return _format_typescript(docs, max_chars)
    return ""


# ── Python formatting ──────────────────────────────────────────────


def _format_python(docs: Dict, max_chars: int) -> str:
    out_lines = [
        "WORKSPACE CONTEXT (already in this service — IMPORT these, "
        "do NOT redefine them; if the symbol you need isn't here, you "
        "must create it, not assume it exists elsewhere):",
        "",
    ]
    used = sum(len(line) + 1 for line in out_lines)

    for path, file_doc in sorted(docs["files"].items()):
        if file_doc.get("_parse_error"):
            continue
        chunk = _format_python_file(path, file_doc)
        if not chunk:
            continue
        if used + len(chunk) > max_chars:
            out_lines.append(f"... ({len(docs['files']) - len(out_lines) + 2} more files truncated for length)")
            break
        out_lines.append(chunk)
        used += len(chunk) + 1

    return "\n".join(out_lines).rstrip() + "\n"


def _format_python_file(path: str, file_doc: Dict) -> str:
    classes = file_doc.get("classes") or []
    functions = file_doc.get("functions") or []
    if not classes and not functions:
        return ""
    lines = [f"  {path}:"]
    for cls in classes:
        bases = ",".join(cls.get("bases", []))
        bases_part = f"({bases})" if bases else ""
        fields = cls.get("fields") or []
        if fields:
            field_strs = []
            for f in fields[:8]:
                t = f.get("type") or "Any"
                field_strs.append(f"{f['name']}: {t}")
            field_part = " — fields: " + ", ".join(field_strs)
        else:
            field_part = ""
        lines.append(f"    class {cls['name']}{bases_part}{field_part}")
        # Methods (just signatures, very compact)
        for m in (cls.get("methods") or [])[:6]:
            sig = _format_python_function_sig(m, indent=6)
            lines.append(sig)
    for fn in functions:
        lines.append(_format_python_function_sig(fn, indent=4))
    return "\n".join(lines)


def _format_python_function_sig(fn: Dict, indent: int) -> str:
    params = fn.get("params") or []
    param_strs = []
    for p in params:
        s = p["name"]
        if p.get("type"):
            s += f": {p['type']}"
        param_strs.append(s)
    sig = f"{fn['name']}({', '.join(param_strs)})"
    rt = fn.get("return_type")
    if rt:
        sig += f" -> {rt}"
    if fn.get("is_async"):
        sig = "async " + sig
    return " " * indent + sig


# ── TypeScript formatting ──────────────────────────────────────────


def _format_typescript(docs: Dict, max_chars: int) -> str:
    out_lines = [
        "WORKSPACE CONTEXT (already in this service — IMPORT these, "
        "do NOT redefine them; if the symbol you need isn't here, you "
        "must create it, not assume it exists elsewhere):",
        "",
    ]
    used = sum(len(line) + 1 for line in out_lines)

    for path, file_doc in sorted(docs["files"].items()):
        if file_doc.get("_parse_error"):
            continue
        chunk = _format_typescript_file(path, file_doc)
        if not chunk:
            continue
        if used + len(chunk) > max_chars:
            out_lines.append("... (more files truncated for length)")
            break
        out_lines.append(chunk)
        used += len(chunk) + 1

    return "\n".join(out_lines).rstrip() + "\n"


def _format_typescript_file(path: str, file_doc: Dict) -> str:
    exports = file_doc.get("exports") or []
    interfaces = file_doc.get("interfaces") or []
    types = file_doc.get("types") or []
    stores = file_doc.get("stores") or []

    if not exports and not interfaces and not types and not stores:
        return ""

    lines = [f"  {path}:"]

    for exp in exports:
        kind = exp.get("kind", "")
        name = exp.get("name", "")
        if kind == "function":
            params = ", ".join(
                _format_ts_param(p) for p in (exp.get("params") or [])
            )
            ret = exp.get("return_type") or ""
            ret_part = f": {ret}" if ret else ""
            async_part = "async " if exp.get("async") else ""
            lines.append(f"    export {async_part}function {name}({params}){ret_part}")
        elif kind == "class":
            ext = exp.get("extends")
            ext_part = f" extends {ext}" if ext else ""
            lines.append(f"    export class {name}{ext_part}")
        elif kind == "const":
            t = exp.get("type")
            t_part = f": {t}" if t else ""
            lines.append(f"    export const {name}{t_part}")
        elif kind == "enum":
            lines.append(f"    export enum {name}")
        elif kind == "reexport":
            lines.append(f"    export {{ {name} }}")
        elif kind == "default-ref":
            lines.append(f"    export default {name}")

    for iface in interfaces:
        members = iface.get("members") or []
        if members:
            preview_members = []
            for m in members[:8]:
                if m.get("kind") == "method":
                    params = ", ".join(_format_ts_param(p) for p in (m.get("params") or []))
                    rt = m.get("return_type") or ""
                    rt_part = f": {rt}" if rt else ""
                    preview_members.append(f"{m['name']}({params}){rt_part}")
                else:
                    t = m.get("type") or "any"
                    preview_members.append(f"{m['name']}: {t}")
            members_str = "; ".join(preview_members)
            lines.append(f"    export interface {iface['name']} {{ {members_str} }}")
        else:
            lines.append(f"    export interface {iface['name']}")

    for ta in types:
        defn = ta.get("definition") or "..."
        if len(defn) > 120:
            defn = defn[:117] + "..."
        lines.append(f"    export type {ta['name']} = {defn}")

    for store in stores:
        members = ", ".join(store.get("members") or [])
        type_arg = store.get("type_arg") or ""
        ta_part = f"<{type_arg}>" if type_arg else ""
        lines.append(f"    STORE {store['name']}{ta_part} — exposed members: {members}")

    return "\n".join(lines)


def _format_ts_param(p: Dict) -> str:
    s = p.get("name", "?")
    if p.get("type"):
        s += f": {p['type']}"
    if p.get("default"):
        s += f" = {p['default']}"
    return s
