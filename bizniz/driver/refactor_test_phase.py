"""Post-refactor test phase (item 7A).

Runs after the Refactorer extracts code to ``core/``. Dispatches
the project's full test suite (unit + integration) for each
affected service; on failures, escalates through a chain of
repair-debugger tiers; if no tier converges, reverts the refactor
extraction via ``ProjectGit``.

This is the "immune system" half of roadmap item 7 — the tester
that catches refactor-induced regressions automatically. Pairs
with ``SmokeRecovery``'s multi-tier upgrade (item 7C) and the
``Refactorer`` agent (item 6).

Flow:

  Refactorer applies extraction
  → RefactorTestPhase.run() is invoked
    → run_tests(service) for each affected service
      → if pass: record, continue
      → if fail: escalate(repair_tiers)
        → tier 0: cheap-model debugger one-shot
        → tier 1: mid-model debugger up to N attempts
        → tier 2: expensive-model debugger up to M attempts
      → if all tiers fail: revert via ProjectGit, mark phase failed

Every collaborator (test runner, debugger tier factories, git ops)
is constructor-injected for testability. Production wiring lives
in ``v2_build.py`` and calls into the existing pytest sidecar +
``ProjectGit``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.lib.tier_escalation import (
    AttemptOutcome, EscalationResult, TierSpec, escalate,
)


# ── Output schema ────────────────────────────────────────────────


class ServiceTestResult(BaseModel):
    """Outcome of testing one service after refactor."""
    service_name: str
    initial_test_passed: bool = False
    initial_test_output_tail: str = ""
    repair_succeeded: bool = False
    repair_tier_used: Optional[str] = None
    repair_attempts_used: int = 0
    final_test_passed: bool = False
    repair_output_tail: str = ""
    reverted: bool = False
    revert_to_rev: Optional[str] = None


class RefactorTestPhaseResult(BaseModel):
    """End-of-phase summary."""
    duration_s: float = 0.0
    service_results: List[ServiceTestResult] = Field(default_factory=list)
    overall_passed: bool = False
    services_reverted: int = 0
    skipped_reason: Optional[str] = None

    def services_failed(self) -> List[str]:
        return [
            sr.service_name for sr in self.service_results
            if not sr.final_test_passed
        ]


# ── Phase ────────────────────────────────────────────────────────


class RefactorTestPhase:
    """Drives post-refactor test + repair across one or more services.

    Constructor injection:

    - ``services`` — list of service names to test (typically the
      services the Refactorer touched).
    - ``run_tests`` — callable ``(service_name) -> (passed, output)``.
      Production wraps the pytest sidecar invocation.
    - ``repair_tiers`` — list of ``TierSpec[RepairAgent]``. Each
      tier's ``factory`` returns a fresh agent bound to the service
      workspace; the escalation loop tries each tier in order.
    - ``repair_attempt_fn`` — callable
      ``(agent, service_name, failure_output) -> (succeeded, repair_output)``.
      The function does ONE repair attempt — invoking the agent to
      edit the workspace, then re-running tests once. The escalation
      primitive handles the loop.
    - ``git_ops`` — ``ProjectGit``-like interface for revert on
      total failure.
    - ``pre_phase_rev`` — git rev recorded BEFORE the refactor ran.
      The phase reverts to this rev if no repair tier converges.
    """

    def __init__(
        self,
        services: List[str],
        run_tests: Callable[[str], "tuple[bool, str]"],
        repair_tiers: List[TierSpec],
        repair_attempt_fn: Callable[..., "tuple[bool, str]"],
        git_ops: "RefactorGitOps",
        pre_phase_rev: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._services = list(services)
        self._run_tests = run_tests
        self._repair_tiers = repair_tiers
        self._repair_attempt_fn = repair_attempt_fn
        self._git_ops = git_ops
        self._pre_phase_rev = pre_phase_rev
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def run(self) -> RefactorTestPhaseResult:
        """Run the post-refactor test + repair loop. Never raises.

        Returns ``overall_passed=True`` only when every service's
        final test result is green (after repair if needed)."""
        t0 = time.time()
        if not self._services:
            return RefactorTestPhaseResult(
                duration_s=0.0,
                overall_passed=True,
                skipped_reason="no services to test",
            )

        results: List[ServiceTestResult] = []
        for service in self._services:
            sr = self._run_one_service(service)
            results.append(sr)

        all_green = all(sr.final_test_passed for sr in results)
        services_reverted = sum(1 for sr in results if sr.reverted)

        # Whole-phase revert: if ANY service stayed red after
        # exhausting repair, and we have a pre-phase rev, revert
        # the entire phase rather than ship a half-refactored stack.
        if not all_green and self._pre_phase_rev is not None:
            self._log(
                f"RefactorTestPhase: {len([sr for sr in results if not sr.final_test_passed])} "
                f"service(s) still red after repair — reverting refactor "
                f"to {self._pre_phase_rev[:8] if self._pre_phase_rev else '?'}"
            )
            try:
                self._git_ops.revert_to(self._pre_phase_rev)
                for sr in results:
                    if not sr.final_test_passed:
                        sr.reverted = True
                        sr.revert_to_rev = self._pre_phase_rev
                services_reverted = sum(
                    1 for sr in results if sr.reverted
                )
            except Exception as e:
                self._log(
                    f"RefactorTestPhase: revert raised "
                    f"{type(e).__name__}: {e} — leaving workspace as-is"
                )

        return RefactorTestPhaseResult(
            duration_s=time.time() - t0,
            service_results=results,
            overall_passed=all_green,
            services_reverted=services_reverted,
        )

    def _run_one_service(self, service: str) -> ServiceTestResult:
        """Test one service; on failure, escalate through repair tiers."""
        self._log(f"RefactorTestPhase: testing '{service}'...")
        passed, output = self._run_tests(service)
        sr = ServiceTestResult(
            service_name=service,
            initial_test_passed=passed,
            initial_test_output_tail=output[-1000:],
            final_test_passed=passed,
            repair_output_tail=output[-1000:],
        )
        if passed:
            self._log(f"RefactorTestPhase: '{service}' passed first try")
            return sr

        if not self._repair_tiers:
            self._log(
                f"RefactorTestPhase: '{service}' failed and no repair "
                f"tiers wired — skipping repair"
            )
            return sr

        self._log(
            f"RefactorTestPhase: '{service}' failed — escalating "
            f"through {len(self._repair_tiers)} repair tier(s)"
        )

        def attempt_fn(agent, ti, ai, prior_output):
            # The attempt fn invokes the repair agent + re-runs tests
            # in one bundle. Returns AttemptOutcome with success flag
            # + the resulting test output for the next attempt's
            # context.
            repair_passed, repair_output = self._repair_attempt_fn(
                agent, service, prior_output or output,
            )
            return AttemptOutcome(
                succeeded=repair_passed,
                output=repair_output,
            )

        escalation = escalate(
            self._repair_tiers, attempt_fn,
            on_status=lambda m: self._log(f"  [{service}] {m}"),
        )
        sr.repair_succeeded = escalation.succeeded
        sr.repair_tier_used = escalation.final_tier_label
        sr.repair_attempts_used = escalation.total_attempts
        sr.final_test_passed = escalation.succeeded
        sr.repair_output_tail = escalation.final_output[-1000:]
        if escalation.succeeded:
            self._log(
                f"RefactorTestPhase: '{service}' recovered via "
                f"tier {escalation.final_tier_label!r} after "
                f"{escalation.total_attempts} attempt(s)"
            )
        else:
            self._log(
                f"RefactorTestPhase: '{service}' did NOT recover — "
                f"all tiers exhausted ({escalation.total_attempts} "
                f"total attempt(s))"
            )
        return sr


# ── Git-ops interface ────────────────────────────────────────────


class RefactorGitOps:
    """Minimal git interface the phase needs. ``ProjectGit`` (item 3)
    satisfies this — production wraps it; tests pass a fake."""

    def revert_to(self, rev: str) -> None:
        raise NotImplementedError
