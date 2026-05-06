"""AuthAgent — owns FusionAuth runtime configuration + auth-correctness audits.

Tool-loop agent (inherits ToolLoopAgent). Two modes:
  - ``configure`` — plan the milestone's auth state, apply it, verify
  - ``audit``     — verify only, no mutations

Independently invokable: pipeline-driven (called per-milestone by the
v2 pipeline driver) or solo (CLI entry point against an existing
project for production fixes / drift checks).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.auth.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth_agent.fa_tools import build_fa_handlers
from bizniz.auth_agent.prompt import build_auth_agent_system_prompt
from bizniz.auth_agent.schema import AUTH_AGENT_ACTION_SCHEMA
from bizniz.auth_agent.types import (
    AuditReport,
    AuthAgentMode,
    AuthAgentResult,
)
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.lib.tool_loop_agent import ToolHandler, ToolLoopAgent
from bizniz.workspace.base_workspace import BaseWorkspace


class AuthAgent(ToolLoopAgent):
    """Owns FusionAuth runtime configuration + auth-correctness audits.

    Construction is mode-agnostic; mode is chosen at ``run()``-call time
    via the typed entry methods ``configure()`` and ``audit()``. Both
    methods build the appropriate initial context + tool surface, then
    delegate to the inherited tool loop.
    """

    def __init__(
        self,
        client: BaseAIClient,
        workspace: BaseWorkspace,
        fa_orchestrator: FusionAuthOrchestrator,
        on_status: Optional[Callable[[str], None]] = None,
        tool_iterations: int = 30,
        timeout_seconds: int = 600,
    ):
        super().__init__(
            client=client,
            workspace=workspace,
            on_status=on_status,
            tool_iterations=tool_iterations,
            timeout_seconds=timeout_seconds,
        )
        self._fa_orchestrator = fa_orchestrator
        # Set per-call by configure() / audit(). The ABC reads these
        # via the abstract properties below.
        self._mode: AuthAgentMode = "configure"
        self._handlers: Dict[str, ToolHandler] = {}

    # ── ToolLoopAgent contract ──────────────────────────────────────────────

    @property
    def system_prompt(self) -> str:
        return build_auth_agent_system_prompt(self._mode)

    @property
    def action_schema(self) -> dict:
        return AUTH_AGENT_ACTION_SCHEMA

    @property
    def terminal_action(self) -> str:
        return "submit_contract"

    def tool_handlers(self) -> Dict[str, ToolHandler]:
        return self._handlers

    def parse_terminal_action(self, action: dict) -> AuthAgentResult:
        return AuthAgentResult(
            mode=self._mode,
            contract_markdown=action.get("contract_markdown") or "",
            summary=action.get("summary") or "",
            applied_changes=list(action.get("applied_changes") or []),
            audit=AuditReport(),  # populated by run_audit_battery later
        )

    # ── Public entry methods ────────────────────────────────────────────────

    def configure(
        self,
        problem_slice: str,
        architecture: SystemArchitecture,
        primary_app_id: str,
        tenant_id: str,
        existing_contract: Optional[str] = None,
        write_contract_to: Optional[Path] = None,
    ) -> AuthAgentResult:
        """Configure mode: apply the milestone's auth state to FA, then verify.

        ``existing_contract`` is the prior AUTH_CONTRACT.md (if any) —
        passed to the agent so it knows what state was claimed before
        and can surface drift.

        ``write_contract_to`` is an optional path. When provided, the
        result's ``contract_markdown`` is written to disk and the
        result's ``contract_path`` is populated.
        """
        return self._run_mode(
            mode="configure",
            problem_slice=problem_slice,
            architecture=architecture,
            primary_app_id=primary_app_id,
            tenant_id=tenant_id,
            existing_contract=existing_contract,
            write_contract_to=write_contract_to,
        )

    def audit(
        self,
        architecture: SystemArchitecture,
        primary_app_id: str,
        tenant_id: str,
        existing_contract: Optional[str] = None,
        write_contract_to: Optional[Path] = None,
    ) -> AuthAgentResult:
        """Audit mode: verify FA state without mutating it.

        Equivalent to ``configure(problem_slice="(audit only — no
        changes)", ...)`` but with mutating tools removed from the
        agent's surface and the prompt set to audit semantics.
        """
        return self._run_mode(
            mode="audit",
            problem_slice="(audit-only run — no state changes permitted)",
            architecture=architecture,
            primary_app_id=primary_app_id,
            tenant_id=tenant_id,
            existing_contract=existing_contract,
            write_contract_to=write_contract_to,
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _run_mode(
        self,
        mode: AuthAgentMode,
        problem_slice: str,
        architecture: SystemArchitecture,
        primary_app_id: str,
        tenant_id: str,
        existing_contract: Optional[str],
        write_contract_to: Optional[Path],
    ) -> AuthAgentResult:
        self._mode = mode
        self._handlers = build_fa_handlers(
            self._fa_orchestrator,
            audit_mode=(mode == "audit"),
        )

        initial = self._build_initial_context(
            mode=mode,
            problem_slice=problem_slice,
            architecture=architecture,
            primary_app_id=primary_app_id,
            tenant_id=tenant_id,
            existing_contract=existing_contract,
        )

        result: AuthAgentResult = self.run(initial)

        # Run the deterministic audit battery against the post-loop state.
        result.audit = self._run_audit_battery(
            architecture=architecture,
            primary_app_id=primary_app_id,
            tenant_id=tenant_id,
            contract_markdown=result.contract_markdown,
        )

        if write_contract_to is not None and result.contract_markdown:
            try:
                write_contract_to.parent.mkdir(parents=True, exist_ok=True)
                write_contract_to.write_text(result.contract_markdown)
                result.contract_path = str(write_contract_to)
                self._log(
                    f"AuthAgent: wrote AUTH_CONTRACT.md to {write_contract_to}"
                )
            except Exception as e:
                self._log(
                    f"AuthAgent: failed to write contract "
                    f"({type(e).__name__}: {e})"
                )

        return result

    def _build_initial_context(
        self,
        mode: AuthAgentMode,
        problem_slice: str,
        architecture: SystemArchitecture,
        primary_app_id: str,
        tenant_id: str,
        existing_contract: Optional[str],
    ) -> str:
        languages = sorted({
            s.language for s in architecture.services
            if s.service_type in ("backend", "frontend", "worker")
        })
        services_block = "\n".join(
            f"  - {s.name} ({s.service_type}, {s.framework}/{s.language}, "
            f"port={s.port})"
            for s in architecture.services
        )

        existing_block = (
            f"\n\nEXISTING AUTH_CONTRACT.md (cumulative state from "
            f"prior milestones):\n```markdown\n{existing_contract}\n```\n"
            if existing_contract else
            "\n\nNo prior AUTH_CONTRACT.md exists yet (this is the first "
            "milestone that touches auth, or solo audit on a fresh project).\n"
        )

        return (
            f"AUTHAGENT RUN — MODE: {mode.upper()}\n"
            f"\n"
            f"Project architecture (the languages list determines which "
            f"verification code samples must appear in the contract):\n"
            f"{services_block}\n"
            f"\n"
            f"Stack languages: {', '.join(languages) or '(none)'}\n"
            f"\n"
            f"FusionAuth coordinates:\n"
            f"  - primary application UUID: {primary_app_id}\n"
            f"  - tenant UUID: {tenant_id}\n"
            f"\n"
            f"Milestone problem slice (what auth state this milestone "
            f"requires):\n{problem_slice}\n"
            f"{existing_block}\n"
            f"Begin by reading live FA state (fa_diagnose, fa_list_roles, "
            f"fa_list_users, smoke-login + decode_jwt). Do NOT trust the "
            f"prior contract or your assumptions about what FA emits — "
            f"measure first."
        )

    def _run_audit_battery(
        self,
        architecture: SystemArchitecture,
        primary_app_id: str,
        tenant_id: str,
        contract_markdown: str,
    ) -> AuditReport:
        """Deterministic post-loop audit battery. Same battery for both
        ``configure`` and ``audit`` modes.

        See ``bizniz/auth_agent/audits.py`` for the individual checks.
        v2.0 ships 5 checks: jwks_reachable, jwt_signing,
        token_validation (per test user), test_users_in_fa,
        credential_exposure. Two more (role_enforcement,
        idempotency_replay) are queued for v2.1.
        """
        from bizniz.auth_agent.audits import run_audit_battery
        try:
            report = run_audit_battery(
                orch=self._fa_orchestrator,
                workspace=self._workspace,
                architecture=architecture,
                primary_app_id=primary_app_id,
                tenant_id=tenant_id,
                contract_markdown=contract_markdown,
            )
        except Exception as e:
            # Audits are best-effort: a battery crash shouldn't kill
            # the whole AuthAgent run. Log + return an empty report.
            self._log(
                f"AuthAgent: audit battery raised "
                f"({type(e).__name__}: {e}) — returning empty report"
            )
            return AuditReport(checks=[])

        passed = sum(1 for c in report.checks if c.passed)
        total = len(report.checks)
        self._log(
            f"AuthAgent: audit battery — {passed}/{total} checks passed"
        )
        for c in report.failed:
            self._log(f"AuthAgent: audit FAIL [{c.name}] — {c.detail}")
        return report
