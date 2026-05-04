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
from bizniz.integration.contracts import (
    _wait_for_openapi,
    capture_backend_contracts,
)


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


def _load_auth_contract_for_compose(compose_path: str) -> Optional[str]:
    """Read AUTH_CONTRACT.md from the project root if present.

    Project root is two levels up from infra/development/docker-compose.yml.
    Mirrors `bizniz.integration.debug_loop._load_auth_contract` so testers
    and debugger see the same contract.
    """
    try:
        candidate = Path(compose_path).parent.parent.parent / "AUTH_CONTRACT.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


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


def _frontends(arch: SystemArchitecture) -> List[ServiceDefinition]:
    return [
        s for s in arch.services
        if s.service_type == "frontend" and s.port
    ]


def _run_playwright_in_sidecar(
    service: ServiceDefinition,
    workspace_path: Path,
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    timeout_s: float = 600.0,
) -> tuple[bool, str]:
    """Run Playwright tests against the live frontend container via the
    pre-built Playwright sidecar joined to the compose network.

    Uses ``bizniz-test-playwright:latest`` which has @playwright/test
    pre-installed. No runtime npm install.
    """
    project_name = _compose_project_name(compose_path)
    network = f"{project_name}_app-network"
    base_url = f"http://{service.name}:{service.port}"

    # Write a minimal Playwright config. .cjs forces CommonJS parsing
    # regardless of the workspace's package.json "type" field.
    config_body = (
        'module.exports = { testDir: "tests/integration", '
        'testMatch: ["**/*.spec.cjs", "**/*.spec.js"], '
        'reporter: "list", timeout: 30000, '
        'fullyParallel: false, workers: 1, '
        'forbidOnly: true, '
        'use: { trace: "off", video: "off", screenshot: "off" } };'
    )
    write_config = f"printf '%s' {shlex.quote(config_body)} > playwright.smoke.config.cjs"
    run_cmd = (
        f"cd /workspace && {write_config} && "
        f"FRONTEND_URL={shlex.quote(base_url)} "
        f"npx playwright test --config=playwright.smoke.config.cjs"
    )

    cmd = [
        "docker", "run", "--rm",
        "--network", network,
        "-v", f"{workspace_path}:/workspace",
        "-w", "/workspace",
        "--ipc=host",
        "-e", "NODE_PATH=/usr/lib/node_modules",
        PLAYWRIGHT_SIDECAR_IMAGE,
        "sh", "-c", run_cmd,
    ]

    _log(on_status, f"Integration: running Playwright sidecar for '{service.name}' against {base_url}...")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return False, f"playwright sidecar timed out after {timeout_s:.0f}s\n{e.stdout or ''}{e.stderr or ''}"

    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


# Pre-built sidecar images (built by docker/test-sidecars/build.sh)
PYTEST_SIDECAR_IMAGE = "bizniz-test-pytest:latest"
PLAYWRIGHT_SIDECAR_IMAGE = "bizniz-test-playwright:latest"


