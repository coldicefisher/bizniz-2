"""Deterministic post-seed substitutions for skeleton workspaces.

Skeletons ship with hardcoded service references that assume the
Architect names services in a specific way (e.g. the react skeleton's
``vite.config.ts`` proxies ``/api → http://api:8000``, assuming the
backend is called ``api``). When the Architect names the backend
``backend`` or anything else, the SPA's API calls 502 in the browser.

This module runs after ``seed_workspace`` and rewrites the known
hardcoded refs to match the actual architecture. Pure str-replace —
no LLM, no AST.

Each substitution is keyed by ``skeleton.name`` and declares:
  - ``file``       — workspace-relative path to edit
  - ``find``       — exact string to replace (a placeholder or known default)
  - ``compute``    — function ``(architecture, service) -> str`` returning the replacement

If the file or the ``find`` pattern is missing, we log and skip — never
crash. The skeleton may have moved on between Bizniz versions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture


@dataclass(frozen=True)
class _Substitution:
    file: str
    find: str
    compute: Callable[[SystemArchitecture, ServiceDefinition], Optional[str]]


def _backend_internal_url(arch: SystemArchitecture, _self: ServiceDefinition) -> Optional[str]:
    """``http://<backend-service-name>:<container-port>`` for the
    architecture's primary backend. Returns None when there's no
    backend service (worker-only / pure-frontend projects).

    The container port is the IN-NETWORK port — for a FastAPI service
    where the Dockerfile binds uvicorn to 8000, this is 8000 regardless
    of what host port the provisioner remapped to. Containers on the
    docker network talk to each other on the original (non-remapped)
    port; the host-port remap only matters for browser-to-host.
    """
    for svc in arch.services:
        if (svc.service_type or "").lower() == "backend":
            # The skeleton's Dockerfile binds at a known port — for
            # the fastapi skeleton it's 8000. We can't easily read the
            # Dockerfile here, so we encode the skeleton convention.
            # Cross-check: ``app_python.py`` template uses 8000.
            port = 8000 if (svc.framework or "").lower() == "fastapi" else 8000
            return f"http://{svc.name}:{port}"
    return None


# Per-skeleton substitution tables. Add an entry when a new
# skeleton ships hardcoded service refs that the Architect can't
# guarantee.
_SUBSTITUTIONS: dict = {
    "react": [
        # vite proxy target: ``http://api:8000`` is the skeleton's
        # default; rewrite to the actual backend service.
        _Substitution(
            file="vite.config.ts",
            find='target: "http://api:8000"',
            compute=lambda arch, svc: (
                f'target: "{_backend_internal_url(arch, svc)}"'
                if _backend_internal_url(arch, svc) else None
            ),
        ),
    ],
    # angular / teams-frontend / others can register here when they
    # ship similar hardcoded refs. Empty list = no substitutions.
}


def apply_substitutions(
    skeleton_name: str,
    workspace_root: Path,
    architecture: SystemArchitecture,
    service: ServiceDefinition,
    on_status: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Run the substitution table for ``skeleton_name`` over the
    workspace. Returns the list of (file:find) pairs that were applied.

    Never raises. Missing file or missing pattern just logs and
    moves on — the skeleton may not have shipped that file in this
    version, and the substitution is additive.
    """
    subs = _SUBSTITUTIONS.get(skeleton_name) or []
    applied: List[str] = []
    for sub in subs:
        path = workspace_root / sub.file
        if not path.exists():
            if on_status:
                on_status(
                    f"Provisioner: skeleton substitution skipped — "
                    f"{sub.file} not in '{skeleton_name}' workspace"
                )
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            if on_status:
                on_status(
                    f"Provisioner: skeleton substitution read failed for "
                    f"{sub.file} ({type(e).__name__}: {e})"
                )
            continue
        if sub.find not in content:
            if on_status:
                on_status(
                    f"Provisioner: skeleton substitution skipped — "
                    f"pattern not found in {sub.file} "
                    f"({sub.find[:60]!r})"
                )
            continue
        replacement = sub.compute(architecture, service)
        if not replacement:
            if on_status:
                on_status(
                    f"Provisioner: skeleton substitution skipped — "
                    f"compute() returned None for {sub.file} "
                    f"(probably no matching service in architecture)"
                )
            continue
        new_content = content.replace(sub.find, replacement)
        path.write_text(new_content, encoding="utf-8")
        applied.append(f"{sub.file}:{sub.find[:50]}")
        if on_status:
            on_status(
                f"Provisioner: applied skeleton substitution in "
                f"{sub.file} ({sub.find[:40]!r} → {replacement[:60]!r})"
            )
    return applied
