"""Architect verify-phase entry point.

After all engineering services pass their unit tests:

  1. Bring the full stack up via docker compose
  2. Capture each backend's ``/openapi.json`` (also handed back as
     contracts for downstream layers / future iterations)
  3. For each backend, dispatch HTTPApiTester to write
     ``tests/integration/test_<svc>_api.py`` in the service's workspace
  4. Execute those tests inside a sidecar container joined to the
     compose network — assertions hit the real, running services
  5. Aggregate failures into ``service_results`` (success → False,
     error → ``integration_failed: <details>``)
  6. Tear down the stack regardless of outcome

Framework-blind: the runner doesn't know FastAPI from Express; it
only knows "the service exposes /openapi.json on a port" and "pytest
runs the file we just wrote." Adding a new backend skeleton requires
zero changes here.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, ServiceResult, SystemArchitecture
from bizniz.integration.contracts import capture_backend_contracts


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _wait_http_ok(url: str, deadline_s: float) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def _backends(arch: SystemArchitecture) -> List[ServiceDefinition]:
    return [
        s for s in arch.services
        if s.service_type == "backend" and s.port
    ]


def _compose_project_name(compose_path: str) -> str:
    """Parse ``name:`` out of compose file. Falls back to the parent
    directory's name (compose's default behavior).
    """
    try:
        text = Path(compose_path).read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name:"):
                return stripped.split(":", 1)[1].strip()
    except Exception:
        pass
    return Path(compose_path).resolve().parent.name


def _run_pytest_in_sidecar(
    service: ServiceDefinition,
    workspace_path: Path,
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    timeout_s: float = 180.0,
) -> tuple[bool, str]:
    """Run ``pytest tests/integration/`` inside a sidecar python:3.12
    container joined to the compose project's network. Returns
    ``(passed, output)``.

    Why a sidecar instead of running pytest on the host: we need DNS
    resolution of the compose service names (e.g. ``backend:8000``)
    and we don't want to require pytest+httpx on the host. The
    sidecar gets both via pip install at run time.
    """
    project_name = _compose_project_name(compose_path)
    network = f"{project_name}_app-network"

    base_url = f"http://{service.name}:{service.port}"
    install_cmd = "pip install --quiet pytest httpx"
    # --noconftest: HTTPApiTester writes self-contained tests with
    # their own httpx.Client fixture. Loading the project's
    # tests/conftest.py would require installing the service's full
    # requirements (sqlalchemy, fastapi, etc.) in the sidecar — a
    # ~30-60s tax for fixtures the integration tests don't use.
    # --rootdir tests/integration: keeps pytest from walking up
    # looking for parent conftest/pyproject.
    run_cmd = (
        f"cd /workspace && {install_cmd} && "
        f"API_BASE_URL={shlex.quote(base_url)} "
        f"pytest tests/integration/ --noconftest --rootdir tests/integration "
        f"-v --tb=short --no-header"
    )

    cmd = [
        "docker", "run", "--rm",
        "--network", network,
        "-v", f"{workspace_path}:/workspace",
        "-w", "/workspace",
        "python:3.12-slim",
        "sh", "-c", run_cmd,
    ]

    _log(on_status, f"Integration: running pytest sidecar for '{service.name}' against {base_url}...")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return False, f"pytest sidecar timed out after {timeout_s:.0f}s\n{e.stdout or ''}{e.stderr or ''}"

    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def _mark_failed(
    results: List[ServiceResult],
    name: str,
    error: str,
) -> None:
    for i, r in enumerate(results):
        if r.service_name == name:
            results[i] = ServiceResult(
                service_name=r.service_name,
                workspace_name=r.workspace_name,
                success=False,
                issues_total=r.issues_total,
                issues_passed=r.issues_passed,
                error=error,
            )
            return


def run_integration_phase(
    architecture: SystemArchitecture,
    service_results: List[ServiceResult],
    project_root: Path,
    problem_statement: str,
    compose_path: str,
    http_api_tester_factory: Callable[..., "HTTPApiTester"],
    service_workspaces: Dict[str, "BaseWorkspace"],  # noqa: F821
    on_status: Optional[Callable[[str], None]] = None,
    backend_wait_s: float = 60.0,
) -> List[ServiceResult]:
    """Verify-phase orchestration. See module docstring."""
    backends = _backends(architecture)
    if not backends:
        _log(on_status, "Integration: no HTTP backends to verify, skipping")
        return service_results

    if not _docker_available():
        _log(on_status, "Integration: docker unavailable, skipping")
        return service_results

    out: List[ServiceResult] = list(service_results)

    # Step 1: capture all backend contracts (also brings backends up
    # and stops them — we re-use the running state below by leaving
    # them up for the test run).
    _log(on_status, f"Integration: capturing contracts for {len(backends)} backend(s)...")
    contracts = capture_backend_contracts(
        architecture=architecture,
        project_root=project_root,
        compose_path=compose_path,
        on_status=on_status,
        backend_wait_s=backend_wait_s,
    )

    # Step 2: bring full stack up for the test phase
    _log(on_status, "Integration: bringing up full stack for test execution...")
    up = subprocess.run(
        ["docker", "compose", "-f", compose_path, "up", "-d"],
        capture_output=True, text=True, timeout=240,
    )
    if up.returncode != 0:
        _log(
            on_status,
            f"Integration: compose up failed (rc={up.returncode}); "
            f"skipping integration phase. stderr: {up.stderr.strip()[:300]}"
        )
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "down"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            pass
        return service_results

    try:
        for backend in backends:
            ws = service_workspaces.get(backend.name)
            if ws is None:
                _log(on_status, f"Integration: '{backend.name}' has no workspace, skipping")
                continue

            contract = contracts.get(backend.name)
            if contract is None:
                _log(
                    on_status,
                    f"Integration: '{backend.name}' has no captured contract — "
                    f"marking integration_failed (couldn't reach /openapi.json)"
                )
                _mark_failed(
                    out, backend.name,
                    "integration_failed: backend did not expose /openapi.json",
                )
                continue

            # Wait for the backend's /health (or /) to confirm it's up
            # post-stack-bringup (the contracts capture stopped them).
            base = f"http://localhost:{backend.port}"
            if not _wait_http_ok(f"{base}/openapi.json", deadline_s=backend_wait_s):
                _log(
                    on_status,
                    f"Integration: '{backend.name}' didn't return on full-stack bring-up"
                )
                _mark_failed(
                    out, backend.name,
                    "integration_failed: backend not reachable on full-stack bringup",
                )
                continue

            # Step 3: dispatch HTTPApiTester for this backend
            _log(
                on_status,
                f"Integration: generating tests for '{backend.name}' "
                f"({len(contract.get('paths', {}))} paths)..."
            )
            tester = http_api_tester_factory(workspace=ws)
            try:
                test_source = tester.generate_test_file(
                    problem_statement=problem_statement,
                    service=backend,
                    openapi_doc=contract,
                )
            except Exception as e:
                _log(on_status, f"Integration: test generation failed for '{backend.name}': {e}")
                _mark_failed(
                    out, backend.name,
                    f"integration_failed: test generation error — {type(e).__name__}: {e}",
                )
                continue

            target_rel = "tests/integration/test_api.py"
            target_path = ws.path(target_rel)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(test_source)
            (target_path.parent / "__init__.py").touch()
            _log(on_status, f"Integration: '{backend.name}' tests written → {target_rel}")

            # Step 4: execute via sidecar
            workspace_root = Path(ws.root) if hasattr(ws, "root") else target_path.parent.parent.parent
            passed, output = _run_pytest_in_sidecar(
                service=backend,
                workspace_path=workspace_root,
                compose_path=compose_path,
                on_status=on_status,
            )

            if passed:
                _log(on_status, f"Integration: '{backend.name}' PASS")
            else:
                tail = "\n".join(output.splitlines()[-30:])
                _log(on_status, f"Integration: '{backend.name}' FAIL\n{tail}")
                _mark_failed(
                    out, backend.name,
                    f"integration_failed: pytest non-zero exit. Tail:\n{tail[:1500]}",
                )
    finally:
        _log(on_status, "Integration: tearing down stack...")
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "down"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            _log(on_status, f"Integration: teardown error ({e})")

    return out
