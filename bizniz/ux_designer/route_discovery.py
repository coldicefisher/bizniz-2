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
from typing import Callable, List, Optional

from pydantic import BaseModel, Field


# Pluggable agent signature. Any callable matching this shape can
# stand in as a Tier 2 backend — Claude CLI today, Gemini or another
# model tomorrow when we A/B test architecture quality. Returns a
# list of route dicts (loosely shaped; the dispatcher normalizes
# them through ``_make_route``).
RouteDiscoveryAgent = Callable[[Path, Optional[str]], List[dict]]


class RouteSpec(BaseModel):
    path: str
    params: List[str] = Field(default_factory=list)
    is_dynamic: bool = False
    source_file: Optional[str] = None
    # Whether visiting this route unauthenticated redirects to login
    # / yields a denied state. Detected deterministically by grepping
    # for guard wrappers in the source file (React) or canActivate
    # in the route config (Angular). ``None`` = unknown / undetected
    # (caller may probe by curl). Public routes (``/``, ``/login``,
    # ``/register``) typically resolve to False.
    requires_auth: Optional[bool] = None
    # Comma-separated list of guard names found, for diagnostics
    # ("RequireAuth", "AdminRouteGuard", "canActivate"). Empty when
    # no guards detected.
    auth_signals: List[str] = Field(default_factory=list)


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

