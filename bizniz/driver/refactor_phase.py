"""RefactorPhase — placeholder for the cross-service refactor pass.

Stage 1 wiring only: the phase is structurally in place so the
milestone loop can mark it complete and resume gates know about it.
The real Refactorer agent (cross-service duplication detection,
extract-to-shared-lib, dedup) is Stage 2.

Until Stage 2 lands, the phase reports ``ran=False`` with a
``skipped_reason="not_implemented"`` so downstream artifacts make the
status visible. Run-state still marks the SubPhase done — without that
the milestone loop would refuse to advance to DONE.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, Optional

from pydantic import BaseModel

from bizniz.architect.types import SystemArchitecture
from bizniz.planner.types import Milestone
from bizniz.workspace.base_workspace import BaseWorkspace


class RefactorPhaseResult(BaseModel):
    passed: bool = True
    ran: bool = False
    skipped_reason: Optional[str] = None
    duration_s: float = 0.0


class RefactorPhase:
    """Driver-side placeholder. ``run()`` is a no-op until the
    Refactorer agent ships in Stage 2."""

    def __init__(
        self,
        refactorer_factory: Optional[Callable] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._refactorer_factory = refactorer_factory
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
        is_final_milestone: bool,
    ) -> RefactorPhaseResult:
        t0 = time.time()
        if self._refactorer_factory is None:
            scope = "final-milestone" if is_final_milestone else "mid-project"
            self._log(
                f"RefactorPhase ({scope}): no refactorer wired — "
                f"skipping (Stage 2 work)"
            )
            return RefactorPhaseResult(
                passed=True, ran=False,
                skipped_reason="not_implemented",
                duration_s=time.time() - t0,
            )
        # Stage 2: invoke the real refactorer here.
        # refactorer = self._refactorer_factory()
        # result = refactorer.run(...)
        return RefactorPhaseResult(
            passed=True, ran=False,
            skipped_reason="not_implemented",
            duration_s=time.time() - t0,
        )
