"""Per-candidate refactor decision gate (D19 / v3 refactorer Step 1).

For every refactor candidate (anti-pattern finding, CPD duplicate,
or misplaced-business-logic finding) the v3 pipeline asks an agent:

    Is this candidate actually worth refactoring?

The gate is a cheap, **single-shot** classifier — no tools, no
discovery, just the candidate's context + a yes/no rationale. It
runs BEFORE the planner/executor touch the codebase, so we save the
expensive extraction work on candidates that aren't worth doing.

**Why a gate at all (vs. just "extract everything CPD finds"):**

- CPD flags verbatim 50-token duplicates. Many of those are
  boilerplate that's HEALTHIER duplicated than abstracted
  (pydantic models, FastAPI route signatures).
- Anti-pattern findings can be false positives in context. A
  hardcoded `"localhost:5432"` in a docker-compose file is fine;
  in production code it's a bug. The gate reads the context.
- The misplacement scanner produces candidates the agent THINKS
  are wrong-layer. Sometimes they're idiomatic enough that
  extracting would over-engineer.

Decisions are **logged with rationale** — operator (and future
"brain" knob-tweaker) can audit which candidates got rejected and
why.
"""
from __future__ import annotations

import json
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field


CandidateKind = Literal["anti_pattern", "cpd_duplicate", "misplaced_logic"]


class CandidateContext(BaseModel):
    """The bundle of info handed to the gate for ONE candidate.

    Shape is intentionally generic — the same gate handles
    candidates from all three signal sources.
    """
    kind: CandidateKind = Field(
        ...,
        description=(
            "Source of the candidate. Sets the gate's framing — "
            "e.g. anti_pattern asks 'is this really wrong?', "
            "cpd_duplicate asks 'is the duplication harmful?', "
            "misplaced_logic asks 'is this really business logic in "
            "the wrong layer?'"
        ),
    )
    summary: str = Field(
        ...,
        description="One-line description of the candidate.",
    )
    file_path: str = Field(..., description="Where the candidate lives.")
    line_range: Optional[tuple] = Field(
        default=None,
        description="(start, end) line numbers (1-based, inclusive).",
    )
    snippet: str = Field(
        default="",
        description="Code snippet (the candidate's actual content).",
    )
    extra: dict = Field(
        default_factory=dict,
        description=(
            "Source-specific extras — e.g. cpd_duplicate carries "
            "occurrence_count, misplaced_logic carries suggested_core_module."
        ),
    )


class GateDecision(BaseModel):
    """The gate's verdict for one candidate."""
    refactor: bool = Field(..., description="Yes/no — should we extract this?")
    rationale: str = Field(
        ...,
        description="One-paragraph explanation, kept for audit.",
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description=(
            "Gate's self-rated confidence in its decision. Low "
            "confidence on a YES suggests bumping to a higher-tier "
            "model; low confidence on a NO suggests escalation to "
            "human review."
        ),
    )


# ── Schema (for response_format-aware clients) ───────────────────


GATE_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["refactor", "rationale"],
    "properties": {
        "refactor": {"type": "boolean"},
        "rationale": {"type": "string", "minLength": 1},
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
    "additionalProperties": False,
}


_SYSTEM_PROMPT = """\
You are a refactor decision gate for the bizniz pipeline.

A deterministic scanner (or another agent) found a refactor
candidate. Your job is to decide whether the bizniz refactorer
should actually do the extraction, or skip it.

You are NOT doing the extraction. You're only deciding.

Guidance per candidate kind:

  - **anti_pattern** — A deterministic regex/AST scanner matched a
    known bad pattern. Decide: is this a true positive worth
    fixing? Or context where the pattern is OK (e.g. a hardcoded
    URL in a docker-compose template, a deliberately duplicated
    type alias)? YES if the pattern is genuinely bad here.

  - **cpd_duplicate** — Two or more places have ~identical token
    sequences. Decide: is the duplication harmful enough to
    extract? Boilerplate-shaped duplication (Pydantic field
    declarations, FastAPI route signatures, simple data classes)
    is usually HEALTHIER duplicated than abstracted into a shared
    helper. Real business logic appearing in 3+ places is worth
    extracting. YES when the duplication is real business logic
    and consolidation would meaningfully reduce future drift.

  - **misplaced_logic** — Agent scan thinks this code is in the
    wrong layer (e.g. business rules embedded in an API route
    handler that should be a service-layer function). Decide: is
    the misplacement real, or is the code idiomatic for its
    layer? YES when extracting to core/python/ would meaningfully
    improve testability or reuse.

What to consider:

  1. **Risk** — would extraction break other consumers? Are the
     call sites complex enough that the new abstraction needs
     careful design?
  2. **Value** — how many consumers would benefit? Is the
     abstraction obvious or speculative?
  3. **Cost** — would the resulting code be CLEARER than what's
     there now? Sometimes "shared helper" is just indirection
     without value.
  4. **Idiomaticity** — is the candidate's current location
     actually fine for its kind?

Respond with VALID JSON matching this shape:

    {
      "refactor": true | false,
      "rationale": "one-paragraph explanation",
      "confidence": 0.0-1.0
    }

Be honest in rationale. Don't hedge. If you're uncertain, say so
and set confidence accordingly — the harness will escalate or
surface for human review.
"""


