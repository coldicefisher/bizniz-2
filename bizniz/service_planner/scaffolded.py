"""``ServicePlannerWithScaffold`` — test variant for the v3 pipeline spec.

Same single-call shape as production ``ServicePlanner``, but with an
extended schema + prompt that ALSO emits ``seeded_files``: concrete
scaffold of every file an issue's target_files references, with signatures
+ imports + types complete and bodies left as
``raise NotImplementedError`` / ``pass``.

Lives separately from production ``ServicePlanner`` so the test variant
doesn't disturb live builds. If the seeded-scaffold idea proves out, this
schema + prompt promote into the production agent. Reuses the production
user-prompt builder (same inputs).
"""
from __future__ import annotations

from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.coder.types import Issue
from bizniz.lib.llm_utils import call_with_retry
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.service_planner.agent import ServicePlannerError
from bizniz.service_planner.prompts.schema_with_scaffold import (
    SERVICE_PLANNER_SCAFFOLD_SCHEMA,
)
from bizniz.service_planner.prompts.system_prompt_with_scaffold import (
    SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT,
)
from bizniz.service_planner.prompts.user_prompt import (
    build_service_planner_prompt,
)


class SeededFile(BaseModel):
    """One file in the scaffold."""
    path: str = Field(..., description="Workspace-relative path.")
    content: str = Field(..., description="Complete file contents.")
    rationale: str = Field(..., description="What the file exports + which issue fills it.")


class ScaffoldedPlanResult(BaseModel):
    """ServicePlannerWithScaffold's return type.

    Same issues list as production ServicePlanner produces, plus the
    seeded_files array. Issues are NOT topologically sorted here (we
    leave that to the caller / test scenario).
    """
    issues: List[Issue] = Field(default_factory=list)
    seeded_files: List[SeededFile] = Field(default_factory=list)


class ServicePlannerWithScaffold:
    """Test variant of ServicePlanner that also emits a seeded scaffold."""

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    def plan_service(
        self,
        *,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        service: ServiceDefinition,
        skeleton_md: Optional[str] = None,
        auth_contract: Optional[str] = None,
    ) -> ScaffoldedPlanResult:
        """Return issues + seeded_files for ``service``.

        Same input surface as production ``ServicePlanner.plan_service``.
        Validates only the LLM output shape — AST / symbol checks happen
        in the perf-test scenario so this stays a thin LLM wrapper.
        """
        self._log(f"ServicePlannerWithScaffold: {service.name}")

        user_prompt = build_service_planner_prompt(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=service,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=SERVICE_PLANNER_SCAFFOLD_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"ServicePlannerWithScaffold({service.name})",
        )

        items = raw.get("issues") or []
        if not items:
            raise ServicePlannerError(
                f"ServicePlannerWithScaffold returned 0 issues for "
                f"{service.name!r}. Refusing to ship an empty plan."
            )
        seeded = raw.get("seeded_files") or []
        if not seeded:
            raise ServicePlannerError(
                f"ServicePlannerWithScaffold returned 0 seeded_files for "
                f"{service.name!r}. The whole point of this variant is "
                f"to emit a seeded scaffold."
            )

        # Stamp invariants onto issues, same shape as production.
        for it in items:
            it["service"] = service.name
            it["language"] = (service.language or "python").lower()

        issues: List[Issue] = []
        for it in items:
            try:
                issues.append(Issue(**it))
            except Exception as e:
                raise ServicePlannerError(
                    f"ServicePlannerWithScaffold issue failed validation: "
                    f"{type(e).__name__}: {e}; item: {it}"
                )

        seeded_files: List[SeededFile] = []
        for s in seeded:
            try:
                seeded_files.append(SeededFile(**s))
            except Exception as e:
                raise ServicePlannerError(
                    f"ServicePlannerWithScaffold seeded_file failed "
                    f"validation: {type(e).__name__}: {e}; item: {s}"
                )

        self._log(
            f"ServicePlannerWithScaffold: {service.name} → "
            f"{len(issues)} issue(s), {len(seeded_files)} seeded file(s)"
        )
        return ScaffoldedPlanResult(issues=issues, seeded_files=seeded_files)

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass
