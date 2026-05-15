"""Dynamic-route resolver — turn ``/recipes/:id`` into a concrete URL.

Given a list of dynamic ``RouteSpec`` entries and a running backend,
return one concrete URL per template that loads the corresponding
frontend view correctly. The resolver agent picks its own strategy
per route — read an existing list, POST a new record, use a known
fixture — and we just take the result.

Pluggable across LLM backends, mirroring ``route_discovery``:
  - ``claude_resolve_routes`` shells out to ``claude --print`` with
    Read/Glob/Grep/Bash so the model can probe the live API.
  - ``text_client_resolve_routes`` (BaseAIClient) pre-renders the view
    source + OpenAPI summary into the prompt for models without native
    file/shell tools. Useful for A/B'ing architecture quality.

Cache + staleness:
  - Resolved routes persist to ``<workspace>/.bizniz/ux_resolved_routes.json``.
  - Before each run, the harness GETs ``verification_url`` against the
    running backend; 404 (or other non-auth 4xx) invalidates the entry
    and triggers a re-resolve. Auth errors (401/403) leave the entry
    alone — we have no token in the validation call.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.ux_designer.route_discovery import RouteSpec


RESOLVED_FILENAME = "ux_resolved_routes.json"
CACHE_VERSION = 1
_DEFAULT_AGENT_TIMEOUT_S = 600.0


class ResolvedRoute(BaseModel):
    """One template → concrete URL mapping produced by the resolver."""

    template: str
    # The URL the frontend should navigate to (path-only, relative to
    # the SPA's base — e.g. ``/recipes/abc-123``).
    concrete_url: str
    # The backend API URL we'll GET to verify the underlying resource
    # still exists between runs (path-only — e.g. ``/api/recipes/abc-123``).
    # Empty string means the resolver couldn't identify a verification
    # endpoint; the harness skips staleness checks for this entry.
    verification_url: str = ""
    # How the agent obtained the id. Free-form, for diagnostics:
    # ``created_via_post``, ``existing_from_list``, ``fixture``, ``other``.
    strategy: str = "other"
    notes: str = ""
    resolved_at: datetime = Field(default_factory=datetime.utcnow)


# Pluggable agent signature. Any callable matching this shape can
# stand in as the resolver backend — Claude CLI today, Gemini or
# another model tomorrow. Returns a list of ResolvedRoute (one per
# input template). The agent is responsible for any HTTP traffic it
# wants to do; the harness only does pre-check / post-verify.
RouteResolverAgent = Callable[
    [Path, List[RouteSpec], str, Optional[str]],
    List[ResolvedRoute],
]


# ── Cache I/O ────────────────────────────────────────────────────────────


def cache_path(workspace_root: Path) -> Path:
    return workspace_root / ".bizniz" / RESOLVED_FILENAME


def load_cache(workspace_root: Path) -> Dict[str, ResolvedRoute]:
    """Return ``{template: ResolvedRoute}``. Empty dict when missing
    or corrupt — caller treats that as a full cache miss."""
    fp = cache_path(workspace_root)
    if not fp.exists():
        return {}
    try:
        payload = json.loads(fp.read_text())
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("version") != CACHE_VERSION:
        return {}
    out: Dict[str, ResolvedRoute] = {}
    for tmpl, raw in (payload.get("entries") or {}).items():
        try:
            out[tmpl] = ResolvedRoute.model_validate(raw)
        except Exception:
            continue
    return out


def save_cache(
    workspace_root: Path,
    entries: Dict[str, ResolvedRoute],
) -> None:
    fp = cache_path(workspace_root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "saved_at": datetime.utcnow().isoformat(),
        "entries": {
            t: json.loads(r.model_dump_json()) for t, r in entries.items()
        },
    }
    fp.write_text(json.dumps(payload, indent=2))


# ── Staleness check ──────────────────────────────────────────────────────


def is_still_valid(
    resolved: ResolvedRoute,
    backend_url: Optional[str],
    timeout_s: float = 5.0,
) -> bool:
    """Probe the verification_url. Returns True when the cached entry
    looks usable, False when it should be re-resolved.

    Rules:
      - No backend_url or no verification_url → assume valid (best-effort).
      - 2xx → valid.
      - 401/403 → valid (auth-protected; we don't have a token here,
        but the resource still exists — re-resolving wouldn't help).
      - 404/410 → invalid (resource gone).
      - Other 4xx / 5xx / network errors → valid (don't burn cache on
        transient failures).
    """
    if not backend_url or not resolved.verification_url:
        return True
    url = _join_url(backend_url, resolved.verification_url)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return True
        if e.code in (404, 410):
            return False
        return True
    except Exception:
        return True


def _join_url(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


# ── Top-level dispatcher ─────────────────────────────────────────────────


def resolve_dynamic_routes(
    workspace_root: Path,
    routes: List[RouteSpec],
    *,
    backend_url: Optional[str] = None,
    openapi_path: Optional[Path] = None,
    auth_contract: Optional[str] = None,
    agent_fn: Optional[RouteResolverAgent] = None,
    command: str = "claude",
    on_status: Optional[Callable[[str], None]] = None,
    timeout_seconds: float = _DEFAULT_AGENT_TIMEOUT_S,
) -> Dict[str, ResolvedRoute]:
    """Resolve every dynamic route in ``routes``. Returns the full
    template → ResolvedRoute map (cache hits + fresh resolutions).

    Workflow:
      1. Load cache from disk.
      2. For each cached entry, probe ``verification_url`` — drop
         stale entries.
      3. Collect templates without a valid cache entry.
      4. Dispatch the agent for the remaining templates (one call
         covers all of them — the agent picks per-route strategy).
      5. Merge fresh results into the cache map and persist.

    ``agent_fn=None`` picks the Claude CLI implementation by default.
    Pass any callable with the ``RouteResolverAgent`` signature to swap
    in Gemini, OpenAI, or your own.
    """
    def _log(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    dynamic = [r for r in routes if r.is_dynamic]
    if not dynamic:
        return {}

    cache = load_cache(workspace_root)
    # Drop stale entries up front so the remainder is the work list.
    valid_cache: Dict[str, ResolvedRoute] = {}
    dropped: List[str] = []
    for tmpl, entry in cache.items():
        if is_still_valid(entry, backend_url):
            valid_cache[tmpl] = entry
        else:
            dropped.append(tmpl)
    if dropped:
        _log(
            f"route_resolver: dropping {len(dropped)} stale cached "
            f"entr{'y' if len(dropped) == 1 else 'ies'}: {dropped[:4]}"
        )

    need_resolve = [
        r for r in dynamic if r.path not in valid_cache
    ]
    if not need_resolve:
        _log(
            f"route_resolver: all {len(dynamic)} dynamic route(s) "
            f"resolved from cache"
        )
        return valid_cache

    _log(
        f"route_resolver: resolving {len(need_resolve)} dynamic route(s): "
        f"{[r.path for r in need_resolve]}"
    )
    fn = agent_fn or (
        lambda ws, rts, base_url, auth: claude_resolve_routes(
            workspace_root=ws,
            routes=rts,
            backend_url=base_url,
            auth_contract=auth,
            openapi_path=openapi_path,
            command=command,
            on_status=on_status,
            timeout_seconds=timeout_seconds,
        )
    )
    fresh = fn(workspace_root, need_resolve, backend_url or "", auth_contract)
    merged: Dict[str, ResolvedRoute] = dict(valid_cache)
    for entry in fresh:
        merged[entry.template] = entry
    try:
        save_cache(workspace_root, merged)
    except Exception as e:
        _log(
            f"route_resolver: save_cache failed "
            f"({type(e).__name__}: {e}) — non-fatal"
        )
    return merged


# ── Resolver prompt (shared between Claude + text-client variants) ───────


RESOLVER_SYSTEM_PROMPT = """\
You are a dynamic-route resolver. For each frontend route template in
the input list, return a concrete URL that loads that view correctly.

Strategy is your call — pick whichever works for the project at hand:

  - **Read an existing list**: GET the relevant collection endpoint,
    pick the first id, build the concrete URL.
  - **Create a new record**: POST to the create endpoint with a minimal
    valid payload, use the returned id.
  - **Use a known fixture**: if the skeleton ships seed data with a
    known id, point at that.

You have:
  - The frontend view source (in the workspace).
  - An OpenAPI spec (when one is provided in the input).
  - Live HTTP access to the backend via Bash + curl. Use it.

For each input template, output one entry in this exact shape:

  {
    "template": "/recipes/:id",
    "concrete_url": "/recipes/abc-123",
    "verification_url": "/api/recipes/abc-123",
    "strategy": "created_via_post",
    "notes": "POST /api/recipes returned id abc-123 (minimal payload: title, ingredients)"
  }

Rules:
  - ``concrete_url`` is the PATH only, no host. Frontend-relative.
  - ``verification_url`` is the backend API URL we can GET to confirm
    the underlying resource still exists. Path only.
  - ``strategy`` ∈ {"created_via_post", "existing_from_list",
    "fixture", "other"}.
  - If you genuinely cannot resolve a template (no API path makes
    sense, no list to pick from, no permission), still output an entry
    with ``concrete_url`` empty and ``notes`` explaining what blocked
    you.

Output a SINGLE JSON object as your last message — no markdown
fences, no prose:

  {"resolved": [ { ... }, { ... } ], "notes": "..." }
"""


RESOLVER_USER_TEMPLATE = """\
TEMPLATES TO RESOLVE (one entry per dynamic route — emit one
concrete URL per template):

{templates_block}

BACKEND URL: {backend_url}

{openapi_section}

{auth_section}

Resolve every template above. Use the live API via Bash+curl when
you need to GET a list or POST a new record. Output the single JSON
object described in the system prompt.
"""


def _build_templates_block(routes: List[RouteSpec]) -> str:
    lines = []
    for r in routes:
        params = ", ".join(r.params) if r.params else "(none)"
        src = f" (declared in {r.source_file})" if r.source_file else ""
        lines.append(f"  - {r.path}  [params: {params}]{src}")
    return "\n".join(lines) if lines else "  (none)"


def _build_openapi_section(openapi_path: Optional[Path]) -> str:
    if openapi_path and openapi_path.is_file():
        return (
            f"OPENAPI SPEC available at: {openapi_path}\n"
            f"Read it to find the right endpoints + payload shapes."
        )
    return (
        "OPENAPI SPEC: not provided. Read the backend code "
        "(app/api/routes/, urls.py, controllers, etc.) to derive "
        "endpoints + payload shapes."
    )


def _build_auth_section(auth_contract: Optional[str]) -> str:
    if not auth_contract:
        return "AUTH: assume the API is public; no auth header needed."
    return (
        "AUTH CONTRACT — the backend is authed; obtain a token via the "
        "login flow and pass it as the Authorization header on each "
        "POST/GET you make:\n\n"
        f"{auth_contract}"
    )


# ── Claude CLI implementation ────────────────────────────────────────────


_ALLOWED_TOOLS_RESOLVE = ["Read", "Glob", "Grep", "Bash"]


def claude_resolve_routes(
    workspace_root: Path,
    routes: List[RouteSpec],
    backend_url: str,
    auth_contract: Optional[str] = None,
    openapi_path: Optional[Path] = None,
    command: str = "claude",
    timeout_seconds: float = _DEFAULT_AGENT_TIMEOUT_S,
    on_status: Optional[Callable[[str], None]] = None,
) -> List[ResolvedRoute]:
    """Claude CLI variant. Native Read/Glob/Grep/Bash — the agent
    walks the code AND can curl the live backend. Returns ``[]`` on
    any subprocess failure."""
    if shutil.which(command) is None:
        return []

    def _log(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    user_prompt = RESOLVER_USER_TEMPLATE.format(
        templates_block=_build_templates_block(routes),
        backend_url=backend_url or "(unknown — derive from running stack)",
        openapi_section=_build_openapi_section(openapi_path),
        auth_section=_build_auth_section(auth_contract),
    )
    cmd = [
        command, "--print",
        "--output-format=json",
        "--append-system-prompt", RESOLVER_SYSTEM_PROMPT,
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", " ".join(_ALLOWED_TOOLS_RESOLVE),
        "--add-dir", str(workspace_root),
    ]
    if openapi_path and openapi_path.parent != workspace_root:
        # Mount the OpenAPI dir too so Read can pick it up.
        cmd.extend(["--add-dir", str(openapi_path.parent)])
    try:
        proc = subprocess.run(
            cmd, input=user_prompt,
            capture_output=True, text=True,
            timeout=timeout_seconds,
            cwd=str(workspace_root),
        )
    except subprocess.TimeoutExpired:
        _log(f"claude_resolve_routes: timed out after {timeout_seconds:.0f}s")
        return []
    except FileNotFoundError:
        return []

    if proc.returncode != 0:
        _log(
            f"claude_resolve_routes: claude exited {proc.returncode}: "
            f"{(proc.stderr or '')[:200]}"
        )
        return []

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if payload.get("is_error"):
        return []
    return _parse_resolver_result(payload.get("result") or "")


def _parse_resolver_result(text: str) -> List[ResolvedRoute]:
    """Extract the trailing JSON object and unpack ``resolved``.
    Mirrors the parser shape route_discovery uses."""
    if not text:
        return []
    parsed = _extract_trailing_json(text)
    if not isinstance(parsed, dict):
        return []
    out: List[ResolvedRoute] = []
    for entry in parsed.get("resolved") or []:
        if not isinstance(entry, dict):
            continue
        tmpl = (entry.get("template") or "").strip()
        url = (entry.get("concrete_url") or "").strip()
        if not tmpl or not url:
            # Allow the agent to emit a "couldn't resolve" entry with
            # an empty url + a note — caller filters these out before
            # use, but we still keep them for diagnostics.
            if not tmpl:
                continue
        try:
            out.append(ResolvedRoute(
                template=tmpl,
                concrete_url=url,
                verification_url=(entry.get("verification_url") or "").strip(),
                strategy=(entry.get("strategy") or "other").strip() or "other",
                notes=(entry.get("notes") or "").strip(),
            ))
        except Exception:
            continue
    return out


def _extract_trailing_json(text: str):
    """Direct → fenced → forward-scan-from-first-brace, same pattern
    as route_discovery._parse_agent_result."""
    candidate = text.strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    fenced = re.search(
        r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL,
    )
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
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


# ── Text-client implementation ───────────────────────────────────────────


def text_client_resolve_routes(
    workspace_root: Path,
    routes: List[RouteSpec],
    backend_url: str,
    auth_contract: Optional[str] = None,
    openapi_path: Optional[Path] = None,
    *,
    client=None,
    on_status: Optional[Callable[[str], None]] = None,
    max_view_bytes: int = 6000,
    max_openapi_bytes: int = 20000,
) -> List[ResolvedRoute]:
    """BaseAIClient-driven variant. No native file/shell tools, so we
    pre-render: each route's view source, the OpenAPI spec (truncated),
    and the auth context. The agent then names a strategy + concrete
    URL based on what's in the prompt.

    Caveat vs the Claude variant: this can't actually execute a POST
    against the live backend. For ``created_via_post`` strategies the
    caller would need a second pass to materialize. For now we accept
    that limitation — text-client mode is the architecture baseline,
    not the daily driver. Returns ``[]`` when no client supplied.
    """
    if client is None:
        return []

    rendered_views = _render_view_sources(
        workspace_root, routes, max_bytes=max_view_bytes,
    )
    openapi_text = _read_openapi_text(openapi_path, max_bytes=max_openapi_bytes)
    openapi_section = (
        f"OPENAPI SPEC (truncated to {max_openapi_bytes} bytes):\n\n"
        f"{openapi_text}"
        if openapi_text else
        "OPENAPI SPEC: not available. Infer endpoints from view source."
    )
    user_prompt = RESOLVER_USER_TEMPLATE.format(
        templates_block=_build_templates_block(routes),
        backend_url=backend_url or "(unknown)",
        openapi_section=openapi_section,
        auth_section=_build_auth_section(auth_contract),
    ) + "\n\nFRONTEND VIEW SOURCE (one block per template):\n\n" + rendered_views
    sys_prompt = RESOLVER_SYSTEM_PROMPT + (
        "\n\nIMPORTANT: you do NOT have file or shell tools in this "
        "mode. Use ONLY the view source + OpenAPI included inline "
        "below. If a strategy requires POST'ing, mark "
        "``strategy: 'created_via_post'`` and set ``concrete_url`` to "
        "a placeholder like ``/recipes/${NEW_ID}`` — the harness will "
        "materialize later."
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
                    f"text_client_resolve_routes: client call raised "
                    f"{type(e).__name__}: {e}"
                )
            except Exception:
                pass
        return []
    return _parse_resolver_result(text or "")


def _render_view_sources(
    workspace_root: Path,
    routes: List[RouteSpec],
    max_bytes: int,
) -> str:
    parts: List[str] = []
    seen: set = set()
    for r in routes:
        src = r.source_file
        if not src or src in seen:
            continue
        seen.add(src)
        fp = workspace_root / src
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if len(text) > max_bytes:
            text = text[:max_bytes] + "\n... [truncated]"
        parts.append(f"--- {src}  (used by {r.path}) ---\n{text}")
    return "\n\n".join(parts) if parts else "(no view source files resolvable)"


def _read_openapi_text(
    openapi_path: Optional[Path],
    max_bytes: int,
) -> str:
    if not openapi_path or not openapi_path.is_file():
        return ""
    try:
        text = openapi_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    if len(text) > max_bytes:
        return text[:max_bytes] + "\n... [truncated]"
    return text
