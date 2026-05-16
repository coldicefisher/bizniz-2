"""CodeReviewer — fresh-context, single-call code review agent."""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.code_reviewer.prompts.review_prompt import (
    CODE_REVIEW_SCHEMA,
    build_review_prompt,
)
from bizniz.code_reviewer.prompts.system_prompt import CODE_REVIEWER_SYSTEM_PROMPT
from bizniz.code_reviewer.types import CodeReviewError, CodeReviewReport
from bizniz.lib.llm_utils import call_with_retry
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import EnrichedSpec


class CodeReviewer:
    """Single-call code review agent with fresh context every run.

    Distinct from QualityEngineer: this one DOES read source code (it
    must — that's the point). No chat history with the Engineer; reads
    the Engineer's output cold.
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

    def review(
        self,
        milestone: Milestone,
        enriched_spec: EnrichedSpec,
        changed_files: Dict[str, str],
        architecture: Optional[SystemArchitecture] = None,
        existing_symbols: Optional[str] = None,
        auth_contract: Optional[str] = None,
        prior_specs: Optional[Iterable[EnrichedSpec]] = None,
    ) -> CodeReviewReport:
        """Review ``changed_files`` against the EnrichedSpec.

        Returns a CodeReviewReport. Caller decides what to do with
        ``approved=False`` — typically dispatching ``Engineer.repair``
        with the report as seed context.
        """
        self._log(
            f"CodeReviewer: {milestone.name} "
            f"({len(changed_files)} file(s))"
        )

        if not changed_files:
            # Nothing to review — return an explicit empty pass with
            # zero confidence. Caller can decide whether to skip or
            # treat as a missed gate.
            return CodeReviewReport(
                milestone_name=milestone.name,
                approved=True,
                summary="(no changed files supplied — nothing to review)",
                confidence=0.0,
            )

        prior_jsons = [s.model_dump_json(indent=2) for s in (prior_specs or [])]

        user_prompt = build_review_prompt(
            milestone_name=milestone.name,
            enriched_spec_json=enriched_spec.model_dump_json(indent=2),
            changed_files=changed_files,
            architecture=architecture,
            existing_symbols=existing_symbols,
            auth_contract=auth_contract,
            prior_specs=prior_jsons,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=CODE_REVIEWER_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=CODE_REVIEW_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="CodeReviewer",
        )

        # Force milestone name to canonical (the LLM occasionally
        # restates it differently).
        raw["milestone_name"] = milestone.name

        # Lenient fallback — code review failure is side-channel
        # (drives REPAIR iters). On schema validation failure, return
        # a conservative "not approved, no findings, low confidence"
        # verdict. The milestone's existing repair-iter cap will
        # eventually accept and move on rather than loop forever.
        # Confidence=0.0 marks this as a fallback so future tooling
        # can distinguish from a real "0 findings" clean review.
        try:
            report = CodeReviewReport.model_validate(raw)
        except Exception as e:
            self._log(
                f"CodeReviewer: schema validation failed ({e}) — "
                f"returning conservative not-approved fallback"
            )
            return CodeReviewReport(
                milestone_name=milestone.name,
                approved=False,
                summary=(
                    "auto-fallback: code review LLM output failed schema "
                    "validation; conservative not-approved verdict so "
                    "the next repair iter (or max-iter cap) decides "
                    "whether to proceed."
                ),
                confidence=0.0,
                recommendations=[
                    "CodeReviewer LLM returned malformed JSON after "
                    "all retries; re-run review or escalate model "
                    "tier if this persists."
                ],
            )

        # Force approved=False if any critical finding exists. The LLM
        # occasionally rubber-stamps despite flagging crits.
        if report.has_critical and report.approved:
            self._log(
                "CodeReviewer: overriding approval — "
                f"{len(report.critical_findings)} critical finding(s)"
            )
            report.approved = False

        self._log(
            f"CodeReviewer: approved={report.approved}, "
            f"findings={report.total_findings} "
            f"({len(report.critical_findings)} critical)"
        )
        return report

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)
