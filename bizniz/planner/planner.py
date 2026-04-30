"""
Planner — top-level sequencing agent.

Produces a ProjectPlan: an ordered list of Milestones that the rest of
the pipeline (Architect → Provisioner → Engineer → Orchestrator) walks
through one milestone at a time.

The Planner is a one-shot agent — typically called once at the
beginning of a project, with optional re-plans when the user adds
scope. It runs on the top-tier model (default ``gemini-pro``) because
the cost of one extra top-tier call per project is rounding error and
the quality bump is foundational.
"""
from __future__ import annotations

import json
import time
from typing import Callable, List, Optional

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace.naming import slugify

from bizniz.planner.types import (
    Milestone,
    ProjectPlan,
    PlannerBadAIResponseError,
)
from bizniz.planner.prompts.system_prompt import PLANNER_SYSTEM_PROMPT
from bizniz.planner.prompts.plan_prompt import (
    build_plan_prompt,
    EXISTING_STATE_TEMPLATE,
)
from bizniz.planner.prompts.schema import PlannerSchema


class Planner(BaseAIAgent):
    """Decomposes a problem into ordered, deliverable milestones."""

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        max_retries: Optional[int] = 3,
        on_event: Optional[Callable] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(
            client=client,
            environment=environment,
            workspace=workspace,
            max_retries=max_retries,
            on_event=on_event,
            on_status_message=on_status_message,
        )

    @property
    def _process_system_prompt(self) -> str:
        return PLANNER_SYSTEM_PROMPT

    # ── Public API ────────────────────────────────────────────────────────────

    def plan(
        self,
        problem_statement: str,
        project_name: str,
        existing_architecture=None,
        completed_milestones: Optional[List[Milestone]] = None,
        project_db=None,
    ) -> ProjectPlan:
        """Decompose ``problem_statement`` into ordered milestones.

        Parameters
        ----------
        problem_statement:
            Natural-language description of what to build.
        project_name:
            Human-readable project name (slugified for ``project_slug``).
        existing_architecture:
            Optional ``SystemArchitecture`` for re-plans against an
            existing project. When set, the Planner is told what already
            exists and asked to plan only the remaining work.
        completed_milestones:
            Optional list of already-shipped milestones to give the
            Planner context for what's already done.
        project_db:
            When provided, persist the produced plan + milestones to the
            project DB. Older active plans for the same slug are
            archived first so ``get_active_plan`` returns the new one.
        """
        def log(msg: str) -> None:
            if self._on_status_message:
                self._on_status_message(msg)

        project_slug = slugify(project_name)
        existing_block = ""
        if existing_architecture is not None or completed_milestones:
            existing_block = self._format_existing_state_block(
                existing_architecture, completed_milestones,
            )

        log(f"Planner: planning '{project_name}'...")
        user_prompt = build_plan_prompt(
            problem_statement=problem_statement,
            project_name=project_name,
            project_slug=project_slug,
            existing_state_block=existing_block,
        )
        raw = self._call_ai_for_plan(user_prompt)

        milestones = [
            Milestone(
                sequence_index=int(m.get("sequence_index", i)),
                name=m["name"],
                problem_slice=m["problem_slice"],
                use_cases=list(m.get("use_cases") or []),
                success_criteria=list(m.get("success_criteria") or []),
                depends_on_names=list(m.get("depends_on_names") or []),
                estimated_effort=m.get("estimated_effort"),
            )
            for i, m in enumerate(raw.get("milestones") or [])
        ]
        # Stable-sort by sequence_index so AI numbering issues don't
        # produce out-of-order plans
        milestones.sort(key=lambda x: x.sequence_index)

        if not milestones:
            raise PlannerBadAIResponseError(
                "Planner returned no milestones — refusing to ship an empty plan."
            )

        plan = ProjectPlan(
            project_slug=raw.get("project_slug") or project_slug,
            problem_statement=problem_statement,
            description=raw.get("description") or "",
            milestones=milestones,
        )

        log(
            f"Planner: produced {len(plan.milestones)} milestone(s): "
            + " → ".join(m.name for m in plan.milestones)
        )

        if project_db is not None:
            self._persist_plan(plan, project_db, log)

        return plan

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist_plan(self, plan: ProjectPlan, project_db, log) -> None:
        """Save the plan + milestones to the project DB. Archives any
        prior active plan for the same project_slug first so
        ``get_active_plan`` always returns the newest."""
        try:
            existing = project_db.get_active_plan(plan.project_slug)
            if existing is not None:
                project_db.archive_plan(existing["id"])
                log(f"Planner: archived prior plan #{existing['id']}")

            plan_id = project_db.save_project_plan(
                project_slug=plan.project_slug,
                problem_statement=plan.problem_statement,
                description=plan.description,
            )
            plan.db_id = plan_id

            for m in plan.milestones:
                m.db_id = project_db.save_milestone(
                    plan_id=plan_id,
                    sequence_index=m.sequence_index,
                    name=m.name,
                    problem_slice=m.problem_slice,
                    use_cases=m.use_cases,
                    success_criteria=m.success_criteria,
                    depends_on_names=m.depends_on_names,
                    estimated_effort=m.estimated_effort,
                    status=m.status,
                )
            log(f"Planner: persisted plan #{plan_id} with {len(plan.milestones)} milestones")
        except Exception as e:
            log(f"Planner: failed to persist plan ({type(e).__name__}: {e})")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _format_existing_state_block(
        self,
        existing_architecture,
        completed_milestones: Optional[List[Milestone]],
    ) -> str:
        arch_summary = "(none)"
        if existing_architecture is not None:
            services = getattr(existing_architecture, "services", [])
            if services:
                arch_summary = "\n".join(
                    f"- {s.name} ({s.framework}/{s.language}, {s.service_type})"
                    f"{': ' + s.description if getattr(s, 'description', '') else ''}"
                    for s in services
                )

        completed = "(none)"
        if completed_milestones:
            completed = "\n".join(
                f"- {m.name}: {m.problem_slice[:120]}"
                for m in completed_milestones
            )

        return EXISTING_STATE_TEMPLATE.format(
            architecture_summary=arch_summary,
            completed_milestones=completed,
        )

    # ── AI call ──────────────────────────────────────────────────────────────

    def _call_ai_for_plan(self, user_prompt: str) -> dict:
        """Call AI for project planning and return parsed JSON."""
        attempts = self.max_retries
        last_error: Optional[Exception] = None

        def log(msg: str) -> None:
            if self._on_status_message:
                self._on_status_message(msg)

        self.clear_message_history()
        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                log(f"Planner: AI plan call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, _, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=PlannerSchema,
                )
                elapsed = time.time() - t0
                log(f"Planner: AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = self.clean_llm_json(text)
                return json.loads(text)
            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"Planner: attempt {attempt} failed — {type(e).__name__}: {e}")
                continue

        raise PlannerBadAIResponseError(
            f"Planner failed to produce a project plan after {attempts} "
            f"attempts. Last error: {last_error}"
        )