_USER_TEMPLATE = """\
Refactor candidate ({kind})

**Summary:** {summary}
**Location:** {file_path}{line_block}

**Snippet:**
```
{snippet}
```
{extra_block}
Decide: should this be refactored? Return JSON per the schema.
"""


class DecisionGate:
    """Runs single-shot decisions per candidate.

    The LLM call shape is up to the caller — pass an ``llm_invoker``
    that returns the parsed JSON dict. Default implementation
    expects a plain text LLM that we then json.loads.

    The gate itself never raises — bad responses become
    conservative "skip with rationale: parse failed" decisions
    so the rest of the pipeline can proceed cleanly.
    """

    def __init__(
        self,
        llm_invoker: Callable[[str, str], str],
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._invoke = llm_invoker
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def decide(self, candidate: CandidateContext) -> GateDecision:
        """Return YES/NO decision for one candidate."""
        user_prompt = self._build_user_prompt(candidate)
        try:
            raw = self._invoke(_SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            self._log(
                f"DecisionGate: llm invoke raised {type(e).__name__}: {e}"
            )
            return GateDecision(
                refactor=False,
                rationale=(
                    f"gate failed to invoke LLM ({type(e).__name__}: {e}) "
                    f"— defaulting to skip"
                ),
                confidence=0.0,
            )
        return _parse_decision(raw, on_status=self._on_status)

    def decide_all(
        self,
        candidates: List[CandidateContext],
    ) -> List[GateDecision]:
        """Run ``decide`` over a list. Each decision is independent
        — a failed call on candidate N doesn't poison N+1."""
        return [self.decide(c) for c in candidates]

    @staticmethod
    def _build_user_prompt(candidate: CandidateContext) -> str:
        line_block = ""
        if candidate.line_range:
            line_block = (
                f" (lines {candidate.line_range[0]}-{candidate.line_range[1]})"
            )
        extra_block = ""
        if candidate.extra:
            extra_lines = "\n".join(
                f"- {k}: {v}" for k, v in candidate.extra.items()
            )
            extra_block = f"\n**Extra context:**\n{extra_lines}\n"
        return _USER_TEMPLATE.format(
            kind=candidate.kind,
            summary=candidate.summary,
            file_path=candidate.file_path,
            line_block=line_block,
            snippet=(candidate.snippet or "(no snippet)")[:2000],
            extra_block=extra_block,
        )


# ── Internals ────────────────────────────────────────────────────


def _parse_decision(
    raw_text: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> GateDecision:
    """Parse the LLM's response into a GateDecision. Defensive
    against extra prose around the JSON, bad shapes, etc."""
    text = (raw_text or "").strip()
    if not text:
        return _conservative_skip("empty LLM response")

    # Strip code fences if present.
    if text.startswith("```"):
        # Drop the first fence + optional ```json label.
        lines = text.splitlines()
        lines = lines[1:]
        # Drop trailing fence.
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try strict JSON first; fall back to substring extraction.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return _conservative_skip(f"no JSON object in response: {text[:200]}")
        try:
            data = json.loads(text[start: end + 1])
        except json.JSONDecodeError as e:
            return _conservative_skip(f"JSON parse failed: {e}")

    if not isinstance(data, dict):
        return _conservative_skip(f"response was {type(data).__name__}, not object")

    refactor = data.get("refactor")
    rationale = data.get("rationale") or "no rationale provided"
    confidence = data.get("confidence", 0.5)

    if not isinstance(refactor, bool):
        return _conservative_skip(
            f"'refactor' field was {type(refactor).__name__}, not bool"
        )
    try:
        confidence_f = float(confidence)
    except (TypeError, ValueError):
        confidence_f = 0.5
    confidence_f = max(0.0, min(1.0, confidence_f))

    return GateDecision(
        refactor=refactor,
        rationale=str(rationale)[:2000],
        confidence=confidence_f,
    )


def _conservative_skip(reason: str) -> GateDecision:
    """When parsing fails, default to NO with the failure reason in
    the rationale. Safer than letting a parse failure trigger an
    extraction we can't audit."""
    return GateDecision(
        refactor=False,
        rationale=f"defaulted to skip — {reason}",
        confidence=0.0,
    )
