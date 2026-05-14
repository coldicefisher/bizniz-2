"""Deterministic frontend route discovery.

Three tiers, in order of preference:

  1. **Known-framework parser** — pattern-matched parsing for the
     frameworks our skeletons emit (React Router, Angular Router).
     Cheap (no LLM), reliable for projects that follow the skeleton.

  2. **Agent fallback** — when Tier 1 returns nothing, dispatch a
     short claude --print session with Read/Glob/Grep so the agent
     walks the code and returns a JSON route list. Owned by the
     caller (ProUXDesigner) — this module exposes only the prompt
     + parser.

  3. **Design plan fallback** — if both fail, the caller falls back
     to the design plan's per_view_plan route list. Not implemented
     here; pro_ux_designer handles that bridge.

Returned routes are normalized as ``RouteSpec``: literal path
(``/recipes/:id``), param names (``["id"]``), is_dynamic flag, and
provenance (which source file emitted it). Dynamic routes get
captured once per pattern — callers seed via API + substitute.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class RouteSpec(BaseModel):
    path: str
    params: List[str] = Field(default_factory=list)
    is_dynamic: bool = False
    source_file: Optional[str] = None


# ── Tier 1: framework-specific parsers ──────────────────────────────────


_REACT_PATH_RE = re.compile(
    r"""path\s*:\s*['"`]([^'"`]+)['"`]""",
)
# JSX-style: <Route path="/foo" element={...}/>
_REACT_JSX_PATH_RE = re.compile(
    r"""<Route[^>]*\bpath\s*=\s*['"`]([^'"`]+)['"`]""",
)
# Angular: { path: 'foo', component: FooComponent }
_ANGULAR_PATH_RE = re.compile(
    r"""path\s*:\s*['"`]([^'"`]+)['"`]""",
)


def discover_react_routes(workspace_root: Path) -> List[RouteSpec]:
    """Parse a React Router project for declared route paths.

    Recognises two patterns:
      - One file per route at ``src/routes/*.tsx`` with a default
        export of ``{ path: '/foo', element: <Foo /> }`` (the recipe_box
        / bizniz skeleton convention).
      - JSX ``<Route path="/foo" ...>`` inside ``src/App.tsx``,
        ``src/main.tsx``, or a router config module.
    """
    out: List[RouteSpec] = []
    seen: set = set()

    routes_dir = workspace_root / "src" / "routes"
    if routes_dir.is_dir():
        for fp in sorted(routes_dir.glob("*.tsx")):
            if fp.name.endswith((".test.tsx", ".spec.tsx")):
                continue
            text = _safe_read(fp)
            if not text:
                continue
            for m in _REACT_PATH_RE.finditer(text):
                path = _normalize_path(m.group(1))
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append(_make_route(path, str(fp.relative_to(workspace_root))))

    # Inline JSX <Route path="..." /> declarations.
    for rel in (
        "src/App.tsx", "src/App.jsx",
        "src/main.tsx", "src/main.jsx",
        "src/routes.tsx", "src/router.tsx",
        "src/index.tsx",
    ):
        fp = workspace_root / rel
        if not fp.is_file():
            continue
        text = _safe_read(fp)
        for m in _REACT_JSX_PATH_RE.finditer(text):
            path = _normalize_path(m.group(1))
            if not path or path in seen:
                continue
            seen.add(path)
            out.append(_make_route(path, rel))

    return out


def discover_angular_routes(workspace_root: Path) -> List[RouteSpec]:
    """Parse an Angular Router project for declared route paths.

    Recognises ``{ path: 'foo', component: FooComponent }`` entries
    inside ``Routes`` arrays declared in ``*-routing.module.ts``,
    ``app.routes.ts``, or ``src/app/**/*.routes.ts``.
    """
    out: List[RouteSpec] = []
    seen: set = set()

    candidates: List[Path] = []
    for pattern in (
        "src/app/**/*-routing.module.ts",
        "src/app/**/*.routes.ts",
        "src/app/app-routing.module.ts",
        "src/app/app.routes.ts",
    ):
        candidates.extend(workspace_root.glob(pattern))

    for fp in sorted(set(candidates)):
        text = _safe_read(fp)
        if not text or "Routes" not in text and "RouterModule" not in text:
            # Skip files that don't smell like a router config.
            continue
        for m in _ANGULAR_PATH_RE.finditer(text):
            raw = m.group(1).strip()
            # Angular paths are usually written without a leading slash.
            # Normalize to ``/foo`` for consistency with React's shape.
            normalized = "/" + raw.lstrip("/") if raw else "/"
            if normalized == "/" and raw == "":
                # The empty-string path means "redirect index" — keep
                # it as "/" only once.
                pass
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append(_make_route(
                normalized, str(fp.relative_to(workspace_root)),
            ))

    return out


# ── Dispatcher ──────────────────────────────────────────────────────────


def discover_routes(
    workspace_root: Path,
    framework: Optional[str] = None,
) -> List[RouteSpec]:
    """Pick the right Tier 1 parser by framework signature.

    Returns ``[]`` when the framework is unknown or no routes are
    found — the caller should then fall through to Tier 2 (agent).
    """
    fw = (framework or "").lower()
    if fw in ("react", "react-router", "react-vite", "vite-react"):
        return discover_react_routes(workspace_root)
    if fw == "angular":
        return discover_angular_routes(workspace_root)

    # Unknown framework — try React (most common), then Angular, and
    # let the caller fall to the agent if both empty.
    react = discover_react_routes(workspace_root)
    if react:
        return react
    angular = discover_angular_routes(workspace_root)
    if angular:
        return angular
    return []


# ── Tier 2: agent fallback prompt + parser ──────────────────────────────


AGENT_DISCOVERY_SYSTEM_PROMPT = """\
You are a route discovery agent. Your job is to walk a frontend
codebase and emit the canonical list of user-visible URL routes.

Workflow:
  - Use Glob to find router config files. Common locations:
    React Router: src/routes/*.tsx, src/App.tsx, src/main.tsx,
                  src/router.tsx
    Angular:      src/app/**/*-routing.module.ts,
                  src/app/**/*.routes.ts
    Next.js:      app/**/page.tsx, pages/**/*.tsx
    Vue Router:   src/router/index.ts, src/router.ts
    SvelteKit:    src/routes/**/+page.svelte
  - Read each one, extract every declared route path.
  - Normalize: leading slash, no trailing slash except "/".
  - Mark dynamic routes (``:id``, ``[id]``, ``:slug``, etc.) and list
    the parameter names.

Output a SINGLE JSON object as your last message — no markdown
fences, no prose:

{
  "routes": [
    {"path": "/", "params": [], "is_dynamic": false, "source_file": "src/routes/home.tsx"},
    {"path": "/recipes/:id", "params": ["id"], "is_dynamic": true, "source_file": "src/App.tsx"}
  ],
  "notes": "any caveats — e.g. routes loaded at runtime from a CMS"
}
"""


AGENT_DISCOVERY_USER_TEMPLATE = """\
PROJECT FRAMEWORK: {framework}
WORKSPACE: the directory you have access to

Walk the codebase and return the route list as JSON per the system
prompt. If you find none, return ``{{"routes": [], "notes": "..."}}``
with a one-sentence explanation.
"""


# ── Helpers ─────────────────────────────────────────────────────────────


_PARAM_RE = re.compile(r":([A-Za-z_]\w*)")


def _make_route(path: str, source_file: Optional[str]) -> RouteSpec:
    params = _PARAM_RE.findall(path)
    return RouteSpec(
        path=path,
        params=params,
        is_dynamic=bool(params),
        source_file=source_file,
    )


def _normalize_path(p: str) -> str:
    p = p.strip()
    if not p:
        return ""
    # Strip unrelated artifacts that sometimes appear in matches.
    if " " in p or "\n" in p or "\t" in p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


def _safe_read(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


# ── Tier 2: claude --print agent fallback ──────────────────────────────


_ALLOWED_TOOLS_DISCOVERY = ["Read", "Glob", "Grep"]
_AGENT_TIMEOUT_S = 600.0


def agent_discover_routes(
    workspace_root: Path,
    framework: Optional[str] = None,
    command: str = "claude",
    timeout_seconds: float = _AGENT_TIMEOUT_S,
    on_status=None,
) -> List[RouteSpec]:
    """Shell out to ``claude --print --add-dir <workspace>`` and ask it
    to walk the code, returning a JSON route list. Falls back to ``[]``
    on any subprocess failure (caller decides whether to use the plan's
    per_view_plan instead).
    """
    if shutil.which(command) is None:
        return []

    def _log(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    user_prompt = AGENT_DISCOVERY_USER_TEMPLATE.format(
        framework=framework or "unknown",
    )
    cmd = [
        command, "--print",
        "--output-format=json",
        "--append-system-prompt", AGENT_DISCOVERY_SYSTEM_PROMPT,
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", " ".join(_ALLOWED_TOOLS_DISCOVERY),
        "--add-dir", str(workspace_root),
    ]
    try:
        proc = subprocess.run(
            cmd, input=user_prompt,
            capture_output=True, text=True,
            timeout=timeout_seconds,
            cwd=str(workspace_root),
        )
    except subprocess.TimeoutExpired:
        _log(f"agent_discover_routes: timed out after {timeout_seconds:.0f}s")
        return []
    except FileNotFoundError:
        return []

    if proc.returncode != 0:
        _log(
            f"agent_discover_routes: claude exited {proc.returncode}: "
            f"{(proc.stderr or '')[:200]}"
        )
        return []

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if payload.get("is_error"):
        return []
    parsed = _parse_agent_result(payload.get("result") or "")
    if parsed is None:
        return []
    out: List[RouteSpec] = []
    seen: set = set()
    for entry in parsed.get("routes") or []:
        path = _normalize_path(str(entry.get("path") or ""))
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(_make_route(path, entry.get("source_file")))
    return out


def _parse_agent_result(text: str):
    """Extract trailing JSON from the agent's response. Mirrors the
    parser shape ClaudeUXDesigner uses — direct, fenced, then
    forward-scan-from-first-brace."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # Fenced.
    fenced = re.search(
        r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL,
    )
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Forward-scan.
    first = text.find("{")
    if first == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(first, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[first:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ── Combined discovery: Tier 1 then Tier 2 ──────────────────────────────


def discover_routes_with_fallback(
    workspace_root: Path,
    framework: Optional[str] = None,
    *,
    enable_agent_fallback: bool = True,
    command: str = "claude",
    on_status=None,
) -> List[RouteSpec]:
    """Try the deterministic parser first; if empty AND the agent
    fallback is enabled, dispatch claude --print to walk the code."""
    routes = discover_routes(workspace_root, framework=framework)
    if routes or not enable_agent_fallback:
        return routes
    if on_status:
        try:
            on_status(
                "route_discovery: Tier 1 found nothing — falling back to "
                "claude --print agent"
            )
        except Exception:
            pass
    return agent_discover_routes(
        workspace_root, framework=framework,
        command=command, on_status=on_status,
    )
