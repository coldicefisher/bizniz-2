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
) -> ToolHandler:
    """Run pytest in the pytest sidecar against the live stack.

    Action fields:
      - ``path``: space-separated test paths relative to the workspace
                  (default: ``tests/``)

    The sidecar joins the compose project's docker network so tests can
    hit ``http://<service>:<port>`` URLs. Uses ``--noconftest --rootdir``
    so pytest doesn't try to collect parent conftest files that would
    require the service's full dependency tree.
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

        project_name = _compose_project_name(compose_path)
        network = f"{project_name}_app-network"
        env_url = base_url or ""

        run_cmd = (
            f"cd /workspace && "
            + (f"API_BASE_URL={shlex.quote(env_url)} " if env_url else "")
            + f"pytest {path_spec} --noconftest --rootdir {shlex.quote(path_spec.split()[0])} "
            f"-v --tb=short --no-header"
        )

        cmd = [
            "docker", "run", "--rm",
            "--network", network,
            "-v", f"{workspace_path}:/workspace",
            "-w", "/workspace",
            PYTEST_SIDECAR_IMAGE,
            "sh", "-c", run_cmd,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or "") + (e.stderr or "")
            return _truncate(
                f"ERROR: pytest sidecar timed out after {timeout_s:.0f}s\n{partial}"
            )
        except FileNotFoundError:
            return "ERROR: docker not available on host."
        except Exception as e:
            return f"ERROR: run_tests failed: {type(e).__name__}: {e}"

        output = (proc.stdout or "") + (proc.stderr or "")
        verdict = "TESTS PASSED" if proc.returncode == 0 else "TESTS FAILED"
        return _truncate(f"{verdict}\n(exit code: {proc.returncode})\n\n{output}")
    return handler


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
) -> Dict[str, ToolHandler]:
    """Standard test-execution toolkit: run_tests + smoke_import.

    The Engineer composes this into its ``tool_handlers()`` dict.
    """
    return {
        "run_tests": make_run_tests(
            compose_path=compose_path,
            workspace_path=workspace_path,
            target_service=target_service,
            base_url=base_url,
            timeout_s=timeout_s,
        ),
        "smoke_import": make_smoke_import(compose_path, target_service),
    }
