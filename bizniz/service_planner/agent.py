"""ServicePlanner — single-call agent that emits Coder issues.

ONE call per service. Takes (architecture, EnrichedSpec, ServiceDefinition,
optional skeleton_md / auth_contract) and returns a topo-sorted List[Issue].

This is what replaces v1's Engineer.analyze() + Engineer.plan() and
the earlier v2 Engineer.implement()'s issue-generation step. The
generated issues feed the Orchestrator, which dispatches the v2.5 Coder
per issue.

No tool loop, no multi-turn — just a structured-output JSON call with
retry on transient failures.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.code_reviewer.types import CodeReviewReport
from bizniz.coder.types import Issue
from bizniz.lib.dependency_graph import (
    CyclicDependencyError, topological_layers,
)
from bizniz.lib.llm_utils import call_with_retry
from bizniz.quality_engineer.types import CoverageReport, EnrichedSpec
from bizniz.service_planner.prompts.repair_prompt import build_repair_prompt
from bizniz.service_planner.prompts.schema import SERVICE_PLANNER_SCHEMA
from bizniz.service_planner.prompts.system_prompt import (
    SERVICE_PLANNER_SYSTEM_PROMPT,
)
from bizniz.service_planner.prompts.user_prompt import (
    build_service_planner_prompt,
)


class ServicePlannerError(Exception):
    """The ServicePlanner's LLM output failed validation."""


