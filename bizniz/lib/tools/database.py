"""Database introspection tool factory for v2 tool-loop agents.

Exposes a single tool — ``query_database`` — that runs SQL via ``psql``
inside a Postgres container in the compose stack. Read-only by
convention (the system prompt should tell the agent so), but not
enforced at the tool layer: enforcement would require parsing SQL,
which is brittle.

Auto-detects the postgres service from ``compose.yaml`` if no
``service`` is passed: scans for an image containing ``postgres`` /
``postgis``. Pass ``service=`` explicitly to override.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional


ToolHandler = Callable[[Dict], str]


_MAX_OUTPUT_BYTES = 10_000


def _truncate(s: str, n: int = _MAX_OUTPUT_BYTES) -> str:
    return s if len(s) <= n else s[:n] + f"\n\n... (truncated, total {len(s)} bytes)"


def _shell_quote(s: str) -> str:
    """Single-quote-escape a string for shell."""
    return "'" + s.replace("'", "'\\''") + "'"


def _guess_db_service(compose_path: str) -> Optional[str]:
    """Best-effort autodetect — first service whose image looks postgres-y."""
    if not compose_path or not Path(compose_path).exists():
        return None
    try:
        import yaml  # type: ignore
        with open(compose_path, "r") as f:
            compose = yaml.safe_load(f) or {}
        services = compose.get("services") or {}
        for name, spec in services.items():
            image = ((spec or {}).get("image") or "").lower()
            if "postgres" in image or "postgis" in image:
                return name
    except Exception:
        return None
    return None


def make_query_database(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Run a SQL statement via ``psql`` in the postgres container.

    Action fields:
      - ``service``: optional postgres container name (auto-detected if
                     omitted)
      - ``sql``:     the SQL to run
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: query_database unavailable (no compose_path)."
        sql = (action.get("sql") or "").strip()
        if not sql:
            return "ERROR: query_database requires a non-empty 'sql'."

        target = (action.get("service") or "").strip() or default_service
        if not target:
            target = _guess_db_service(compose_path)
        if not target:
            return (
                "ERROR: query_database could not auto-detect a postgres "
                "service. Pass service= explicitly."
            )

        # -At = unaligned, tuples-only; -U/-d defaulted from container env.
        psql_cmd = (
            f'psql -At -U "${{POSTGRES_USER:-dev}}" '
            f'-d "${{POSTGRES_DB:-postgres}}" -c {_shell_quote(sql)}'
        )
        cmd = [
            "docker", "compose", "-f", compose_path, "exec", "-T",
            target, "sh", "-c", psql_cmd,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "ERROR: query_database timed out (30s)."
        except Exception as e:
            return f"ERROR: query_database failed: {type(e).__name__}: {e}"
        output = (r.stdout or "") + (r.stderr or "")
        return _truncate(
            f"=== psql {target} ===\n{output}\n(exit code: {r.returncode})"
        )
    return handler


def build_database_handlers(
    compose_path: str,
    default_service: Optional[str] = None,
) -> Dict[str, ToolHandler]:
    return {"query_database": make_query_database(compose_path, default_service)}