# React-style guard wrappers. Match conservatively — wrapping element
# names like ``<RequireAuth>`` or ``<AdminRouteGuard>``. ``Protected
# Route``, ``AuthGate``, ``RequireRole`` are common variants. Custom
# guard names get caught by the agent fallback (Tier 2).
_REACT_GUARD_RE = re.compile(
    r"""<\s*(RequireAuth|RequireRole|AdminRouteGuard|ProtectedRoute|AuthGate|RequireLogin)\b""",
    re.IGNORECASE,
)
# Angular: ``canActivate: [SomeGuard]`` or
# ``canActivateChild: [SomeGuard]`` — both signal auth/role checks.
_ANGULAR_GUARD_RE = re.compile(
    r"""\bcan(Activate|ActivateChild|Load)\s*:\s*\[""",
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
            guard_signals = _detect_react_guards(text)
            for m in _REACT_PATH_RE.finditer(text):
                path = _normalize_path(m.group(1))
                if not path or path in seen:
                    continue
                seen.add(path)
                spec = _make_route(
                    path, str(fp.relative_to(workspace_root)),
                )
                if guard_signals:
                    spec.requires_auth = True
                    spec.auth_signals = guard_signals
                elif _is_publicly_named_route(path):
                    spec.requires_auth = False
                out.append(spec)

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
        # File-level guard signals apply to every JSX <Route> that
        # appears inside a guard wrapper. We approximate by scanning
        # the line around each match — full JSX parsing would be
        # more accurate but is heavier than the value gives us.
        for m in _REACT_JSX_PATH_RE.finditer(text):
            path = _normalize_path(m.group(1))
            if not path or path in seen:
                continue
            seen.add(path)
            spec = _make_route(path, rel)
            # Look at the 200 chars before this match for a guard
            # wrapper ancestor.
            ctx = text[max(0, m.start() - 200):m.start()]
            ctx_guards = _detect_react_guards(ctx)
            if ctx_guards:
                spec.requires_auth = True
                spec.auth_signals = ctx_guards
            elif _is_publicly_named_route(path):
                spec.requires_auth = False
            out.append(spec)

    return out


def _detect_react_guards(text: str) -> List[str]:
    """Return the list of guard component names found in ``text``."""
    return sorted({m.group(1) for m in _REACT_GUARD_RE.finditer(text)})


def apply_conservative_auth_default(routes: List[RouteSpec]) -> None:
    """Fill in ``requires_auth=True`` for any route where detection
    returned None and the path doesn't match a publicly-named pattern.

    Rationale: guards sometimes live inside page components rather than
    in the route file (recipe_box's ``/dashboard`` is just
    ``<DashboardPage />`` — the auth check is inside the page). When
    we can't tell from a quick grep, the safer default is to assume
    protected: pre-authing for a route that's actually public still
    captures the right page, while skipping auth on a protected route
    captures the login redirect.

    Caller invokes this AFTER all parsers run if they want the safer
    default. Not applied automatically by ``discover_routes`` so tests
    and callers that care about the raw "unknown" signal still see it.
    """
    for r in routes:
        if r.requires_auth is None and not _is_publicly_named_route(r.path):
            r.requires_auth = True
            r.auth_signals = ["(conservative-default)"]


# Routes whose names strongly imply public access. We mark these as
# ``requires_auth=False`` so the screenshot script doesn't pre-auth
# for them. (Pre-auth still wouldn't hurt, but it lets the test more
# honestly capture the public marketing entry.)
_PUBLIC_ROUTE_NAMES = frozenset({
    "/", "/login", "/register", "/signup", "/sign-up", "/sign-in",
    "/forgot-password", "/reset-password", "/verify-email",
    "/about", "/pricing", "/contact", "/privacy", "/terms",
})


def _is_publicly_named_route(path: str) -> bool:
    return path in _PUBLIC_ROUTE_NAMES


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
            normalized = "/" + raw.lstrip("/") if raw else "/"
            if normalized in seen:
                continue
            seen.add(normalized)
            spec = _make_route(
                normalized, str(fp.relative_to(workspace_root)),
            )
            # Walk the ``Routes`` array entry for this path and check
            # if it declares canActivate. We use a small window
            # forward from the path match.
            window = text[m.end():m.end() + 400]
            guards = _ANGULAR_GUARD_RE.findall(window)
            if guards:
                spec.requires_auth = True
                spec.auth_signals = [f"can{g}" for g in guards]
            elif _is_publicly_named_route(normalized):
                spec.requires_auth = False
            out.append(spec)

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
    """Claude CLI implementation of the Tier 2 agent. Spawns
    ``claude --print --add-dir <workspace>`` so the model can use its
    native Read/Glob/Grep tools to walk the code.

    Returns ``[]`` on any subprocess failure. For a non-Claude backend
    (Gemini, OpenAI, etc.), use ``text_client_agent_discover_routes``,
    which renders the workspace into the prompt and makes a single
    text call.
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


# ── Text-client Tier 2: any BaseAIClient ──────────────────────────────


def text_client_agent_discover_routes(
    workspace_root: Path,
    framework: Optional[str] = None,
    *,
    client=None,
    on_status=None,
    max_files: int = 40,
    max_bytes_per_file: int = 8000,
) -> List[RouteSpec]:
    """BaseAIClient-driven route discovery. Renders the relevant
    frontend files into the prompt (Tier 1's candidate paths + a
    small src/ snapshot), then makes a single text call. Works with
    any client that implements ``BaseAIClient.get_text`` — Gemini,
    OpenAI, etc. — so we can A/B this against the Claude CLI agent.

    Tradeoff vs the Claude CLI variant: no native file-tool access,
    so we have to choose what to send. We bias toward router config
    files (much smaller window). Returns ``[]`` on parse failure or
    when no client is supplied.
    """
    if client is None:
        return []

    rendered = _render_router_candidates(
        workspace_root, max_files=max_files,
        max_bytes_per_file=max_bytes_per_file,
    )
    if not rendered:
        return []

    sys_prompt = AGENT_DISCOVERY_SYSTEM_PROMPT + (
        "\n\nIMPORTANT: you do NOT have file-system tools in this "
        "mode. Read ONLY the files included inline in the user "
        "message. Do not invent paths."
    )
    user_prompt = (
        AGENT_DISCOVERY_USER_TEMPLATE.format(framework=framework or "unknown")
        + "\n\nROUTER-CONFIG CANDIDATE FILES:\n\n"
        + rendered
    )

    try:
        text, _, _ = client.get_text(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            use_message_history=False,
        )
    except Exception as e:
        if on_status:
            try:
                on_status(
                    f"text_client_agent_discover_routes: client call "
                    f"raised {type(e).__name__}: {e}"
                )
            except Exception:
                pass
        return []

    parsed = _parse_agent_result(text or "")
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


def _render_router_candidates(
    workspace_root: Path,
    *,
    max_files: int,
    max_bytes_per_file: int,
) -> str:
    """Collect the small set of files most likely to declare routes
    and render them into a single string for the prompt."""
    patterns = (
        "src/routes/*.tsx", "src/routes/*.jsx",
        "src/routes/*.ts",  "src/routes/*.js",
        "src/App.tsx", "src/App.jsx",
        "src/main.tsx", "src/main.jsx",
        "src/router.tsx", "src/router.ts",
        "src/index.tsx", "src/index.jsx",
        "src/app/**/*-routing.module.ts",
        "src/app/**/*.routes.ts",
        "src/router/index.ts", "src/router/index.js",
        "app/**/page.tsx", "pages/**/*.tsx",  # Next.js
        "src/routes/**/+page.svelte",         # SvelteKit
    )
    found: List[Path] = []
    seen: set = set()
    for p in patterns:
        for fp in workspace_root.glob(p):
            if fp in seen or not fp.is_file():
                continue
            seen.add(fp)
            found.append(fp)
            if len(found) >= max_files:
                break
        if len(found) >= max_files:
            break

    parts = []
    for fp in found:
        text = _safe_read(fp)
        if not text:
            continue
        if len(text) > max_bytes_per_file:
            text = text[:max_bytes_per_file] + "\n... [truncated]"
        rel = fp.relative_to(workspace_root)
        parts.append(f"--- {rel} ---\n{text}")
    return "\n\n".join(parts)


# ── Combined discovery: Tier 1 then Tier 2 ──────────────────────────────


def discover_routes_with_fallback(
    workspace_root: Path,
    framework: Optional[str] = None,
    *,
    enable_agent_fallback: bool = True,
    agent_fn: Optional[RouteDiscoveryAgent] = None,
    command: str = "claude",
    on_status=None,
) -> List[RouteSpec]:
    """Tier 1 first; if empty AND fallback enabled, dispatch the
    pluggable agent.

    ``agent_fn`` is the swappable backend. When ``None``, the default
    is the Claude CLI variant (``agent_discover_routes``). Pass any
    ``Callable[[Path, Optional[str]], List[RouteSpec]]`` to swap in
    Gemini, OpenAI, or your own — useful for A/B-ing architecture
    quality against different model backends.
    """
    routes = discover_routes(workspace_root, framework=framework)
    if routes or not enable_agent_fallback:
        return routes
    if on_status:
        try:
            on_status(
                "route_discovery: Tier 1 found nothing — falling "
                "through to Tier 2 agent"
            )
        except Exception:
            pass
    fn = agent_fn or (
        lambda ws, fw: agent_discover_routes(
            ws, framework=fw, command=command, on_status=on_status,
        )
    )
    return fn(workspace_root, framework)
