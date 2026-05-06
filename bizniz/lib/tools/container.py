"""Live-container tool factories for v2 tool-loop agents.

Wraps ``docker compose exec`` operations as discrete tools so agents
can introspect and act on the running stack:

  - ``run_in_container``         arbitrary `sh -c <cmd>`
  - ``run_python_in_container``  `python -c <snippet>` inside the service
  - ``hit_endpoint``             HTTP request via curl from inside the
                                 docker network
  - ``inspect_env``              env vars filtered by prefix
  - ``tail_logs``                container stdout/stderr

Each factory closes over the compose path + the agent's default
service. Action dicts can override the service per call (so the agent
can target peers like the auth or db service).
"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Callable, Dict, List, Optional


ToolHandler = Callable[[Dict], str]


_MAX_OUTPUT_BYTES = 10_000


def _truncate(s: str, n: int = _MAX_OUTPUT_BYTES) -> str:
    return s if len(s) <= n else s[:n] + f"\n\n... (truncated, total {len(s)} bytes)"


def _resolve_service(action: Dict, default: Optional[str]) -> Optional[str]:
    """Action's ``service`` field overrides the agent's default."""
    s = (action.get("service") or "").strip()
    return s or default


def _exec(
    compose_path: str,
    service: str,
    argv: List[str],
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", compose_path, "exec", "-T", service] + argv
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ── tail_logs ──────────────────────────────────────────────────────────


def make_tail_logs(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Tail the container's stdout/stderr.

    Action fields:
      - ``service``: optional container override (default: agent's service)
      - ``path``:    number of lines as a string (e.g. "200"); empty = 100
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: tail_logs unavailable (no compose_path configured)."
        target = _resolve_service(action, default_service)
        if not target:
            return "ERROR: tail_logs needs a service name."
        try:
            n = int((action.get("path") or "100").strip() or "100")
        except ValueError:
            n = 100
        n = max(1, min(n, 500))
        try:
            r = subprocess.run(
                ["docker", "compose", "-f", compose_path,
                 "logs", "--no-color", "--tail", str(n), target],
                capture_output=True, text=True, timeout=30,
            )
            output = (r.stdout or "") + (r.stderr or "")
            if not output.strip():
                return f"(no logs available for {target})"
            return _truncate(f"=== {target} (last {n} lines) ===\n{output}")
        except subprocess.TimeoutExpired:
            return "ERROR: tail_logs timed out."
        except Exception as e:
            return f"ERROR: tail_logs failed: {type(e).__name__}: {e}"
    return handler


# ── run_in_container ───────────────────────────────────────────────────


def make_run_in_container(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Run a shell command inside a running container.

    Action fields:
      - ``service``: optional container override
      - ``command``: shell command (e.g. "ls /workspace", "pip show fastapi")
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: run_in_container unavailable (no compose_path)."
        command = (action.get("command") or "").strip()
        if not command:
            return "ERROR: run_in_container requires non-empty 'command'."
        target = _resolve_service(action, default_service)
        if not target:
            return "ERROR: run_in_container needs a service name."
        try:
            r = _exec(compose_path, target, ["sh", "-c", command], timeout=60)
            output = (r.stdout or "") + (r.stderr or "")
            return _truncate(f"{output}\n(exit code: {r.returncode})")
        except subprocess.TimeoutExpired:
            return "ERROR: run_in_container timed out (60s)."
        except Exception as e:
            return f"ERROR: run_in_container failed: {type(e).__name__}: {e}"
    return handler


# ── run_python_in_container ────────────────────────────────────────────


def make_run_python_in_container(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Run a Python one-liner inside the container's interpreter.

    Most useful for inspecting live state: ``from app.core.config import
    get_settings; print(get_settings().fusionauth_issuer)``.

    Action fields:
      - ``service``: optional container override
      - ``command``: Python code; runs as ``python -c '<code>'``
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: run_python_in_container unavailable (no compose_path)."
        code = (action.get("command") or "").strip()
        if not code:
            return "ERROR: run_python_in_container requires non-empty 'command'."
        target = _resolve_service(action, default_service)
        if not target:
            return "ERROR: run_python_in_container needs a service name."
        try:
            r = _exec(compose_path, target, ["python", "-c", code], timeout=60)
            output = (r.stdout or "") + (r.stderr or "")
            return _truncate(f"{output}\n(exit code: {r.returncode})")
        except subprocess.TimeoutExpired:
            return "ERROR: run_python_in_container timed out (60s)."
        except Exception as e:
            return f"ERROR: run_python_in_container failed: {type(e).__name__}: {e}"
    return handler


# ── hit_endpoint ───────────────────────────────────────────────────────


def make_hit_endpoint(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Make an HTTP request from inside the docker network via curl.

    Action fields:
      - ``service``:      optional container to issue from
      - ``url``:          full URL (use docker hostnames like
                          http://backend:8000, NOT localhost)
      - ``request_data``: JSON-as-string with optional keys:
                            method (default GET), headers, body
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: hit_endpoint unavailable (no compose_path)."
        url = (action.get("url") or "").strip()
        if not url:
            return "ERROR: hit_endpoint requires a 'url'."
        target = _resolve_service(action, default_service)
        if not target:
            return "ERROR: hit_endpoint needs a service name."

        method = "GET"
        headers: Dict[str, str] = {}
        body = None
        rd = action.get("request_data") or "{}"
        try:
            data = json.loads(rd) if rd.strip() else {}
            if isinstance(data, dict):
                method = (data.get("method") or "GET").upper()
                headers = data.get("headers") or {}
                body = data.get("body")
        except Exception as e:
            return f"ERROR: hit_endpoint could not parse request_data JSON: {e}"

        argv = ["curl", "-sS", "-i", "--max-time", "30", "-X", method]
        for k, v in (headers or {}).items():
            argv.extend(["-H", f"{k}: {v}"])
        if body is not None:
            if isinstance(body, (dict, list)):
                argv.extend(["--data-binary", json.dumps(body)])
                if "Content-Type" not in (headers or {}):
                    argv.extend(["-H", "Content-Type: application/json"])
            else:
                argv.extend(["--data-binary", str(body)])
        argv.append(url)

        try:
            r = _exec(compose_path, target, argv, timeout=45)
            output = (r.stdout or "") + (r.stderr or "")
            return _truncate(f"{output}\n(curl exit code: {r.returncode})")
        except subprocess.TimeoutExpired:
            return "ERROR: hit_endpoint timed out (45s)."
        except FileNotFoundError:
            return (
                "ERROR: curl not available in the target container. "
                "Try a different service or use run_python_in_container "
                "with httpx."
            )
        except Exception as e:
            return f"ERROR: hit_endpoint failed: {type(e).__name__}: {e}"
    return handler


# ── inspect_env ────────────────────────────────────────────────────────


def make_inspect_env(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Dump container env vars, filtered by prefix.

    Action fields:
      - ``service``: optional container override
      - ``path``:    prefix filter (e.g. "FUSIONAUTH"); empty = all vars
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: inspect_env unavailable (no compose_path)."
        target = _resolve_service(action, default_service)
        if not target:
            return "ERROR: inspect_env needs a service name."
        prefix = (action.get("path") or "").strip()
        try:
            r = _exec(compose_path, target, ["printenv"], timeout=15)
            if r.returncode != 0:
                return f"ERROR: printenv failed: {r.stderr or r.stdout}"
            lines = (r.stdout or "").splitlines()
            if prefix:
                lines = [ln for ln in lines if ln.startswith(prefix)]
            lines.sort()
            if not lines:
                hint = f" matching '{prefix}'" if prefix else ""
                return f"(no env vars{hint} in {target})"
            output = "\n".join(lines)
            label = f"=== env in {target}"
            if prefix:
                label += f" (prefix='{prefix}')"
            label += " ==="
            return _truncate(f"{label}\n{output}")
        except subprocess.TimeoutExpired:
            return "ERROR: inspect_env timed out."
        except Exception as e:
            return f"ERROR: inspect_env failed: {type(e).__name__}: {e}"
    return handler


# ── Convenience builder ────────────────────────────────────────────────


def build_container_handlers(
    compose_path: str,
    default_service: Optional[str] = None,
) -> Dict[str, ToolHandler]:
    """Standard container-introspection toolkit. Compose into the
    agent's ``tool_handlers()`` dict."""
    return {
        "tail_logs": make_tail_logs(compose_path, default_service),
        "run_in_container": make_run_in_container(compose_path, default_service),
        "run_python_in_container": make_run_python_in_container(compose_path, default_service),
        "hit_endpoint": make_hit_endpoint(compose_path, default_service),
        "inspect_env": make_inspect_env(compose_path, default_service),
    }
