"""Test-execution tool factories for v2 tool-loop agents.

Two complementary tools:

  - ``run_tests``     run pytest in the pre-built ``bizniz-test-pytest``
                      sidecar against the live compose stack
  - ``smoke_import``  ``python -c 'import <module>'`` inside a service
                      container — a token-cheap way to ask "does this
                      module load at all?" before paying for a full
                      pytest run

The Engineer uses both: ``smoke_import`` for fast feedback while
iterating on a single module, ``run_tests`` once a slice of work is
ready to verify end-to-end.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional


ToolHandler = Callable[[Dict], str]


_MAX_OUTPUT_BYTES = 12_000
PYTEST_SIDECAR_IMAGE = "bizniz-test-pytest:latest"


def _truncate(s: str, n: int = _MAX_OUTPUT_BYTES) -> str:
    return s if len(s) <= n else s[:n] + f"\n\n... (truncated, total {len(s)} bytes)"


def _resolve_service(action: Dict, default: Optional[str]) -> Optional[str]:
    s = (action.get("service") or "").strip()
    return s or default


def _compose_project_name(compose_path: str) -> str:
    """Compose's default project name is the parent dir of the compose
    file (lowercased, special chars stripped). Mirrors integration/runner.
    """
    return Path(compose_path).parent.name.lower().replace("_", "")


# ── run_tests ──────────────────────────────────────────────────────────


def make_run_tests(
    compose_path: str,
    workspace_path: Path,
    target_service: str,
    base_url: Optional[str] = None,
    timeout_s: float = 180.0,
    auxiliary_log_services: Optional[list] = None,
) -> ToolHandler:
    """Run pytest INSIDE the target service's running container.

    Action fields:
      - ``path``: space-separated test paths relative to the workspace
                  (default: ``tests/``)

    Why exec-into-service vs sidecar: the service container has the
    full dep set the Coder's tests need (sqlalchemy, fastapi, the
    actual app code, etc.). The pytest sidecar only has pytest +
    httpx, so any test that does ``from app.main import app`` or
    relies on sqlalchemy fails at import-time with
    ``ModuleNotFoundError`` — even when the code is correct.

    This was the v33 wall: substantial code written, ALL tests failed
    at import-time because of the env mismatch. Coder couldn't
    diagnose because it inspected the backend container (where deps
    are present) not the sidecar (where tests actually ran).

    Trade-off: requires the service container to be running. compose-
    up at the top of the pipeline guarantees this; if the container
    isn't running, the error is clear (``docker compose exec`` fails
    fast with a useful message).

    On TEST FAILURE, this handler ALSO auto-appends container log
    tails for ``target_service`` and any ``auxiliary_log_services``
    (typically ``auth`` for FusionAuth, ``db`` for postgres). This is
    the deterministic-context fix for v33 round 5: cheap-tier models
    refuse to call ``tail_logs`` even when the system prompt commands
    it (round-5 telemetry: 0 tail_logs across 27 iterations of a stuck
    issue). Forcing the log context into the test output makes the
    "why" of a failure impossible to ignore.
    """
    def handler(action: Dict) -> str:
        if not compose_path or not target_service:
            return "ERROR: run_tests unavailable (missing compose_path/service)."

        path_spec = (action.get("path") or "tests/").strip() or "tests/"
        # Light normalization — refuse absolute paths that escape the
        # workspace mount.
        for p in path_spec.split():
            if p.startswith("/") or ".." in p.split("/"):
                return f"ERROR: test path must be relative to workspace: {p!r}"

        env_url = base_url or ""
        run_cmd = (
            (f"API_BASE_URL={shlex.quote(env_url)} " if env_url else "")
            + f"pytest {path_spec} -v --tb=short --no-header"
        )

        cmd = [
            "docker", "compose", "-f", compose_path,
            "exec", "-T", target_service,
            "sh", "-c", run_cmd,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or "") + (e.stderr or "")
            return _truncate(
                f"ERROR: pytest in {target_service} timed out after "
                f"{timeout_s:.0f}s\n{partial}"
            )
        except FileNotFoundError:
            return "ERROR: docker not available on host."
        except Exception as e:
            return f"ERROR: run_tests failed: {type(e).__name__}: {e}"

        output = (proc.stdout or "") + (proc.stderr or "")
        passed = proc.returncode == 0
        verdict = "TESTS PASSED" if passed else "TESTS FAILED"
        body = (
            f"{verdict}\n(exit code: {proc.returncode}, "
            f"ran inside service '{target_service}')\n\n{output}"
        )
        if not passed:
            body += _gather_failure_context(
                compose_path=compose_path,
                target_service=target_service,
                aux_services=auxiliary_log_services or [],
            )
        return _truncate(body)
    return handler


# ── Failure-context gathering ──────────────────────────────────────────


def _gather_failure_context(
    *,
    compose_path: str,
    target_service: str,
    aux_services: list,
    target_lines: int = 30,
    aux_lines: int = 15,
    max_chars: int = 6000,
) -> str:
    """Collect container logs for the target + auxiliary services
    after a failed test run. Returns markdown-ish text appended to the
    pytest output.

    Why: cheap-tier models won't call ``tail_logs`` on their own (v33
    round-5 telemetry: 0 of 21 failed runs prompted a ``tail_logs``
    call, even with the rule explicit in the system prompt). Auto-
    appending logs makes the "why" of a 4xx/5xx impossible to miss —
    the Coder doesn't have to remember to ask.

    The auxiliary services are the upstream dependencies whose error
    responses the target service propagates: FusionAuth (``auth``)
    for auth failures, postgres (``db``) for DB failures. A 400 from
    an FA registration call shows up in the auth container's access
    log even when the backend's own log only says "FA returned 400".

    Best-effort: if any docker call fails or returns nothing, the
    section is silently skipped — never break the run_tests result.
    """
    parts: list = []

    # Target service logs — the failing service's own traceback +
    # access log goes here.
    target_logs = _tail_compose_logs(compose_path, target_service, target_lines)
    if target_logs:
        parts.append(
            f"\n\n=== Container logs: {target_service} "
            f"(last {target_lines} lines, auto-attached on failure) ===\n"
            f"{target_logs}"
        )

    # Auxiliary services — auth, db, etc. Their error responses are
    # often the actual reason the target failed.
    for svc in aux_services:
        if not svc or svc == target_service:
            continue
        svc_logs = _tail_compose_logs(compose_path, svc, aux_lines)
        if svc_logs:
            parts.append(
                f"\n\n=== Container logs: {svc} "
                f"(upstream — last {aux_lines} lines) ===\n"
                f"{svc_logs}"
            )

    if not parts:
        return ""
    parts.insert(0, "\n\n--- AUTO-APPENDED FAILURE CONTEXT ---")
    parts.append(
        "\n\n--- end auto-context ---\n"
        "Read the logs above before editing. The actual error "
        "(traceback, upstream 4xx/5xx body, missing env var) is "
        "almost always there. If the response body of an upstream "
        "call is still unclear, use ``hit_endpoint`` to repro the "
        "request and read the response directly."
    )
    full = "".join(parts)
    # Cap total bytes to keep history compact across many iterations.
    # v33 round 7 lesson: 80+40+40 lines blew context past flash-lite's
    # tolerance and the model started returning empty actions after
    # iter 18. Tight per-call cap keeps the signal without bloat.
    if len(full) > max_chars:
        full = full[:max_chars] + (
            f"\n... (auto-context truncated to {max_chars} chars; "
            f"use ``tail_logs`` for more)"
        )
    return full


def _tail_compose_logs(
    compose_path: str, service: str, lines: int
) -> Optional[str]:
    """``docker compose logs --tail N --no-color <svc>``. Returns the
    captured output or None on any failure."""
    cmd = [
        "docker", "compose", "-f", compose_path,
        "logs", "--tail", str(lines), "--no-color", service,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip()
    return out or None


# ── smoke_import ───────────────────────────────────────────────────────


def make_smoke_import(
    compose_path: str,
    default_service: Optional[str] = None,
) -> ToolHandler:
    """Verify a module imports cleanly inside a running service.

    Cheaper than ``run_tests`` — just spawns a Python interpreter in the
    container and tries the import. Catches the bulk of "I wrote bad
    code" errors (missing imports, syntax errors, circular imports,
    missing dependencies) before paying for a pytest run.

    Action fields:
      - ``service``: optional container override
      - ``path``:    a Python module dotted path (e.g. "app.api.routes.users")
                     OR a file path (e.g. "app/api/routes/users.py")
    """
    def handler(action: Dict) -> str:
        if not compose_path:
            return "ERROR: smoke_import unavailable (no compose_path)."
        target = _resolve_service(action, default_service)
        if not target:
            return "ERROR: smoke_import needs a service name."
        spec = (action.get("path") or "").strip()
        if not spec:
            return "ERROR: smoke_import requires a module dotted path or .py file path."

        if spec.endswith(".py") or "/" in spec:
            module = spec.removesuffix(".py").replace("/", ".").lstrip(".")
        else:
            module = spec

        code = (
            f"import importlib, sys\n"
            f"try:\n"
            f"    m = importlib.import_module({module!r})\n"
            f"    print('OK', m.__name__, getattr(m, '__file__', '?'))\n"
            f"except Exception as e:\n"
            f"    import traceback\n"
            f"    traceback.print_exc()\n"
            f"    sys.exit(1)\n"
        )
        cmd = [
            "docker", "compose", "-f", compose_path, "exec", "-T",
            target, "python", "-c", code,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: smoke_import timed out (30s)."
        except Exception as e:
            return f"ERROR: smoke_import failed: {type(e).__name__}: {e}"
        out = (proc.stdout or "") + (proc.stderr or "")
        verdict = "IMPORT OK" if proc.returncode == 0 else "IMPORT FAILED"
        return _truncate(f"{verdict}\n(exit code: {proc.returncode})\n\n{out}")
    return handler


# ── Convenience builder ────────────────────────────────────────────────


def build_test_handlers(
    compose_path: str,
    workspace_path: Path,
    target_service: str,
    base_url: Optional[str] = None,
    timeout_s: float = 180.0,
    auxiliary_log_services: Optional[list] = None,
) -> Dict[str, ToolHandler]:
    """Standard test-execution toolkit: run_tests + smoke_import.

    The Engineer composes this into its ``tool_handlers()`` dict.

    ``auxiliary_log_services``: services whose container logs should
    auto-append on test failure (auth, db, etc). Call sites that
    don't pass this fall back to a sensible default — see
    ``_default_aux_services``.
    """
    aux = (
        list(auxiliary_log_services)
        if auxiliary_log_services is not None
        else _default_aux_services(target_service)
    )
    return {
        "run_tests": make_run_tests(
            compose_path=compose_path,
            workspace_path=workspace_path,
            target_service=target_service,
            base_url=base_url,
            timeout_s=timeout_s,
            auxiliary_log_services=aux,
        ),
        "smoke_import": make_smoke_import(compose_path, target_service),
    }


def _default_aux_services(target_service: str) -> list:
    """Default upstream services to tail on failure. Conservative —
    only well-known names. Caller can override by passing
    ``auxiliary_log_services`` explicitly.
    """
    candidates = ["auth", "db", "postgres", "redis"]
    return [s for s in candidates if s != target_service]
