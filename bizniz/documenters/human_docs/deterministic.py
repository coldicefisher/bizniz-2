"""Deterministic doc generators — data-driven, no LLM calls.

Each function produces one Markdown doc from structured input
(``SystemArchitecture``, compose YAML, OpenAPI JSON). They're
fast, reproducible, and never need an API call.

Pairs with ``llm.py`` for the LLM-narrative half of the hybrid
doc generator.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.documenters.human_docs.mermaid import render_service_graph


# ── architecture.md ──────────────────────────────────────────────


def render_architecture(arch: SystemArchitecture) -> str:
    """Top-level architecture doc.

    Sections:
    - Project overview (slug + name + description)
    - Service map (Mermaid graph)
    - Service table (name / type / framework / language / port)
    - Environments (stub — filled when CI/CD lands)
    - Cross-cutting concerns (auth, db, observability)
    """
    out: List[str] = []
    p = out.append
    p(f"# Architecture — {arch.project_name}")
    p("")
    p(f"**Slug:** `{arch.project_slug}`")
    if arch.description:
        p("")
        p(arch.description)
    p("")

    p("## Service map")
    p("")
    p(render_service_graph(arch))
    p("")

    p("## Services")
    p("")
    p("| Name | Type | Framework | Language | Port | Workspace |")
    p("|---|---|---|---|---:|---|")
    for svc in arch.services:
        port = str(svc.port) if svc.port else "—"
        ws = svc.workspace_name or svc.name
        p(
            f"| {svc.name} | {svc.service_type or '?'} | "
            f"{svc.framework or '?'} | {svc.language or '?'} | "
            f"{port} | `{ws}/` |"
        )
    p("")

    p("## Cross-cutting concerns")
    p("")
    auth_services = [s for s in arch.services if (s.service_type or "").lower() == "auth"]
    db_services = [s for s in arch.services if (s.service_type or "").lower() == "database"]
    if auth_services:
        p(
            "- **Authentication:** "
            + ", ".join(f"`{s.name}` ({s.framework})" for s in auth_services)
            + " — see [`auth.md`](auth.md) for the contract."
        )
    if db_services:
        p(
            "- **Persistence:** "
            + ", ".join(f"`{s.name}` ({s.framework})" for s in db_services)
        )
    p("- **Observability:** structured logging per service "
      "(see `infrastructure.md`).")
    p("")

    p("## Environments")
    p("")
    p(
        "**Status:** Currently development-only. Production / staging "
        "deployment topology, CI/CD, and live-server documentation will "
        "be filled in here once those land. This section is a stable "
        "anchor — links to it from elsewhere in the docs won't break "
        "when content lands."
    )
    p("")
    p("- `development` — `docker-compose` stack at `infra/development/`.")
    p("- `staging` — TBD.")
    p("- `production` — TBD.")
    p("")
    return "\n".join(out)


# ── infrastructure.md ────────────────────────────────────────────


def render_infrastructure(
    compose_yaml: str,
    architecture: SystemArchitecture,
) -> str:
    """Infrastructure / topology doc generated from the compose
    file. Tabulates services + ports + volumes + networks."""
    try:
        compose = yaml.safe_load(compose_yaml)
    except Exception:
        compose = None
    if not isinstance(compose, dict):
        # Empty / non-dict / malformed YAML — render an empty-state
        # doc rather than crash. Operator can re-run docs after fixing.
        compose = {}
    services = compose.get("services") or {}
    networks = compose.get("networks") or {}
    volumes = compose.get("volumes") or {}

    out: List[str] = []
    p = out.append
    p("# Infrastructure")
    p("")
    p(
        "Generated from `infra/development/docker-compose.yml`. "
        "Rebuilt every time the Provisioner re-renders the compose "
        "file."
    )
    p("")

    p("## Service containers")
    p("")
    p("| Service | Image | Ports (host:container) | Depends on |")
    p("|---|---|---|---|")
    for name in sorted(services.keys()):
        svc = services[name] or {}
        image = svc.get("image", "?")
        ports = ", ".join(svc.get("ports") or []) or "—"
        deps_block = svc.get("depends_on") or {}
        if isinstance(deps_block, dict):
            deps = ", ".join(sorted(deps_block.keys())) or "—"
        elif isinstance(deps_block, list):
            deps = ", ".join(sorted(deps_block)) or "—"
        else:
            deps = "—"
        p(f"| {name} | `{image}` | {ports} | {deps} |")
    p("")

    p("## Networks")
    p("")
    if networks:
        for n in sorted(networks.keys()):
            p(f"- `{n}`")
    else:
        p("(none)")
    p("")

    p("## Volumes (per-service mounts)")
    p("")
    p("| Service | Mounts |")
    p("|---|---|")
    for name in sorted(services.keys()):
        svc = services[name] or {}
        mounts = svc.get("volumes") or []
        rendered = "<br/>".join(f"`{m}`" for m in mounts) if mounts else "—"
        p(f"| {name} | {rendered} |")
    p("")

    p("## Top-level volumes")
    p("")
    if volumes:
        for v in sorted(volumes.keys()):
            p(f"- `{v}`")
    else:
        p("(none)")
    p("")

    p("## Shared core library mounts")
    p("")
    p(
        "Refactorer-managed shared code (roadmap item 6) lives at "
        "`core/python/` and `core/typescript/`. Python services "
        "mount it at `/python_core/` with `PYTHONPATH` set so "
        "imports like `from python_core.data_types.time_instant "
        "import TimeInstant` resolve. TypeScript services mount at "
        "`/ts_core/` with `NODE_PATH` set."
    )
    p("")
    return "\n".join(out)


# ── api/<service>.md ─────────────────────────────────────────────


def render_api_reference(
    service_name: str,
    openapi: Dict[str, Any],
) -> str:
    """Per-service API reference from a captured OpenAPI document.

    Tabulates each path + method + summary + auth requirements.
    Doesn't dive into request/response bodies — that's better
    served by hitting `/docs` on the running service. This doc is
    the "what endpoints exist" overview.
    """
    out: List[str] = []
    p = out.append
    info = openapi.get("info") or {}
    title = info.get("title") or service_name
    version = info.get("version") or "?"
    p(f"# API Reference — {service_name}")
    p("")
    p(f"- **Title:** {title}")
    p(f"- **Version:** {version}")
    p(f"- **Service:** `{service_name}`")
    p("")
    p(
        "Generated from the running service's `/openapi.json` "
        "captured during the integration phase. For interactive "
        "exploration, run the stack and visit "
        f"`http://localhost:<host_port>/docs`."
    )
    p("")

    paths = openapi.get("paths") or {}
    if not paths:
        p("_No paths captured._")
        return "\n".join(out)

    p("## Endpoints")
    p("")
    p("| Method | Path | Summary | Auth | Tags |")
    p("|---|---|---|---|---|")
    for path in sorted(paths.keys()):
        for method, op in (paths[path] or {}).items():
            if method.lower() not in (
                "get", "post", "put", "patch", "delete",
            ):
                continue
            summary = (op or {}).get("summary") or ""
            tags = ", ".join((op or {}).get("tags") or []) or "—"
            auth_required = bool((op or {}).get("security"))
            auth = "🔒 yes" if auth_required else "—"
            p(
                f"| `{method.upper()}` | `{path}` | "
                f"{summary[:80]} | {auth} | {tags} |"
            )
    p("")
    return "\n".join(out)


# ── auth.md ──────────────────────────────────────────────────────


def render_auth_pointer(
    architecture: SystemArchitecture,
    auth_contract_path: str = "../AUTH_CONTRACT.md",
) -> str:
    """Thin pointer to the root-level AUTH_CONTRACT.md.

    The contract itself is a top-level artifact (visible next to
    README); this doc explains where it is and summarizes the
    services that participate in auth.
    """
    out: List[str] = []
    p = out.append
    p("# Authentication")
    p("")
    p(
        f"The auth contract lives at "
        f"[`{auth_contract_path}`]({auth_contract_path}) (root of "
        "the project). It describes:"
    )
    p("")
    p("- The auth provider configuration (FusionAuth realm + app)")
    p("- Issued JWT claims and validation rules")
    p("- Seed test users + roles")
    p("- The login flow (public — no API key required)")
    p("")

    auth_services = [
        s for s in architecture.services
        if (s.service_type or "").lower() == "auth"
    ]
    if auth_services:
        p("## Auth services in this project")
        p("")
        p("| Service | Framework | Port |")
        p("|---|---|---:|")
        for svc in auth_services:
            port = str(svc.port) if svc.port else "—"
            p(f"| {svc.name} | {svc.framework} | {port} |")
        p("")

    api_consumers = [
        s for s in architecture.services
        if (s.service_type or "").lower() == "backend"
    ]
    if api_consumers:
        p("## Services that consume auth")
        p("")
        for svc in api_consumers:
            p(
                f"- **{svc.name}** — validates JWTs on every "
                f"authenticated route. See "
                f"`{svc.workspace_name or svc.name}/AUTH_CONTRACT.md` "
                "for the per-service copy."
            )
        p("")
    return "\n".join(out)
