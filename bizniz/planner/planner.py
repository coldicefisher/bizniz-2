"""Planner — top-level sequencing agent.

v2 single-call agent. Produces a ``ProjectPlan``: an ordered list of
``Milestone`` objects that the Architect walks through one at a time.

Plain class, no ABC, no tool loop. The Planner makes a single LLM call
with structured JSON output. The orchestrator handles file I/O before
and after; the Planner itself is stateless beyond the LLM client.

Re-planning is intentionally not supported in v2.0 — if the project's
scope changes, edit the plan.json on disk or build a dedicated
``extend_plan`` method later when there's a real use case for it. The
historical re-plan path drifted in practice (AI proposed rebuilds,
restructured the roadmap, forgot to chain) without enough usage to
justify the complexity.
"""
from __future__ import annotations

from typing import Callable, Optional

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.lib.llm_utils import call_with_retry
from bizniz.workspace.naming import slugify

from bizniz.planner.types import (
    Milestone,
    ProjectPlan,
    PlannerBadAIResponseError,
)
from bizniz.planner.prompts.system_prompt import PLANNER_SYSTEM_PROMPT
from bizniz.planner.prompts.plan_prompt import build_plan_prompt
from bizniz.planner.prompts.schema import PlannerSchema


class Planner:
    """Decomposes a problem statement into ordered, deliverable milestones.

    Single-call agent — one LLM round-trip per ``plan()`` invocation
    (modulo retries). No tools, no inheritance, no per-call state.
    """

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    # ── Public API ────────────────────────────────────────────────────────────

    def plan(
        self,
        problem_statement: str,
        project_name: str,
        project_db=None,
    ) -> ProjectPlan:
        """Decompose ``problem_statement`` into ordered milestones.

        Parameters
        ----------
        problem_statement:
            Natural-language description of what to build.
        project_name:
            Human-readable project name (slugified for ``project_slug``).
        project_db:
            When provided, persist the produced plan + milestones to
            the project DB.
        """
        project_slug = slugify(project_name)

        self._log(f"Planner: planning '{project_name}'...")
        user_prompt = build_plan_prompt(
            problem_statement=problem_statement,
            project_name=project_name,
            project_slug=project_slug,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=PLANNER_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=PlannerSchema,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="Planner",
        )

        plan = self._build_plan(raw, project_slug, problem_statement)

        self._log(
            f"Planner: produced {len(plan.milestones)} milestone(s): "
            + " → ".join(m.name for m in plan.milestones)
        )

        if project_db is not None:
            self._persist(plan, project_db)

        return plan

    # ── Internals ────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def _build_plan(
        self,
        raw: dict,
        project_slug: str,
        problem_statement: str,
    ) -> ProjectPlan:
        """Parse the LLM output into a typed ``ProjectPlan``. Stable-sorts
        milestones by ``sequence_index`` so AI numbering glitches don't
        produce out-of-order plans."""
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
        milestones.sort(key=lambda x: x.sequence_index)

        if not milestones:
            raise PlannerBadAIResponseError(
                "Planner returned no milestones — refusing to ship an empty plan."
            )

        return ProjectPlan(
            project_slug=raw.get("project_slug") or project_slug,
            problem_statement=problem_statement,
            description=raw.get("description") or "",
            milestones=milestones,
        )

    def _persist(self, plan: ProjectPlan, project_db) -> None:
        """Save the plan + milestones to the project DB. Archives any
        prior active plan for the same project_slug first so
        ``get_active_plan`` always returns the newest."""
        try:
            existing = project_db.get_active_plan(plan.project_slug)
            if existing is not None:
                project_db.archive_plan(existing["id"])
                self._log(f"Planner: archived prior plan #{existing['id']}")

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
            self._log(
                f"Planner: persisted plan #{plan_id} with "
                f"{len(plan.milestones)} milestones"
            )
        except Exception as e:
            self._log(
                f"Planner: failed to persist plan "
                f"({type(e).__name__}: {e})"
            )
