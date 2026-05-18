"""SmokeRecovery — single-session Claude CLI agent for smoke failures.

Refactored 2026-05-17 (D14) to inherit from
``bizniz.lib.agentic_phase_recovery.AgenticPhaseRecovery``. The
plumbing (subprocess invocation, JSON parsing, timeout, action
extraction) lives in the base class; this file owns only the
smoke-focused system prompt + user-message format + the
``MultiTierSmokeRecovery`` escalation wrapper.

When ``SmokePhase`` finds a critical failure (route 5xx, /health
down, /api/login broken), the milestone loop dispatches this agent.
It gets one Claude CLI session with file + bash tools to restart
stale containers, run missing migrations, fix env-var drift, or
make surgical application-code edits. On return, the harness
re-runs ``SmokePhase`` — the external re-check is the source of
truth, not the agent's self-report.

The iterative loop + ProgressTracker live in
``MilestoneLoop._maybe_recover_smoke`` (D3 shipped 2026-05-17).
This module just owns the per-dispatch payload.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from bizniz.lib.agentic_phase_recovery import (
    AgenticPhaseRecovery,
    DEFAULT_TIMEOUT_S as _DEFAULT_TIMEOUT_S,
    PhaseRecoveryResult,
)


# Public alias — preserves the old name for any existing imports.
# Same shape; identical fields. PhaseRecoveryResult is what callers
# get back from ``recover()``.
SmokeRecoveryResult = PhaseRecoveryResult


class SmokeRecovery(AgenticPhaseRecovery):
    """Single-shot Claude CLI agent that tries to fix smoke failures.

    Inherits all CLI plumbing from ``AgenticPhaseRecovery``. The
    smoke-focused system prompt is the class-level ``system_prompt``;
    this class only overrides ``build_user_prompt`` to format the
    failure list + stack context that the model needs.
    """

    label = "SmokeRecovery"

    def __init__(
        self,
        compose_path: str,
        project_root: Path,
        command: str = "claude",
        timeout_seconds: float = _DEFAULT_TIMEOUT_S,
        on_status: Optional[Callable[[str], None]] = None,
        fallback_model: Optional[str] = None,
    ) -> None:
        super().__init__(
            project_root=project_root,
            command=command,
            timeout_seconds=timeout_seconds,
            on_status=on_status,
            fallback_model=fallback_model,
        )
        self._compose_path = compose_path

    # Class attribute — focused prompt for smoke recovery.
    system_prompt = """\
You are a smoke-test recovery agent for the bizniz build pipeline.

The smoke phase makes simple HTTP probes against a running docker
compose stack (health endpoints, login flow, route GETs). It just
caught critical failures. You are running inside an iterative
recovery loop — the harness will keep dispatching you (re-running
smoke between attempts) as long as you keep making the failure
count go DOWN. So bias toward fixing things, not toward "surfacing
for human." A genuinely-unfixable bug stops itself when your fixes
stop landing; you don't need to self-limit.

Common patterns you can fix:

  1. **Stale running process** — backend container restarted long
     ago, source files have changed since, new SQLAlchemy models
     never registered → tables missing.
     Recovery: ``docker compose -f <compose_path> restart <service>``
     and wait for the service to become healthy.
  2. **Missing migration / schema drift** — table referenced in code
     doesn't exist in the database.
     Recovery: ``docker exec <db_container> psql ... -c "CREATE
     TABLE ..."``, or trigger the app's migration via
     ``docker compose exec <backend> python -m alembic upgrade head``
     if Alembic is configured.
  3. **Configuration drift** — env var typo, .env file missing, etc.
     Recovery: edit the file, restart the affected container.
  4. **Frontend dev server cached a broken bundle** —
     Recovery: ``docker compose restart <frontend>``.
  5. **Application-code defect surfaced by smoke** — a route 500s
     because the code references an attribute/column/import that
     doesn't exist, or a SQL query joins on a column the model
     doesn't define. The Coder phase shipped broken code; unit
     tests passed because they mocked the broken dependency.
     Recovery: read the route handler, find the defect, surgical
     ``Edit`` to fix it, then ``docker compose exec <svc>`` no-op
     to verify uvicorn ``--reload`` picked it up (or
     ``docker compose restart <svc>`` if reload isn't on). Prefer
     the smallest surgical change. Don't refactor; don't reshape
     architecture; fix the one defect and exit.

What NOT to do:
  - Don't run destructive migrations (``DROP TABLE``, ``DELETE``
    without WHERE). If the data is wrong, halt for human review.
  - Don't push the failure under the rug by relaxing the smoke
    check itself. The check is the contract.
  - Don't refactor or restructure. Application-code fixes are in
    scope (rule 5), but only for the minimal change that resolves
    the smoke failure. The Coder/Refactorer phases own broader
    code shape.

Your toolset: Bash (for ``docker compose``, ``docker exec``, ``curl``,
``psql``), Read, Edit, Write, Glob, Grep. You have permissive
permissions — no human will be asked to approve individual tool
calls. Use the discovery tools (Read/Glob/Grep) explicitly to
locate the failing code — don't assume the workspace is already
loaded.

