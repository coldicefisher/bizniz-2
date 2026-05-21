"""QualityEngineer — single-call agent with two modes.

  - ``enrich(...)``  produces an ``EnrichedSpec`` for a milestone
  - ``review(...)``  produces a ``CoverageReport`` over the Engineer's tests

Both modes are single LLM round-trips (modulo retry). No tool loop, no
state between calls. The agent doesn't even hold per-call state — each
method is fully parameterized.

Bias firewall: ``review`` only accepts test files, never source. The
call signature itself enforces the firewall.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Iterable, List, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.lib.llm_utils import call_with_retry
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.prompts.enrich_prompt import (
    ENRICH_SCHEMA,
    ENRICH_SYSTEM_PROMPT,
    build_enrich_prompt,
    build_reenrich_prompt,
)
from bizniz.quality_engineer.prompts.patch_prompt import (
    PATCH_SCHEMA,
    PATCH_SYSTEM_PROMPT,
    build_patch_prompt,
)
from bizniz.quality_engineer.prompts.write_patches_prompt import (
    WRITE_PATCHES_SCHEMA,
    WRITE_PATCHES_SYSTEM_PROMPT,
    build_write_patches_prompt,
)
from bizniz.quality_engineer.prompts.write_tests_prompt import (
    WRITE_TESTS_SCHEMA,
    WRITE_TESTS_SYSTEM_PROMPT,
    build_write_tests_prompt,
)
from bizniz.quality_engineer.prompts.review_prompt import (
    REVIEW_SCHEMA,
    REVIEW_SYSTEM_PROMPT,
    build_review_prompt,
)
from bizniz.quality_engineer.types import (
    CoverageReport,
    EnrichedSpec,
    QEGeneratedPatch,
    QEGeneratedTest,
    QEPatchResult,
    QETestPatch,
    QEWritePatchesResult,
    QEWriteTestsResult,
    QualityEngineerError,
)


class QualityEngineer:
    """Single-call dual-mode agent: pre-flight enrich, post-flight review."""

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    # ── Public: enrich ────────────────────────────────────────────────

    def enrich(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        auth_contract: Optional[str] = None,
        prior_specs: Optional[Iterable[EnrichedSpec]] = None,
    ) -> EnrichedSpec:
        """Produce an EnrichedSpec for ``milestone``.

        Runs BEFORE the Engineer. Threaded into the Engineer's initial
        context so the Engineer plans against this spec.
        """
        self._log(f"QualityEngineer (enrich): {milestone.name}")

        prior_jsons = [s.model_dump_json(indent=2) for s in (prior_specs or [])]

        user_prompt = build_enrich_prompt(
            milestone_name=milestone.name,
            problem_slice=milestone.problem_slice,
            use_cases=milestone.use_cases,
            success_criteria=milestone.success_criteria,
            architecture_summary=_summarize_architecture(architecture),
            auth_contract=auth_contract,
            prior_contracts=prior_jsons,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=ENRICH_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=ENRICH_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="QualityEngineer.enrich",
        )

        # Force the milestone name to match what we asked about — the
        # LLM occasionally restates it differently.
        raw["milestone_name"] = milestone.name

        try:
            spec = EnrichedSpec.model_validate(raw)
        except Exception as e:
            raise QualityEngineerError(
                f"enrich: LLM output failed schema validation: {e}"
            ) from e

        if not spec.capabilities:
            raise QualityEngineerError(
                f"enrich: returned zero capabilities for milestone "
                f"{milestone.name!r}. Refusing to ship an empty spec."
            )

        # Guard against duplicate capability ids — would silently break
        # the post-flight coverage_by_capability mapping.
        seen: set = set()
        dups: List[str] = []
        for c in spec.capabilities:
            if c.id in seen:
                dups.append(c.id)
            seen.add(c.id)
        if dups:
            raise QualityEngineerError(
                f"enrich: duplicate capability ids: {dups}"
            )

        self._log(
            f"QualityEngineer (enrich): {len(spec.capabilities)} capabilities, "
            f"confidence={spec.confidence:.2f}"
        )
        return spec

    def re_enrich(
        self,
        milestone: Milestone,
        prior_spec: EnrichedSpec,
        architecture: SystemArchitecture,
        auth_contract: Optional[str] = None,
        prior_specs: Optional[Iterable[EnrichedSpec]] = None,
    ) -> EnrichedSpec:
        """Second-pass enrich when the first pass returned low
        confidence. The model sees its own prior output + an
        explicit "name the ambiguities and either resolve them or
        write TODOs" instruction. Returns a fresh EnrichedSpec;
        caller picks whichever has higher confidence.

        Threaded as part of the load-bearing confidence-signal work
        (roadmap item 1). The prior single-pass behavior had QE
        self-rate confidence as descriptive telemetry; this method
        is the action the harness takes when confidence is in the
        re-enrich band (default 0.4-0.6).
        """
        self._log(
            f"QualityEngineer (re-enrich): {milestone.name} "
            f"(prior confidence={prior_spec.confidence:.2f})"
        )
        prior_jsons = [s.model_dump_json(indent=2) for s in (prior_specs or [])]
        user_prompt = build_reenrich_prompt(
            milestone_name=milestone.name,
            problem_slice=milestone.problem_slice,
            use_cases=milestone.use_cases,
            success_criteria=milestone.success_criteria,
            architecture_summary=_summarize_architecture(architecture),
            auth_contract=auth_contract,
            prior_contracts=prior_jsons,
            prior_low_confidence_spec_json=prior_spec.model_dump_json(indent=2),
            prior_confidence=prior_spec.confidence,
        )
        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=ENRICH_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=ENRICH_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="QualityEngineer.re_enrich",
        )
        raw["milestone_name"] = milestone.name
        # Lenient fallbacks — re_enrich is a side-channel (called only
        # at confidence 0.4-0.6). If the improvement attempt fails, the
        # original low-confidence spec is still usable; the harness
        # can still gate on it via the existing confidence threshold.
        # Halting the milestone for a failed *optional* improvement
        # would defeat the purpose.
        try:
            spec = EnrichedSpec.model_validate(raw)
        except Exception as e:
            self._log(
                f"QualityEngineer (re-enrich): schema validation failed "
                f"({e}) — falling back to prior spec "
                f"(confidence={prior_spec.confidence:.2f})"
            )
            return prior_spec
        if not spec.capabilities:
            self._log(
                f"QualityEngineer (re-enrich): returned zero capabilities "
                f"for milestone {milestone.name!r} — falling back to prior "
                f"spec (confidence={prior_spec.confidence:.2f})"
            )
            return prior_spec
        self._log(
            f"QualityEngineer (re-enrich): {len(spec.capabilities)} capabilities, "
            f"confidence={spec.confidence:.2f} "
            f"({'improved' if spec.confidence > prior_spec.confidence else 'unchanged'} "
            f"vs prior {prior_spec.confidence:.2f})"
        )
        return spec

    # ── Public: review ────────────────────────────────────────────────

    def review(
        self,
        milestone: Milestone,
        enriched_spec: EnrichedSpec,
        engineer_plan: dict,
        test_files: Dict[str, str],
        auth_contract: Optional[str] = None,
    ) -> CoverageReport:
        """Verify that ``test_files`` cover ``enriched_spec``.

        Bias firewall: only test files are accepted — never source. The
        signature of this method is the firewall; do not add a
        ``source_files`` parameter.

        ``engineer_plan`` is the Engineer's submit_plan payload as a
        plain dict (issues, spec_refs, etc.). Helps the reviewer match
        tests to capabilities via the issue→spec_ref→capability_id chain.
        """
        self._log(
            f"QualityEngineer (review): {milestone.name} "
            f"({len(test_files)} test file(s))"
        )

        user_prompt = build_review_prompt(
            milestone_name=milestone.name,
            enriched_spec_json=enriched_spec.model_dump_json(indent=2),
            engineer_plan_json=json.dumps(engineer_plan, indent=2),
            test_files=test_files,
            auth_contract=auth_contract,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=REVIEW_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=REVIEW_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="QualityEngineer.review",
        )

        # Reconcile milestone name (cf. enrich).
        raw["milestone_name"] = milestone.name

        # Lenient fallback — review failure is side-channel (drives
        # REPAIR iters). On schema validation failure, return a
        # conservative "not approved, no findings, low confidence"
        # verdict. The milestone's existing repair-iter cap will
        # eventually accept and move on rather than loop forever.
        # Confidence=0.0 marks this as a fallback so future tooling
        # can distinguish from a real "0 findings" review.
        try:
            report = CoverageReport.model_validate(raw)
        except Exception as e:
            self._log(
                f"QualityEngineer (review): schema validation failed "
                f"({e}) — returning conservative not-approved fallback"
            )
            return CoverageReport(
                milestone_name=milestone.name,
                approved=False,
                summary=(
                    "auto-fallback: review LLM output failed schema "
                    "validation; conservative not-approved verdict so "
                    "the next repair iter (or max-iter cap) decides "
                    "whether to proceed."
                ),
                confidence=0.0,
                recommendations=[
                    "QualityEngineer review LLM returned malformed JSON "
                    "after all retries; re-run review or escalate model "
                    "tier if this persists."
                ],
            )

        # Sanity: every coverage_by_capability key should be a real
        # capability id from the spec. Trim unknown keys with a warning
        # rather than fail — sometimes the LLM hallucinates a related
        # name and we'd rather demote-then-flag than reject the report.
        valid_ids = {c.id for c in enriched_spec.capabilities}
        unknown = [k for k in report.coverage_by_capability if k not in valid_ids]
        if unknown:
            self._log(
                f"QualityEngineer (review): dropping {len(unknown)} unknown "
                f"capability id(s) from coverage map: {unknown}"
            )
            for k in unknown:
                report.coverage_by_capability.pop(k, None)

        # Add explicit "missing" for any capability the LLM forgot to
        # rate. Otherwise approval looks at an incomplete map.
        for c in enriched_spec.capabilities:
            report.coverage_by_capability.setdefault(c.id, "missing")

        # If anything is "missing" or critical scenarios are flagged,
        # force approved=false. The LLM occasionally rubber-stamps.
        has_missing = any(
            v == "missing" for v in report.coverage_by_capability.values()
        )
        has_critical_gap = any(
            ms.priority == "critical" for ms in report.missing_scenarios
        )
        if (has_missing or has_critical_gap) and report.approved:
            self._log(
                "QualityEngineer (review): overriding approval — "
                "found missing capabilities or critical gaps"
            )
            report.approved = False

        self._log(
            f"QualityEngineer (review): approved={report.approved}, "
            f"covered={report.covered_count}/{report.total_count}, "
            f"gaps={len(report.missing_scenarios)}"
        )
        return report

    # ── Public: write_tests ───────────────────────────────────────────

    def write_tests(
        self,
        *,
        coverage: CoverageReport,
        enriched_spec: EnrichedSpec,
        architecture_summary: str,
        compose_path: str,
        test_files: Dict[str, str],
        auth_contract: Optional[str] = None,
    ) -> QEWriteTestsResult:
        """One-shot: write test files for every missing scenario.

        Tests are the ground truth for the agentic debugger. Highest
        scope first (E2E → integration → unit). Always Docker.
        """
        if not coverage.missing_scenarios:
            self._log("QualityEngineer (write_tests): no missing scenarios — skipping")
            return QEWriteTestsResult()

        self._log(
            f"QualityEngineer (write_tests): {len(coverage.missing_scenarios)} "
            f"scenario(s) to cover"
        )

        missing_dicts = [ms.model_dump() for ms in coverage.missing_scenarios]
        user_prompt = build_write_tests_prompt(
            milestone_name=coverage.milestone_name,
            enriched_spec_json=enriched_spec.model_dump_json(indent=2),
            missing_scenarios=missing_dicts,
            architecture_summary=architecture_summary,
            compose_path=compose_path,
            test_files=test_files,
            auth_contract=auth_contract,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=WRITE_TESTS_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=WRITE_TESTS_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="QualityEngineer.write_tests",
        )

        items = raw.get("tests") or []
        tests: List[QEGeneratedTest] = []
        for it in items:
            try:
                tests.append(QEGeneratedTest(**it))
            except Exception as e:
                self._log(
                    f"QualityEngineer (write_tests): skipping malformed entry: "
                    f"{type(e).__name__}: {e}"
                )

        self._log(
            f"QualityEngineer (write_tests): {len(tests)} test file(s) — "
            f"e2e={sum(1 for t in tests if t.scope=='e2e')}, "
            f"integration={sum(1 for t in tests if t.scope=='integration')}, "
            f"unit={sum(1 for t in tests if t.scope=='unit')}"
        )
        return QEWriteTestsResult(tests=tests)

    # ── Public: write_patches ─────────────────────────────────────────

    def write_patches(
        self,
        *,
        coverage: CoverageReport,
        enriched_spec: EnrichedSpec,
        architecture_summary: str,
        source_files: Dict[str, str],
        auth_contract: Optional[str] = None,
    ) -> QEWritePatchesResult:
        """One-shot: write source code patches for missing scenarios.

        Best-effort. The agentic debugger handles convergence.
        Bias firewall RELAXED — sees source files.
        """
        if not coverage.missing_scenarios:
            self._log("QualityEngineer (write_patches): no missing scenarios — skipping")
            return QEWritePatchesResult()

        self._log(
            f"QualityEngineer (write_patches): writing patches for "
            f"{len(coverage.missing_scenarios)} scenario(s)"
        )

        missing_dicts = [ms.model_dump() for ms in coverage.missing_scenarios]
        user_prompt = build_write_patches_prompt(
            milestone_name=coverage.milestone_name,
            enriched_spec_json=enriched_spec.model_dump_json(indent=2),
            missing_scenarios=missing_dicts,
            architecture_summary=architecture_summary,
            source_files=source_files,
            auth_contract=auth_contract,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=WRITE_PATCHES_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=WRITE_PATCHES_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="QualityEngineer.write_patches",
        )

        items = raw.get("patches") or []
        patches: List[QEGeneratedPatch] = []
        for it in items:
            try:
                patches.append(QEGeneratedPatch(**it))
            except Exception as e:
                self._log(
                    f"QualityEngineer (write_patches): skipping malformed entry: "
                    f"{type(e).__name__}: {e}"
                )

        self._log(f"QualityEngineer (write_patches): {len(patches)} patch(es)")
        return QEWritePatchesResult(patches=patches)

    # ── Public: patch ─────────────────────────────────────────────────

    def patch(
        self,
        *,
        coverage: CoverageReport,
        enriched_spec: EnrichedSpec,
        test_files: Dict[str, str],
        auth_contract: Optional[str] = None,
    ) -> QEPatchResult:
        """Emit test file patches for the missing scenarios in ``coverage``.

        Called immediately after review() in the v5 hybrid loop. Patches
        that validate clean auto-resolve their findings before the
        CanonicalReport is frozen, collapsing the most common repair
        iteration (pure test-gap fixes) into the review pass itself.

        Returns an empty QEPatchResult when there is nothing safe to
        patch — callers must tolerate an empty patch list gracefully.
        """
        if not coverage.missing_scenarios:
            return QEPatchResult()

        self._log(
            f"QualityEngineer (patch): {len(coverage.missing_scenarios)} "
            f"missing scenario(s) — attempting inline patches"
        )

        missing_dicts = [
            ms.model_dump() for ms in coverage.missing_scenarios
        ]
        user_prompt = build_patch_prompt(
            milestone_name=coverage.milestone_name,
            enriched_spec_json=enriched_spec.model_dump_json(indent=2),
            missing_scenarios=missing_dicts,
            test_files=test_files,
            auth_contract=auth_contract,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=PATCH_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=PATCH_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="QualityEngineer.patch",
        )

        items = raw.get("patches") or []
        patches: List[QETestPatch] = []
        for it in items:
            try:
                patches.append(QETestPatch(**it))
            except Exception as e:
                self._log(
                    f"QualityEngineer (patch): skipping malformed patch "
                    f"entry: {type(e).__name__}: {e}"
                )

        self._log(
            f"QualityEngineer (patch): emitted {len(patches)} patch(es) "
            f"covering {sum(len(p.capability_ids) for p in patches)} capability id(s)"
        )
        return QEPatchResult(patches=patches)

    # ── Internals ─────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)


# ── Helpers ────────────────────────────────────────────────────────────


def _summarize_architecture(arch: SystemArchitecture) -> str:
    """Compact text summary of a SystemArchitecture for the QE's prompt.

    The full architecture model has a lot of fields — we only need the
    parts that scope the spec: service names, frameworks, languages,
    and how they connect.
    """
    lines = [f"Project: {arch.project_name} ({arch.project_slug})"]
    if arch.description:
        lines.append(f"Description: {arch.description}")
    lines.append("\nServices:")
    for s in arch.services:
        deps = ", ".join(s.depends_on) if s.depends_on else "—"
        lines.append(
            f"  - {s.name} ({s.service_type}/{s.framework}, {s.language}, "
            f"port {s.port}, depends_on: {deps}): {s.description}"
        )
    return "\n".join(lines)