class ServicePlanner:
    """Single-call structured-output agent: spec → list of issues."""

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
    ) -> List[Issue]:
        """Return a List[Issue] for ``service``, topologically sorted.

        Issues are validated on the way out:
          - id must be unique within the response
          - depends_on must reference ids in the same response
          - the depends_on graph must be a DAG (no cycles)
          - target_files / test_files must be non-empty
        """
        self._log(f"ServicePlanner: {service.name}")

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
                Message(role="system", content=SERVICE_PLANNER_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=SERVICE_PLANNER_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"ServicePlanner({service.name})",
        )

        items = raw.get("issues") or []
        if not items:
            raise ServicePlannerError(
                f"ServicePlanner returned 0 issues for service "
                f"{service.name!r}. Refusing to ship an empty plan."
            )

        # Stamp the service + language onto every issue (LLM may forget,
        # and the schema doesn't include them — they're invariant per call).
        for it in items:
            it["service"] = service.name
            it["language"] = (service.language or "python").lower()

        issues: List[Issue] = []
        for it in items:
            try:
                issues.append(Issue.model_validate(it))
            except Exception as e:
                raise ServicePlannerError(
                    f"ServicePlanner({service.name}): issue failed "
                    f"validation — {e}; payload: {it!r}"
                ) from e

        self._validate_unique_ids(issues, service.name)
        self._validate_dep_targets(issues, service.name)
        issues = self._validate_files_non_empty(issues, service.name)

        # Topo-sort. Stable across runs because the LLM's relative
        # ordering of issues feeds Kahn's algorithm directly.
        try:
            layers = topological_layers(issues)
        except CyclicDependencyError as e:
            raise ServicePlannerError(
                f"ServicePlanner({service.name}): {e}"
            ) from e

        ordered: List[Issue] = []
        for layer in layers:
            ordered.extend(layer)

        self._log(
            f"ServicePlanner: {service.name} → {len(ordered)} issue(s) "
            f"in {len(layers)} layer(s)"
        )
        return ordered

    def plan_repair(
        self,
        *,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        service: ServiceDefinition,
        prior_issues: List[Issue],
        prior_dispositions: dict,
        coverage_report: Optional[CoverageReport],
        code_review_report: Optional[CodeReviewReport],
        repair_iteration: int,
        skeleton_md: Optional[str] = None,
        auth_contract: Optional[str] = None,
        # v4 fix #4 (2026-05-19): optional snapshot of the live
        # workspace state so the planner doesn't emit fix-issues for
        # things already partially addressed by earlier iters. When
        # None, prompt unchanged (back-compat with v3.1 + v2 callers).
        workspace_summary: Optional[str] = None,
    ) -> List[Issue]:
        """Repair-mode planning. Takes review findings + prior issues
        and emits MINIMUM fix-issues.

        Returns ``[]`` if the planner determined no fixes are needed
        for this service (findings were elsewhere). The caller should
        treat empty as "no work for this service this iteration."
        """
        self._log(
            f"ServicePlanner (repair iter {repair_iteration}): {service.name}"
        )

        user_prompt = build_repair_prompt(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=service,
            prior_issues=prior_issues,
            prior_dispositions=prior_dispositions,
            coverage_report=coverage_report,
            code_review_report=code_review_report,
            repair_iteration=repair_iteration,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
            workspace_summary=workspace_summary,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=SERVICE_PLANNER_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=SERVICE_PLANNER_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"ServicePlanner.repair({service.name}, iter{repair_iteration})",
        )

        items = raw.get("issues") or []
        if not items:
            # Empty repair plan is legal — this service had no findings.
            self._log(
                f"ServicePlanner (repair iter {repair_iteration}): "
                f"{service.name} → no fixes needed"
            )
            return []

        for it in items:
            it["service"] = service.name
            it["language"] = (service.language or "python").lower()

        # Lenient payload validation in repair mode — losing one bad
        # fix-issue is preferable to crashing the milestone. Same
        # philosophy as ``_repair_dep_targets`` and
        # ``_validate_files_non_empty``. Greenfield ``plan_service``
        # keeps strict validation since unknown payloads there are
        # real defects worth surfacing.
        issues: List[Issue] = []
        dropped_payloads = 0
        for it in items:
            try:
                issues.append(Issue.model_validate(it))
            except Exception as e:
                dropped_payloads += 1
                self._log(
                    f"ServicePlanner.repair({service.name}): dropping "
                    f"invalid fix-issue payload — {e}; payload: {it!r}"
                )
        if dropped_payloads and not issues:
            self._log(
                f"ServicePlanner.repair({service.name}): all "
                f"{dropped_payloads} payload(s) invalid — returning no "
                f"fix-issues this iter (milestone will proceed to next gate)"
            )
            return []

        self._validate_unique_ids(issues, service.name)
        issues = self._repair_dep_targets(issues, service.name)
        issues = self._validate_files_non_empty(issues, service.name)

        try:
            layers = topological_layers(issues)
        except CyclicDependencyError as cycle_err:
            issues = self._break_cycle(issues, cycle_err, service.name)
            layers = topological_layers(issues)

        ordered: List[Issue] = []
        for layer in layers:
            ordered.extend(layer)

        self._log(
            f"ServicePlanner (repair iter {repair_iteration}): "
            f"{service.name} → {len(ordered)} fix-issue(s)"
        )
        return ordered

    # ── Validation ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_unique_ids(issues: List[Issue], service_name: str) -> None:
        seen: set = set()
        dups: List[str] = []
        for i in issues:
            if i.id in seen:
                dups.append(i.id)
            seen.add(i.id)
        if dups:
            raise ServicePlannerError(
                f"ServicePlanner({service_name}): duplicate issue ids: {dups}"
            )

    @staticmethod
    def _validate_dep_targets(issues: List[Issue], service_name: str) -> None:
        ids = {i.id for i in issues}
        bad: List[str] = []
        for i in issues:
            for d in i.depends_on:
                if d not in ids:
                    bad.append(f"{i.id} → {d}")
        if bad:
            raise ServicePlannerError(
                f"ServicePlanner({service_name}): depends_on references "
                f"unknown issue id(s): {bad}"
            )

    def _break_cycle(
        self,
        issues: List[Issue],
        cycle_err: "CyclicDependencyError",
        service_name: str,
    ) -> List[Issue]:
        """Repair-mode counterpart to the strict cycle raise.

        Drops every ``depends_on`` edge whose source is in the
        cyclic_ids set, so topo-sort succeeds on the re-call. Issues
        entirely outside the cycle's reachability keep all their
        edges.

        Note: ``CyclicDependencyError.cyclic_ids`` from Kahn's
        algorithm includes both items strictly IN the cycle AND
        items merely blocked behind it. So a blocked-behind item C
        that depends on a cycle member A also loses its edge — best-
        effort repair trades C's ordering hint for "milestone keeps
        moving." Greenfield ``plan_service`` keeps the strict raise
        because cycles in a fresh plan usually mean the LLM
        contradicted itself.
        """
        cyclic_set = {cid for cid in cycle_err.cyclic_ids}
        self._log(
            f"ServicePlanner.repair({service_name}): dependency cycle "
            f"involving {sorted(cyclic_set)} — dropping inter-cycle dep "
            f"edges and re-sorting"
        )
        repaired: List[Issue] = []
        for issue in issues:
            if issue.id in cyclic_set:
                new_deps = [
                    d for d in issue.depends_on if d not in cyclic_set
                ]
                issue = issue.model_copy(update={"depends_on": new_deps})
            repaired.append(issue)
        return repaired

    def _repair_dep_targets(
        self, issues: List[Issue], service_name: str,
    ) -> List[Issue]:
        """Repair-mode counterpart to ``_validate_dep_targets``.

        Drops unknown dep edges with a warning instead of raising.
        Repair iterations are a side-channel and losing a dep edge
        (worst case: a fix-issue runs in a slightly suboptimal order)
        is preferable to aborting the milestone. Live-surfaced
        repair iter 1 on CRM v1 M5 where the LLM emitted
        ``BA-fix1-3 depends_on=['BA-fix1-2']`` without emitting
        ``BA-fix1-2`` itself.
        """
        ids = {i.id for i in issues}
        repaired: List[Issue] = []
        for i in issues:
            bad = [d for d in i.depends_on if d not in ids]
            if not bad:
                repaired.append(i)
                continue
            good = [d for d in i.depends_on if d in ids]
            self._log(
                f"ServicePlanner.repair({service_name}): issue {i.id} cites "
                f"unknown dep(s) {bad} — dropping those edges, continuing"
            )
            repaired.append(i.model_copy(update={"depends_on": good}))
        return repaired

    def _validate_files_non_empty(
        self, issues: List[Issue], service_name: str,
    ) -> List[Issue]:
        """Auto-repair instead of raising:

        - Empty test_files: auto-fill with ``tests/test_<id>.py``.
          The LLM occasionally omits test_files; we'd rather give the
          Coder a default path than lose the issue.
        - Empty target_files: drop the issue with a warning. Without
          target_files there's nothing for the Coder to edit, and the
          repair iteration is a side-channel — losing one fix-issue
          is better than crashing the milestone. Recipe_box repair
          iter 2 surfaced ``BA-fix2-1`` with empty target_files when
          the LLM punted; the gate now drops it instead of raising.
        """
        dropped_no_target: List[str] = []
        repaired: List[Issue] = []
        for i in issues:
            if not i.target_files:
                dropped_no_target.append(i.id)
                continue
            if not i.test_files:
                slug = i.id.lower().replace("-", "_").replace(":", "_")
                default_test = f"tests/test_{slug}.py"
                self._log(
                    f"ServicePlanner({service_name}): {i.id} had no "
                    f"test_files — auto-filling with {default_test!r}"
                )
                i = i.model_copy(update={"test_files": [default_test]})
            repaired.append(i)
        if dropped_no_target:
            self._log(
                f"ServicePlanner({service_name}): dropped "
                f"{len(dropped_no_target)} issue(s) with empty "
                f"target_files: {dropped_no_target}"
            )
        return repaired

    # ── Status ─────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass
