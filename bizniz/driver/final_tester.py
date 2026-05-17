"""FinalTester — end-of-milestone e2e canary.

The last gate before a milestone is marked DONE. Verifies the stack
is shippable end-to-end by hitting real HTTP endpoints as a user
would. No fixtures, no test data manipulation — just confirms the
running services respond on their happy paths.

Catches the class of bug where integration tests "passed" but
broke the stack on teardown (the 2026-05-16 crm_v1 M5 incident
where ``Base.metadata.drop_all`` per-test fixtures left the
production Postgres empty after the suite ran).

Why a separate phase from smoke:
- ``SmokePhase`` runs BEFORE integration phases (catches "shipped
  broken code on first boot")
- Post-integration smoke (commit ``50cf1cd``) catches "integration
  teardown broke the stack"
- ``FinalTester`` runs AFTER everything (UX_REVIEW + REFACTOR) and
  immediately before DONE — catches damage from any subsequent
  phase: refactor extracts that break imports, UX fixes that
  mis-wire a route, the milestone shipping completely.

What it checks (per service type):
- backend: ``/health`` 200, ``/openapi.json`` reachable, every
  authenticated GET route returns non-5xx
- auth: public-flow login works for every seed user
- frontend: ``/`` returns 200 with HTML
- worker: (TODO if/when we have observable worker checks)

Implementation reuses ``SmokePhase``'s probes — same auth contract
parsing, same route enumeration, same 5xx detection logic. The
``FinalTester`` is conceptually a renaming + late-placement of
smoke, but it's a SEPARATE STATE PHASE so the harness records
its result independently and resume can pick up at exactly that
point if it fails.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import SystemArchitecture
from bizniz.driver.smoke_phase import SmokeCheck, SmokePhase
from bizniz.planner.types import Milestone


class FinalTestResult(BaseModel):
    """Outcome of the FINAL_TEST phase."""
    passed: bool = False
    checks: List[SmokeCheck] = Field(default_factory=list)
    critical_failures: List[str] = Field(default_factory=list)
    duration_s: float = 0.0


class FinalTester:
    """End-of-milestone stack verification.

    Constructor takes a ``SmokePhase`` instance — we delegate to its
    probe machinery rather than duplicate auth-contract parsing and
    OpenAPI walking. The placement (after REFACTOR) and the result
    model are what's new.
    """

    def __init__(
        self,
        smoke_phase: SmokePhase,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._smoke = smoke_phase
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def run(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        project_root: Path,
        auth_contract: Optional[str] = None,
    ) -> FinalTestResult:
        """Run the end-of-milestone e2e canary."""
        self._log(
            f"FinalTester: starting for "
            f"M{milestone.sequence_index + 1} '{milestone.name}'"
        )
        smoke_result = self._smoke.run(
            milestone=milestone,
            architecture=architecture,
            project_root=project_root,
            auth_contract=auth_contract,
        )
        result = FinalTestResult(
            passed=smoke_result.passed,
            checks=list(smoke_result.checks),
            critical_failures=list(smoke_result.critical_failures),
            duration_s=smoke_result.duration_s,
        )
        if result.passed:
            self._log(
                f"FinalTester: stack verified shippable "
                f"({len(result.checks)} check(s) passed, "
                f"{result.duration_s:.1f}s)"
            )
        else:
            self._log(
                f"FinalTester: stack NOT shippable — "
                f"{len(result.critical_failures)} critical failure(s)"
            )
            for f in result.critical_failures[:5]:
                self._log(f"  - {f}")
        return result
