"""Integration phase orchestrator (v2).

Runs after Engineer/Reviewer have approved a milestone. Drives:

  run_api  → for each backend in the milestone's architecture:
              capture openapi contract → dispatch HTTPApiTester →
              run pytest sidecar → on fail, dispatch debugger
  run_web  → for each frontend: dispatch WebUITester → run Playwright
              sidecar → on fail, dispatch debugger

Topology: API tests must pass before web tests run. Web tests against a
broken backend are noise. The pipeline calls run_api() first, then
run_web() only if api passed.

This is a thin v2 wrapper over v1 building blocks (HTTPApiTester,
WebUITester, the sidecar pytest/playwright runners, the agentic
debugger). The v1 ``run_integration_phase`` top-level orchestrator
isn't called — its behavior is split across run_api/run_web here so
sub-phase resume can pick up between them.

Stack management is the pipeline's job — IntegrationPhase assumes the
stack is up + healthy before run_api/run_web are called.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import ServiceDefinition, ServiceResult, SystemArchitecture
from bizniz.integration.contracts import capture_backend_contracts
from bizniz.integration.debug_loop import repair_integration_failure
from bizniz.integration.runner import (
    _capture_container_logs,
    _frontends,
    _backends,
    _log_tail,
    _run_pytest_in_sidecar,
    _run_playwright_in_sidecar,
    _wait_http_ok,
)
from bizniz.planner.types import Milestone
from bizniz.workspace.base_workspace import BaseWorkspace


class IntegrationPhaseResult(BaseModel):
    """Per-phase summary returned to the milestone loop."""
    phase: str  # "api" or "web"
    passed: bool
    service_results: List[Dict] = Field(default_factory=list)
    duration_s: float = 0.0
    error_summary: Optional[str] = None
    backend_contracts: Dict[str, Dict] = Field(default_factory=dict)


class IntegrationPhase:
    """Milestone-scoped integration test driver.

    Construction is one-time per pipeline run (factories closed over
    LLM clients, debugger config, etc.). ``run_api`` and ``run_web``
    are the two callable entry points; either may be skipped on resume.
    """

    def __init__(
        self,
        http_tester_factory: Callable[..., object],
        web_tester_factory: Callable[..., object],
        worker_tester_factory: Optional[Callable[..., object]] = None,
        debugger_factory: Optional[Callable[..., object]] = None,
        debugger_max_iterations: int = 3,
        backend_wait_s: float = 60.0,
        problem_statement: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        """Construct.

        ``debugger_factory``, when provided, is called as
        ``debugger_factory(workspace=ws, service=service)`` and must
        return an ``AgenticDebugger`` (or compatible) instance. v1's
        ``repair_integration_failure`` drives the actual loop —
        IntegrationPhase wires the factory + the rerun callback.
        """
        self._http_factory = http_tester_factory
        self._web_factory = web_tester_factory
        self._worker_factory = worker_tester_factory
        self._debugger_factory = debugger_factory
        self._debugger_max_iterations = debugger_max_iterations
        self._backend_wait_s = backend_wait_s
        self._problem_statement = problem_statement
        self._on_status = on_status

    # ── Public ─────────────────────────────────────────────────────────

    def run_api(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        project_root: Path,
        compose_path: str,
        service_workspaces: Dict[str, BaseWorkspace],
        auth_contract: Optional[str] = None,
    ) -> IntegrationPhaseResult:
        """Run API integration tests for every backend in the architecture.

        Returns a result with per-service status. ``passed`` is True only
        if every backend passed.
        """
        t0 = time.time()
        backends = _backends(architecture)
        if not backends:
            self._log("IntegrationPhase: no backends in architecture; skipping API phase")
            return IntegrationPhaseResult(
                phase="api", passed=True, duration_s=time.time() - t0,
            )

        self._log(
            f"IntegrationPhase API: {len(backends)} backend(s), "
            f"capturing contracts..."
        )
        contracts = capture_backend_contracts(
            architecture=architecture,
            project_root=project_root,
            compose_path=compose_path,
            on_status=self._on_status,
            backend_wait_s=self._backend_wait_s,
        )

        results: List[ServiceResult] = []
        all_passed = True

        for backend in backends:
            ws = service_workspaces.get(backend.name)
            if ws is None:
                results.append(_failed_result(
                    backend.name,
                    f"no workspace for backend '{backend.name}'",
                ))
                all_passed = False
                continue

            openapi = contracts.get(backend.name)
            if openapi is None:
                results.append(_failed_result(
                    backend.name,
                    f"failed to capture /openapi.json for '{backend.name}'",
                ))
                all_passed = False
                continue

            self._log(f"IntegrationPhase API: writing tests for '{backend.name}'")
            tester = self._http_factory(workspace=ws)
            try:
                source = tester.generate_test_file(
                    problem_statement=milestone.problem_slice,
                    service=backend,
                    openapi_doc=openapi,
                    target_filepath="tests/integration/test_api.py",
                    auth_contract=auth_contract,
                )
            except Exception as e:
                results.append(_failed_result(
                    backend.name,
                    f"HTTPApiTester raised {type(e).__name__}: {e}",
                ))
                all_passed = False
                continue

            ws.write_file("tests/integration/test_api.py", source)

            passed, output = self._run_pytest_with_repair(
                backend=backend, workspace=ws, compose_path=compose_path,
                project_root=project_root, auth_contract=auth_contract,
                openapi_doc=openapi,
            )
            if passed:
                results.append(_passed_result(backend.name, _log_tail(output, 60)))
            else:
                results.append(_failed_result(
                    backend.name,
                    f"integration tests failed: {_log_tail(output, 30)}",
                ))
                all_passed = False

        return IntegrationPhaseResult(
            phase="api",
            passed=all_passed,
            service_results=[r.model_dump() if hasattr(r, "model_dump") else dict(r.__dict__) for r in results],
            duration_s=time.time() - t0,
            backend_contracts=contracts,
            error_summary=None if all_passed else _summarize_failures(results),
        )

    def run_worker(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        project_root: Path,
        compose_path: str,
        service_workspaces: Dict[str, BaseWorkspace],
        backend_contracts: Dict[str, Dict],
        auth_contract: Optional[str] = None,
    ) -> IntegrationPhaseResult:
        """Run integration tests for every worker service.

        Workers don't have an HTTP surface — tests exercise them
        through their queue/stream/websocket interface. Per-service
        pytest sidecar; debugger dispatched on failure (same
        ``repair_integration_failure`` path as run_api).
        """
        t0 = time.time()
        workers = _list_workers(architecture)
        if not workers:
            self._log("IntegrationPhase: no workers in architecture; skipping Worker phase")
            return IntegrationPhaseResult(
                phase="worker", passed=True, duration_s=time.time() - t0,
            )
        if self._worker_factory is None:
            self._log(
                f"IntegrationPhase: {len(workers)} worker(s) but no "
                f"worker_tester_factory configured — skipping Worker phase"
            )
            return IntegrationPhaseResult(
                phase="worker", passed=True, duration_s=time.time() - t0,
                error_summary="(no worker tester factory configured)",
            )

        results: List[ServiceResult] = []
        all_passed = True
        depends_lookup = {s.name: s for s in architecture.services}

        for worker in workers:
            ws = service_workspaces.get(worker.name)
            if ws is None:
                results.append(_failed_result(
                    worker.name, f"no workspace for worker '{worker.name}'",
                ))
                all_passed = False
                continue

            depends_on_services = {
                dep: depends_lookup[dep] for dep in worker.depends_on
                if dep in depends_lookup
            }

            self._log(f"IntegrationPhase Worker: writing tests for '{worker.name}'")
            tester = self._worker_factory(workspace=ws)
            try:
                source = tester.generate_test_file(
                    problem_statement=milestone.problem_slice,
                    service=worker,
                    backend_contracts=backend_contracts,
                    depends_on_services=depends_on_services,
                    target_filepath="tests/integration/test_worker.py",
                    auth_contract=auth_contract,
                )
            except Exception as e:
                results.append(_failed_result(
                    worker.name,
                    f"WorkerTester raised {type(e).__name__}: {e}",
                ))
                all_passed = False
                continue

            ws.write_file("tests/integration/test_worker.py", source)

            passed, output = self._run_pytest_with_repair(
                backend=worker, workspace=ws, compose_path=compose_path,
                project_root=project_root, auth_contract=auth_contract,
                openapi_doc={},  # workers don't have an OpenAPI doc
            )
            if passed:
                results.append(_passed_result(worker.name, _log_tail(output, 60)))
            else:
                results.append(_failed_result(
                    worker.name,
                    f"worker integration tests failed: {_log_tail(output, 30)}",
                ))
                all_passed = False

        return IntegrationPhaseResult(
            phase="worker",
            passed=all_passed,
            service_results=[
                r.model_dump() if hasattr(r, "model_dump") else dict(r.__dict__)
                for r in results
            ],
            duration_s=time.time() - t0,
            error_summary=None if all_passed else _summarize_failures(results),
        )

    def run_web(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        project_root: Path,
        compose_path: str,
        service_workspaces: Dict[str, BaseWorkspace],
        backend_contracts: Dict[str, Dict],
        auth_contract: Optional[str] = None,
    ) -> IntegrationPhaseResult:
        """Run Web integration tests for every frontend service.

        Assumes ``run_api`` already passed (pipeline gates this).
        ``backend_contracts`` from run_api's result feeds the web tester.
        """
        t0 = time.time()
        frontends = _frontends(architecture)
        if not frontends:
            self._log("IntegrationPhase: no frontends in architecture; skipping Web phase")
            return IntegrationPhaseResult(
                phase="web", passed=True, duration_s=time.time() - t0,
            )

        results: List[ServiceResult] = []
        all_passed = True

        for frontend in frontends:
            ws = service_workspaces.get(frontend.name)
            if ws is None:
                results.append(_failed_result(
                    frontend.name,
                    f"no workspace for frontend '{frontend.name}'",
                ))
                all_passed = False
                continue

            self._log(f"IntegrationPhase Web: writing tests for '{frontend.name}'")
            tester = self._web_factory(workspace=ws)
            try:
                source = tester.generate_test_file(
                    problem_statement=milestone.problem_slice,
                    service=frontend,
                    backend_contracts=backend_contracts,
                    target_filepath="tests/integration/ui.spec.cjs",
                    auth_contract=auth_contract,
                )
            except Exception as e:
                results.append(_failed_result(
                    frontend.name,
                    f"WebUITester raised {type(e).__name__}: {e}",
                ))
                all_passed = False
                continue

            ws.write_file("tests/integration/ui.spec.cjs", source)

            passed, output = self._run_playwright_with_repair(
                frontend=frontend,
                workspace=ws,
                compose_path=compose_path,
                project_root=project_root,
                auth_contract=auth_contract,
            )
            if passed:
                results.append(_passed_result(frontend.name, _log_tail(output, 60)))
            else:
                results.append(_failed_result(
                    frontend.name,
                    f"web integration tests failed: {_log_tail(output, 30)}",
                ))
                all_passed = False

        return IntegrationPhaseResult(
            phase="web",
            passed=all_passed,
            service_results=[r.model_dump() if hasattr(r, "model_dump") else dict(r.__dict__) for r in results],
            duration_s=time.time() - t0,
            error_summary=None if all_passed else _summarize_failures(results),
        )

    # ── Internals ──────────────────────────────────────────────────────

    def _run_pytest_with_repair(
        self,
        backend: ServiceDefinition,
        workspace: BaseWorkspace,
        compose_path: str,
        project_root: Path,
        auth_contract: Optional[str],
        openapi_doc: Dict,
    ) -> tuple[bool, str]:
        """Run pytest in the sidecar; on fail, drive the agentic debug
        loop via ``repair_integration_failure`` (which applies code
        fixes, restarts the container, and re-runs tests with sticky
        repair history).
        """
        workspace_path = (
            Path(workspace.root) if hasattr(workspace, "root") else project_root
        )

        def _rerun() -> tuple[bool, str]:
            """Re-run pytest after debugger applies fixes.

            CRITICAL: rebuild + force-recreate the backend container
            first so code edits take effect. Skipping this caused 3
            wasted iterations and $0.77 in v11 (per CLAUDE.md).

            Then WAIT for ``/health`` before firing pytest — recipe_box
            M2 burned all 3 debugger attempts when every test failed
            with ``httpx.ConnectError: Connection refused`` because
            tests ran ~2s after the container came up, before uvicorn
            accepted connections. The debugger has no fix for
            "connection refused" — it just churns until budget exhausts.
            """
            try:
                subprocess.run(
                    ["docker", "compose", "-f", compose_path, "up", "-d",
                     "--build", "--force-recreate", backend.name],
                    capture_output=True, text=True, timeout=600,
                )
            except Exception as e:
                self._log(
                    f"IntegrationPhase API: container rebuild raised "
                    f"{type(e).__name__}: {e}"
                )
            host_port = _resolve_host_port_via_compose(
                compose_path, backend.name, backend.port,
            )
            ready = _wait_http_ok(
                f"http://localhost:{host_port}/health",
                deadline_s=60,
            )
            if not ready:
                self._log(
                    f"IntegrationPhase API: backend /health not responding "
                    f"60s after rebuild — tests will likely fail with "
                    f"connection refused"
                )
            return _run_pytest_in_sidecar(
                service=backend,
                workspace_path=workspace_path,
                compose_path=compose_path,
                on_status=self._on_status,
            )

        passed, output = _run_pytest_in_sidecar(
            service=backend,
            workspace_path=workspace_path,
            compose_path=compose_path,
            on_status=self._on_status,
        )
        if passed or self._debugger_factory is None:
            return passed, output

        self._log(
            f"IntegrationPhase API: '{backend.name}' tests failed; "
            f"dispatching debugger (max {self._debugger_max_iterations} iterations)"
        )

        # Adapt our (workspace, service) factory to the (workspace,) shape
        # repair_integration_failure expects.
        def _wrapped_factory(ws, _backend=backend):
            return self._debugger_factory(workspace=ws, service=_backend)

        def _capture_logs() -> str:
            return _capture_container_logs(compose_path, backend.name)

        try:
            return repair_integration_failure(
                service=backend,
                workspace=workspace,
                failure_output=output,
                integration_test_rel="tests/integration/test_api.py",
                debugger_factory=_wrapped_factory,
                rerun_tests=_rerun,
                on_status=self._on_status,
                max_iterations=self._debugger_max_iterations,
                capture_logs=_capture_logs,
                compose_path=compose_path,
                problem_statement=self._problem_statement,
            )
        except Exception as e:
            self._log(
                f"IntegrationPhase API: debug loop raised "
                f"{type(e).__name__}: {e}"
            )
            return False, output

    def _run_playwright_with_repair(
        self,
        frontend: ServiceDefinition,
        workspace: BaseWorkspace,
        compose_path: str,
        project_root: Path,
        auth_contract: Optional[str],
    ) -> tuple[bool, str]:
        """Run Playwright in the sidecar; on fail, drive the agentic
        debug loop. Symmetric to ``_run_pytest_with_repair`` but for
        frontend services.
        """
        workspace_path = (
            Path(workspace.root) if hasattr(workspace, "root") else project_root
        )

        def _rerun() -> tuple[bool, str]:
            """Re-run Playwright after debugger applies fixes.

            Rebuild + force-recreate the frontend container first so
            edits to src/ take effect. Vite hot-reload usually picks
            them up, but a rebuild is the deterministic forcing
            function — same reasoning as the backend pytest path.
            """
            try:
                subprocess.run(
                    ["docker", "compose", "-f", compose_path, "up", "-d",
                     "--build", "--force-recreate", frontend.name],
                    capture_output=True, text=True, timeout=600,
                )
            except Exception as e:
                self._log(
                    f"IntegrationPhase Web: container rebuild raised "
                    f"{type(e).__name__}: {e}"
                )
            host_port = _resolve_host_port_via_compose(
                compose_path, frontend.name, frontend.port,
            )
            ready = _wait_http_ok(
                f"http://localhost:{host_port}/",
                deadline_s=60,
            )
            if not ready:
                self._log(
                    f"IntegrationPhase Web: frontend / not responding 60s "
                    f"after rebuild — Playwright will likely fail"
                )
            return _run_playwright_in_sidecar(
                service=frontend,
                workspace_path=workspace_path,
                compose_path=compose_path,
                on_status=self._on_status,
            )

        passed, output = _run_playwright_in_sidecar(
            service=frontend,
            workspace_path=workspace_path,
            compose_path=compose_path,
            on_status=self._on_status,
        )
        if passed or self._debugger_factory is None:
            return passed, output

        self._log(
            f"IntegrationPhase Web: '{frontend.name}' tests failed; "
            f"dispatching debugger (max {self._debugger_max_iterations} iterations)"
        )

        def _wrapped_factory(ws, _frontend=frontend):
            return self._debugger_factory(workspace=ws, service=_frontend)

        def _capture_logs() -> str:
            return _capture_container_logs(compose_path, frontend.name)

        try:
            return repair_integration_failure(
                service=frontend,
                workspace=workspace,
                failure_output=output,
                integration_test_rel="tests/integration/ui.spec.cjs",
                debugger_factory=_wrapped_factory,
                rerun_tests=_rerun,
                on_status=self._on_status,
                max_iterations=self._debugger_max_iterations,
                capture_logs=_capture_logs,
                compose_path=compose_path,
                problem_statement=self._problem_statement,
            )
        except Exception as e:
            self._log(
                f"IntegrationPhase Web: debug loop raised "
                f"{type(e).__name__}: {e}"
            )
            return False, output

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)


# Re-export the resolver so call sites in this module don't depend
# on an import path lower in the stack.
from bizniz.integration.contracts import _resolve_host_port as _resolve_host_port_via_compose  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────


def _list_workers(arch: SystemArchitecture) -> List[ServiceDefinition]:
    """Architecture services whose service_type is ``worker`` or
    ``consumer``. Both names are accepted because skeletons disagree."""
    out: List[ServiceDefinition] = []
    for s in arch.services:
        if (s.service_type or "").lower() in ("worker", "consumer"):
            out.append(s)
    return out


def _failed_result(name: str, error: str) -> ServiceResult:
    return ServiceResult(
        service_name=name,
        workspace_name=name,
        success=False,
        error=f"integration_failed: {error}",
    )


def _passed_result(name: str, output_tail: str) -> ServiceResult:
    return ServiceResult(
        service_name=name,
        workspace_name=name,
        success=True,
        error=None,
    )


def _summarize_failures(results: List[ServiceResult]) -> str:
    failed = [r for r in results if not r.success]
    if not failed:
        return ""
    parts = [f"{r.service_name}: {r.error or '(no detail)'}" for r in failed]
    return " | ".join(parts)
