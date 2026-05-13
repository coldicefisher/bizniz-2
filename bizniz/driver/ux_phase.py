"""UXPhase — runs UXDesigner against every frontend service in the
architecture after INTEGRATION_WEB passes.

Mirrors the shape of SmokePhase + IntegrationPhase: a thin driver-side
adapter that owns sequencing + result aggregation; the heavy lifting
(screenshots, vision eval, fix dispatch) lives in
``bizniz.ux_designer.UXDesigner``.

Skip semantics:
    - Architecture has no frontend → phase passes with zero work.
    - UXDesigner factory wasn't wired (e.g. running on a CI image
      without playwright/gemini-vision) → phase passes with a
      ``skipped`` marker so resume sees it as done.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.planner.types import Milestone
from bizniz.workspace.base_workspace import BaseWorkspace


class UXServiceResult(BaseModel):
    service: str
    initial_score: Optional[int] = None
    final_score: Optional[int] = None
    iterations: int = 0
    fixes_applied: int = 0
    screenshots_taken: int = 0
    skipped_reason: Optional[str] = None


class UXPhaseResult(BaseModel):
    passed: bool = True
    services: List[UXServiceResult] = Field(default_factory=list)
    duration_s: float = 0.0
    note: Optional[str] = None


class UXPhase:
    """Drive UXDesigner across every frontend service in the milestone."""

    def __init__(
        self,
        ux_factory: Optional[Callable] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        # ``ux_factory(service)`` returns a UXDesigner bound to that
        # frontend service (its coder_factory closes over the service's
        # workspace + compose path). Called per-frontend so multi-FE
        # architectures get correctly-scoped Coders.
        self._ux_factory = ux_factory
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def run(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        project_root: Path,
        service_workspaces: Dict[str, BaseWorkspace],
        compose_path: str,
        auth_contract: Optional[str] = None,
    ) -> UXPhaseResult:
        t0 = time.time()
        frontends: List[ServiceDefinition] = [
            s for s in architecture.services
            if (s.service_type or "").lower() == "frontend"
        ]
        if not frontends:
            self._log("UXPhase: no frontend services — skipping")
            return UXPhaseResult(
                passed=True, duration_s=time.time() - t0,
                note="no frontend services",
            )
        if self._ux_factory is None:
            self._log("UXPhase: no ux_factory wired — skipping")
            return UXPhaseResult(
                passed=True, duration_s=time.time() - t0,
                note="ux_factory not wired",
            )

        results: List[UXServiceResult] = []
        for frontend in frontends:
            designer = self._ux_factory(frontend)
            ws = service_workspaces.get(frontend.name)
            if ws is None:
                self._log(
                    f"UXPhase: no workspace for '{frontend.name}', skipping"
                )
                results.append(UXServiceResult(
                    service=frontend.name,
                    skipped_reason="no workspace",
                ))
                continue
            self._log(
                f"UXPhase: reviewing '{frontend.name}' for milestone "
                f"'{milestone.name}'..."
            )
            try:
                review = designer.review_frontend(
                    service=frontend,
                    workspace=ws,
                    compose_path=compose_path,
                    problem_statement=milestone.problem_slice,
                    milestone_scope=milestone.name,
                    auth_contract=auth_contract,
                )
            except Exception as e:
                self._log(
                    f"UXPhase: '{frontend.name}' raised "
                    f"{type(e).__name__}: {e}"
                )
                results.append(UXServiceResult(
                    service=frontend.name,
                    skipped_reason=f"{type(e).__name__}: {e}",
                ))
                continue
            results.append(UXServiceResult(
                service=frontend.name,
                initial_score=review.get("initial_score"),
                final_score=review.get("final_score"),
                iterations=review.get("iterations", 0),
                fixes_applied=review.get("fixes_applied", 0),
                screenshots_taken=review.get("screenshots_taken", 0),
            ))

        return UXPhaseResult(
            passed=True,
            services=results,
            duration_s=time.time() - t0,
        )