Workflow:
  1. ``docker logs --tail 50 <service>`` for each failing service to
     see what's actually breaking.
  2. ``docker exec <db_container> psql -U $USER -d <db> -c "\\dt"``
     to inspect schema state, if a relation-not-found error
     surfaces.
  3. Pick the smallest reversible action that addresses the root
     cause. Restart > recreate > rebuild > edit-and-restart.
  4. Re-probe the failing route with ``curl`` to verify the fix
     actually worked.
  5. End your final assistant message with ONE of:
       - ``ACTION: <one-line summary of what you did>``  (repeat
         per action)
       - ``RECOVERY SUCCESS: <one-paragraph summary>``  (when the
         re-probe returned 2xx)
       - ``RECOVERY FAILED: <one-paragraph reason>``  (when you
         can't fix it without human input)

The harness will independently re-run the smoke phase after your
return — RECOVERY SUCCESS is your claim, but the re-check is the
truth. So don't lie; if your fix didn't take, say RECOVERY FAILED
so the human can see the right state.
"""

    def build_user_prompt(
        self,
        *,
        critical_failures: List[str],
        service_names: List[str],
        milestone_title: str,
    ) -> str:
        failure_block = "\n".join(f"  - {f}" for f in critical_failures)
        services_block = ", ".join(service_names) if service_names else "(unknown)"
        return _USER_TEMPLATE.format(
            milestone_title=milestone_title,
            failure_block=failure_block,
            services_block=services_block,
            compose_path=self._compose_path,
            project_root=str(self._project_root),
        )

    # Back-compat: old code called ``recover(critical_failures=..., ...)``.
    # The base class's ``recover(**context)`` handles that natively —
    # all positional/keyword shapes still work. This explicit method
    # only exists so subclass signature is preserved in IDE / tooling
    # introspection.
    def recover(
        self,
        critical_failures: List[str],
        service_names: List[str],
        milestone_title: str,
    ) -> PhaseRecoveryResult:
        return super().recover(
            critical_failures=critical_failures,
            service_names=service_names,
            milestone_title=milestone_title,
        )


_USER_TEMPLATE = """\
SMOKE FAILURE — milestone: {milestone_title}

The smoke phase just halted with these critical failures:

{failure_block}

Stack context:
  - compose file: {compose_path}
  - project root: {project_root}
  - services in this stack: {services_block}

Attempt recovery per your system prompt. Bias toward the cheapest
reversible action (container restart > config edit > rebuild).
Return the final summary lines (ACTION / RECOVERY SUCCESS /
RECOVERY FAILED) when done.
"""


# ── Multi-tier wrapper (item 7C) ─────────────────────────────────


from typing import Callable as _Callable  # noqa: E402
from pydantic import BaseModel as _BaseModel, Field as _Field  # noqa: E402

from bizniz.lib.tier_escalation import (  # noqa: E402
    AttemptOutcome, EscalationResult, TierSpec, escalate,
)


class MultiTierSmokeRecovery:
    """Walks a tier list (cheap → mid → expensive), running each
    tier's ``SmokeRecovery`` and re-verifying smoke between attempts.

    Each tier's ``factory`` returns a fresh ``SmokeRecovery`` bound
    to the tier's model. The escalation primitive handles the
    loop — this class is the glue.

    Compared to single-shot ``SmokeRecovery``: heavier (more API
    calls if early tiers fail), but recovers from harder problems
    that one cheap-model attempt can't handle.
    """

    def __init__(
        self,
        tiers: List[TierSpec["SmokeRecovery"]],
        verify_smoke: _Callable[[], bool],
        on_status: Optional[_Callable[[str], None]] = None,
    ) -> None:
        if not tiers:
            raise ValueError(
                "MultiTierSmokeRecovery: tiers list must be non-empty"
            )
        self._tiers = tiers
        self._verify_smoke = verify_smoke
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def recover(
        self,
        critical_failures: List[str],
        service_names: List[str],
        milestone_title: str,
    ) -> "MultiTierRecoveryResult":
        """Try each tier in order; on each, run SmokeRecovery once,
        then re-verify smoke. Return the first attempt that produces
        a passing smoke, or a structured failure if none do."""

        def attempt_fn(agent: "SmokeRecovery", ti, ai, prior):
            self._log(
                f"MultiTierSmokeRecovery: tier {ti} attempt {ai} "
                f"({self._tiers[ti].label!r})..."
            )
            sr_result = agent.recover(
                critical_failures=critical_failures,
                service_names=service_names,
                milestone_title=milestone_title,
            )
            # After recovery action, re-verify smoke.
            smoke_passes = False
            verify_error: Optional[str] = None
            try:
                smoke_passes = bool(self._verify_smoke())
            except Exception as e:
                verify_error = (
                    f"verify_smoke raised: {type(e).__name__}: {e}"
                )
                self._log(
                    f"MultiTierSmokeRecovery: {verify_error}"
                )
            output_parts = [
                f"recovery_succeeded={sr_result.succeeded}",
                f"recovery_summary={sr_result.summary[:200]}",
                f"smoke_passes_after={smoke_passes}",
            ]
            if verify_error:
                output_parts.append(verify_error)
            return AttemptOutcome(
                succeeded=smoke_passes,
                output=" | ".join(output_parts),
            )

        escalation = escalate(
            self._tiers, attempt_fn,
            on_status=self._on_status,
        )
        return MultiTierRecoveryResult(
            succeeded=escalation.succeeded,
            final_tier_label=escalation.final_tier_label,
            total_attempts=escalation.total_attempts,
            summary=escalation.final_output,
            tier_history=[
                f"{a.tier_label}#{a.attempt_index}: "
                f"{'PASS' if a.succeeded else 'fail'}"
                for a in escalation.attempts
            ],
        )


class MultiTierRecoveryResult(_BaseModel):
    """End-of-escalation result for ``MultiTierSmokeRecovery``."""
    succeeded: bool = False
    final_tier_label: Optional[str] = None
    total_attempts: int = 0
    summary: str = ""
    tier_history: List[str] = _Field(default_factory=list)
