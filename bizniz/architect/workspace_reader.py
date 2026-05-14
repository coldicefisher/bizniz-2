"""Architect's evolve-mode workspace reader.

Walks ``<project_root>/docs/<service>/`` to surface "what's already
built and documented" into the architect's evolve prompt. Used at
the start of M2+ planning so the architect doesn't re-imagine
services that already exist — it sees concrete extension points
(routes already defined, schemas already shaped, store members
already exposed) and decides what to extend.

Output is a compact text block embedded directly in the prompt.
We deliberately summarize aggressively: full api.json artifacts
can be 50KB+; the architect doesn't need every type alias, just a
sense of "what shape is this service in right now."
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def format_existing_workspace_state(project_root: Path, max_chars: int = 6000) -> str:
    """Walk ``<project_root>/docs/<service>/code/api.json`` for every
    service that has docs, return a compact summary.

    Returns empty string if the docs directory doesn't exist or has
    no service folders. Bounded by ``max_chars`` so a project with
    many services can't blow the prompt budget.
    """
    project_root = Path(project_root)
    docs_root = project_root / "docs"
    if not docs_root.is_dir():
        return ""

    sections = []
    used = 0
    header = (
        "EXISTING WORKSPACE STATE (what each service already has on "
        "disk — extend these, don't recreate them):\n"
    )

    for service_dir in sorted(docs_root.iterdir()):
        if not service_dir.is_dir():
            continue
        api_json_path = service_dir / "code" / "api.json"
        if not api_json_path.exists():
            continue

        try:
            doc = json.loads(api_json_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        section = _summarize_service(service_dir.name, doc)
        if not section:
            continue
        if used + len(section) > max_chars:
            sections.append(f"... ({len(list(docs_root.iterdir()))} total services; rest truncated)")
            break
        sections.append(section)
        used += len(section)

    if not sections:
        return ""
    return header + "\n".join(sections) + "\n"


def _summarize_service(service_name: str, doc: dict) -> str:
    """One-line per file: counts of classes/functions/exports.

    The architect doesn't need to read every signature — it needs
    a sense of "this service has 5 routes and 3 schemas" so it
    can reason about extending them.
    """
    files = doc.get("files") or {}
    if not files:
        return ""
    language = doc.get("language", "?")
    lines = [f"\n  {service_name} ({language}, {len(files)} files):"]

    if language == "python":
        # Count routes vs schemas vs core, etc., based on path
        routes = []
        schemas = []
        models = []
        other = []
        for path, file_doc in sorted(files.items()):
            if "/routes/" in path or path.endswith("/routes.py"):
                routes.append((path, file_doc))
            elif "/schemas/" in path:
                schemas.append((path, file_doc))
            elif "/models/" in path:
                models.append((path, file_doc))
            else:
                other.append((path, file_doc))

        if routes:
            lines.append(f"    routes ({len(routes)} files):")
            for path, file_doc in routes[:6]:
                fn_names = [f["name"] for f in (file_doc.get("functions") or [])][:5]
                lines.append(f"      {path}: {', '.join(fn_names) if fn_names else '(none)'}")
        if schemas:
            lines.append(f"    schemas ({len(schemas)} files):")
            for path, file_doc in schemas[:6]:
                cls_names = [c["name"] for c in (file_doc.get("classes") or [])][:8]
                lines.append(f"      {path}: {', '.join(cls_names) if cls_names else '(none)'}")
        if models:
            lines.append(f"    models ({len(models)} files):")
            for path, file_doc in models[:6]:
                cls_names = [c["name"] for c in (file_doc.get("classes") or [])][:5]
                lines.append(f"      {path}: {', '.join(cls_names) if cls_names else '(none)'}")
        if other:
            lines.append(f"    other ({len(other)} files): " +
                         ", ".join(p for p, _ in other[:6]))

    elif language == "typescript":
        # Group by typical React-ish path conventions
        pages = []
        routes = []
        stores = []
        api_files = []
        types_files = []
        components = []
        other = []
        for path, file_doc in sorted(files.items()):
            if "/pages/" in path or path.endswith("Page.tsx"):
                pages.append((path, file_doc))
            elif "/routes/" in path:
                routes.append((path, file_doc))
            elif "/stores/" in path:
                stores.append((path, file_doc))
            elif "/api/" in path:
                api_files.append((path, file_doc))
            elif "/types/" in path:
                types_files.append((path, file_doc))
            elif "/components/" in path:
                components.append((path, file_doc))
            else:
                other.append((path, file_doc))

        for label, group in [
            ("stores", stores), ("api", api_files), ("types", types_files),
            ("pages", pages), ("routes", routes), ("components", components),
        ]:
            if not group:
                continue
            lines.append(f"    {label} ({len(group)} files):")
            for path, file_doc in group[:6]:
                # For stores, surface members directly — that's the
                # contract that prevents the LoginPage/authStore bug
                # from recurring.
                if label == "stores" and (file_doc.get("stores") or []):
                    for store in file_doc["stores"][:2]:
                        members = ", ".join(store.get("members") or [])
                        lines.append(
                            f"      {path}: store {store['name']} "
                            f"members=[{members}]"
                        )
                else:
                    summary = []
                    for exp in (file_doc.get("exports") or [])[:5]:
                        summary.append(exp.get("name", "?"))
                    lines.append(f"      {path}: {', '.join(summary) if summary else '(none)'}")
        if other:
            lines.append(f"    other ({len(other)} files): " +
                         ", ".join(p for p, _ in other[:4]))

    return "\n".join(lines)