def _run_pytest_in_sidecar(
    service: ServiceDefinition,
    workspace_path: Path,
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    timeout_s: float = 180.0,
) -> tuple[bool, str]:
    """Run ``pytest tests/integration/`` inside the pre-built pytest
    sidecar joined to the compose project's network. Returns
    ``(passed, output)``.

    Uses ``bizniz-test-pytest:latest`` which has pytest + httpx
    pre-installed. No runtime pip install.
    """
    project_name = _compose_project_name(compose_path)
    network = f"{project_name}_app-network"

    base_url = f"http://{service.name}:{service.port}"
    # --noconftest: HTTPApiTester writes self-contained tests with
    # their own httpx.Client fixture. Loading the project's
    # tests/conftest.py would require installing the service's full
    # requirements (sqlalchemy, fastapi, etc.) in the sidecar.
    # --rootdir tests/integration: keeps pytest from walking up
    # looking for parent conftest/pyproject.
    run_cmd = (
        f"cd /workspace && "
        f"API_BASE_URL={shlex.quote(base_url)} "
        f"pytest tests/integration/ --noconftest --rootdir tests/integration "
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

    _log(on_status, f"Integration: running pytest sidecar for '{service.name}' against {base_url}...")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return False, f"pytest sidecar timed out after {timeout_s:.0f}s\n{e.stdout or ''}{e.stderr or ''}"

    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def _capture_container_logs(compose_path: str, service_name: str) -> str:
    """Read docker compose logs for one service. Best-effort — return
    whatever's available, even if the container is gone."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", compose_path, "logs", "--no-color", service_name],
            capture_output=True, text=True, timeout=30,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return f"(could not read container logs: {e})"


def _log_tail(text: str, n: int) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-n:])


def _retry_backend_health(
    backend: ServiceDefinition,
    compose_path: str,
    on_status: Optional[Callable[[str], None]],
    backend_wait_s: float,
) -> tuple[bool, str]:
    """After an agentic-debug repair iteration, the workspace has
    new files. Restart the backend container and try /openapi.json
    again. Returns (passed, output_for_next_iteration).

    The "output" returned is fresh docker logs if startup fails, or
    a success marker if it works — that's what the debugger sees on
    the next iteration.
    """
    _log(on_status, f"Integration debug: restarting '{backend.name}' to test repair...")
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_path, "restart", backend.name],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return False, f"docker compose restart failed: {e}"

    doc = _wait_for_openapi(backend.port, deadline_s=backend_wait_s)
    if doc is not None:
        return True, "Backend now responds on /openapi.json"

    logs = _capture_container_logs(compose_path, backend.name)
    return False, logs


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
    debugger_factory: Optional[Callable] = None,
    debug_max_iterations: int = 3,
    web_ui_tester_factory: Optional[Callable] = None,
    keep_stack_up: bool = False,
    debugger_escalation: Optional[List] = None,  # List[DebuggerTierSpec]
) -> List[ServiceResult]:
    """Verify-phase orchestration. See module docstring.

    ``keep_stack_up=True`` skips the final ``docker compose down``
    so the caller can keep the stack running for subsequent work
    (e.g. the architect calling this between engineering layers as
    a pre-flight gate before dispatching the next layer's
    engineers). The caller is then responsible for tearing the
    stack down themselves.
    """
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
        capture_output=True, text=True, timeout=600,
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

    # Load AUTH_CONTRACT.md once. Both the HTTP and WebUI testers need
    # to know about test users + auth endpoints so they can drive real
    # auth flows instead of skipping protected surface area.
    auth_contract = _load_auth_contract_for_compose(compose_path)
    if auth_contract:
        _log(on_status, "Integration: AUTH_CONTRACT.md found — testers will drive real auth flows")

    try:
        for backend in backends:
            ws = service_workspaces.get(backend.name)
            if ws is None:
                _log(on_status, f"Integration: '{backend.name}' has no workspace, skipping")
                continue

            contract = contracts.get(backend.name)
            if contract is None:
                # Backend container is up (or tried to be) but never
                # responded on /openapi.json. Most common cause: the
                # app crashed at startup. Capture docker logs and
                # hand them to the debugger if available, so we can
                # auto-repair startup bugs (lifespan crashes,
                # missing env vars, import errors).
                logs = _capture_container_logs(compose_path, backend.name)
                _log(
                    on_status,
                    f"Integration: '{backend.name}' has no captured contract — "
                    f"backend likely crashed at startup. Tail of logs:\n"
                    f"{_log_tail(logs, 25)}"
                )

                if debugger_factory is not None:
                    workspace_root = Path(ws.root) if hasattr(ws, "root") else None
                    if workspace_root is not None:
                        from bizniz.integration.debug_loop import repair_integration_failure

                        # Re-run = bring backend up again and try to
                        # hit /openapi.json. If it works, we have a
                        # contract and the debugger has fixed startup.
                        def _rerun_startup():
                            return _retry_backend_health(backend, compose_path, on_status, backend_wait_s)

                        _bound_factory = lambda ws_=ws: debugger_factory(ws_)

                        def _capture_startup_logs():
                            return _capture_container_logs(compose_path, backend.name)

                        repaired, final_output = repair_integration_failure(
                            service=backend,
                            workspace=ws,
                            failure_output=logs,
                            integration_test_rel="(no tests yet — backend startup)",
                            debugger_factory=_bound_factory,
                            rerun_tests=_rerun_startup,
                            on_status=on_status,
                            max_iterations=debug_max_iterations,
                            capture_logs=_capture_startup_logs,
                            compose_path=compose_path,
                            problem_statement=problem_statement,
                            escalation=debugger_escalation,
                        )

                        if repaired:
                            _log(on_status, f"Integration: '{backend.name}' backend now reachable after agentic repair — re-capturing contract")
                            doc = _wait_for_openapi(backend.port, deadline_s=backend_wait_s)
                            if doc is not None:
                                contract = doc
                                # fall through to test generation below
                            else:
                                _mark_failed(
                                    out, backend.name,
                                    "integration_failed: backend started after repair but /openapi.json still unreachable",
                                )
                                continue
                        else:
                            _mark_failed(
                                out, backend.name,
                                f"integration_failed: backend startup did not recover after agentic repair. Tail:\n{_log_tail(final_output, 25)[:1500]}",
                            )
                            continue
                    else:
                        _mark_failed(
                            out, backend.name,
                            "integration_failed: backend did not expose /openapi.json (no workspace.root for debug)",
                        )
                        continue
                else:
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
                    auth_contract=auth_contract,
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

                if debugger_factory is not None:
                    from bizniz.integration.debug_loop import repair_integration_failure

                    def _rerun():
                        # Rebuild + restart the backend container so it picks
                        # up code fixes AND any new dependencies written to
                        # requirements.txt. Using --build ensures the image
                        # is fresh, not just the process.
                        _log(on_status, f"Integration debug: rebuilding + restarting '{backend.name}'...")
                        try:
                            subprocess.run(
                                ["docker", "compose", "-f", compose_path,
                                 "up", "-d", "--build", "--force-recreate", backend.name],
                                capture_output=True, text=True, timeout=300,
                            )
                            _wait_http_ok(f"http://localhost:{backend.port}/openapi.json", deadline_s=60)
                        except Exception as e:
                            _log(on_status, f"Integration debug: rebuild failed ({e})")
                        return _run_pytest_in_sidecar(
                            service=backend,
                            workspace_path=workspace_root,
                            compose_path=compose_path,
                            on_status=on_status,
                        )

                    # Bind the per-service workspace into the factory
                    # so repair_integration_failure can call it with no
                    # args. Each backend gets a fresh debugger instance
                    # against its own workspace.
                    _bound_factory = lambda ws_=ws: debugger_factory(ws_)

                    def _capture_backend_logs():
                        return _capture_container_logs(compose_path, backend.name)

                    repaired, final_output = repair_integration_failure(
                        service=backend,
                        workspace=ws,
                        failure_output=output,
                        integration_test_rel=target_rel,
                        debugger_factory=_bound_factory,
                        rerun_tests=_rerun,
                        on_status=on_status,
                        max_iterations=debug_max_iterations,
                        capture_logs=_capture_backend_logs,
                        compose_path=compose_path,
                        problem_statement=problem_statement,
                        escalation=debugger_escalation,
                    )

                    if repaired:
                        _log(on_status, f"Integration: '{backend.name}' PASS after agentic repair")
                        continue

                    final_tail = "\n".join(final_output.splitlines()[-30:])
                    _mark_failed(
                        out, backend.name,
                        f"integration_failed: pytest non-zero exit after agentic repair. Tail:\n{final_tail[:1500]}",
                    )
                else:
                    _mark_failed(
                        out, backend.name,
                        f"integration_failed: pytest non-zero exit. Tail:\n{tail[:1500]}",
                    )

        # ── Frontend phase: WebUITester via Playwright sidecar ────────
        if web_ui_tester_factory is not None:
            for frontend in _frontends(architecture):
                ws = service_workspaces.get(frontend.name)
                if ws is None:
                    _log(on_status, f"Integration: '{frontend.name}' has no workspace, skipping")
                    continue

                # Wait for frontend to actually serve content
                fe_base = f"http://localhost:{frontend.port}"
                if not _wait_http_ok(f"{fe_base}/", deadline_s=backend_wait_s):
                    _log(
                        on_status,
                        f"Integration: '{frontend.name}' didn't respond on /"
                    )
                    _mark_failed(
                        out, frontend.name,
                        "integration_failed: frontend not reachable on /",
                    )
                    continue

                _log(on_status, f"Integration: generating UI tests for '{frontend.name}'...")
                tester = web_ui_tester_factory(workspace=ws)
                try:
                    test_source = tester.generate_test_file(
                        problem_statement=problem_statement,
                        service=frontend,
                        backend_contracts=contracts,
                        auth_contract=auth_contract,
                    )
                except Exception as e:
                    _log(on_status, f"Integration: UI test generation failed for '{frontend.name}': {e}")
                    _mark_failed(
                        out, frontend.name,
                        f"integration_failed: UI test generation error — {type(e).__name__}: {e}",
                    )
                    continue

                target_rel_fe = "tests/integration/ui.spec.cjs"
                target_path_fe = ws.path(target_rel_fe)
                target_path_fe.parent.mkdir(parents=True, exist_ok=True)
                target_path_fe.write_text(test_source)
                _log(on_status, f"Integration: '{frontend.name}' UI tests written → {target_rel_fe}")

                workspace_root_fe = Path(ws.root) if hasattr(ws, "root") else target_path_fe.parent.parent.parent
                fe_passed, fe_output = _run_playwright_in_sidecar(
                    service=frontend,
                    workspace_path=workspace_root_fe,
                    compose_path=compose_path,
                    on_status=on_status,
                )

                if fe_passed:
                    _log(on_status, f"Integration: '{frontend.name}' UI PASS")
                else:
                    fe_tail = "\n".join(fe_output.splitlines()[-30:])
                    _log(on_status, f"Integration: '{frontend.name}' UI FAIL\n{fe_tail}")

                    if debugger_factory is not None:
                        from bizniz.integration.debug_loop import repair_integration_failure

                        def _rerun_fe(svc=frontend, ws_root=workspace_root_fe):
                            # Rebuild + restart frontend so it picks up code
                            # fixes AND any new npm packages.
                            _log(on_status, f"Integration debug: rebuilding + restarting '{svc.name}'...")
                            try:
                                subprocess.run(
                                    ["docker", "compose", "-f", compose_path,
                                     "up", "-d", "--build", "--force-recreate", svc.name],
                                    capture_output=True, text=True, timeout=300,
                                )
                                _wait_http_ok(f"http://localhost:{svc.port}/", deadline_s=60)
                            except Exception as e:
                                _log(on_status, f"Integration debug: rebuild failed ({e})")
                            return _run_playwright_in_sidecar(
                                service=svc,
                                workspace_path=ws_root,
                                compose_path=compose_path,
                                on_status=on_status,
                            )

                        _bound_factory_fe = lambda ws_=ws: debugger_factory(ws_)

                        def _capture_frontend_logs(svc_name=frontend.name):
                            return _capture_container_logs(compose_path, svc_name)

                        repaired_fe, final_fe_output = repair_integration_failure(
                            service=frontend,
                            workspace=ws,
                            failure_output=fe_output,
                            integration_test_rel=target_rel_fe,
                            debugger_factory=_bound_factory_fe,
                            rerun_tests=_rerun_fe,
                            on_status=on_status,
                            max_iterations=debug_max_iterations,
                            capture_logs=_capture_frontend_logs,
                            compose_path=compose_path,
                            problem_statement=problem_statement,
                            escalation=debugger_escalation,
                        )

                        if repaired_fe:
                            _log(on_status, f"Integration: '{frontend.name}' UI PASS after agentic repair")
                            continue

                        final_fe_tail = "\n".join(final_fe_output.splitlines()[-30:])
                        _mark_failed(
                            out, frontend.name,
                            f"integration_failed: playwright non-zero exit after agentic repair. Tail:\n{final_fe_tail[:1500]}",
                        )
                    else:
                        _mark_failed(
                            out, frontend.name,
                            f"integration_failed: playwright non-zero exit. Tail:\n{fe_tail[:1500]}",
                        )
    finally:
        if keep_stack_up:
            _log(on_status, "Integration: leaving stack up (keep_stack_up=True)")
        else:
            _log(on_status, "Integration: tearing down stack...")
            try:
                subprocess.run(
                    ["docker", "compose", "-f", compose_path, "down"],
                    capture_output=True, text=True, timeout=120,
                )
            except Exception as e:
                _log(on_status, f"Integration: teardown error ({e})")

    return out
