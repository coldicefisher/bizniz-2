"""Generic multi-tier escalation primitive.

A "tier" is a (model, attempts) pair. The escalation loop runs each
tier until either:
- The attempt succeeds (escalation completes)
- The tier exhausts its attempts (advance to next tier)
- All tiers exhaust (escalation fails)

Used by:
- ``SmokeRecovery`` (item 7C — extending one-shot to multi-tier)
- ``RefactorTestPhase`` (item 7A — post-refactor regression repair)
- Future agents that need tier-based escalation

Existing ``DebuggerTierSpec`` + ``repair_integration_failure`` in
``integration/debug_loop.py`` predate this primitive and are
preserved for legacy callers. New code should prefer this module
for its smaller surface + clearer semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, List, Optional, TypeVar

T = TypeVar("T")


@dataclass
class TierSpec(Generic[T]):
    """One tier in the escalation chain.

    ``label`` is the human-readable identifier (model name, agent
    name) used in logs. ``attempts`` is the per-tier retry budget.
    ``factory`` is a no-arg callable returning a fresh agent for
    this tier — the loop calls it once per tier entry, not per
    attempt, so within-tier retries reuse the same agent instance.
    """
    label: str
    attempts: int
    factory: Callable[[], T]

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(
                f"TierSpec {self.label!r}: attempts must be >= 1, "
                f"got {self.attempts}"
            )


@dataclass
class TierAttemptResult:
    """Outcome of one attempt at one tier."""
    tier_label: str
    tier_index: int
    attempt_index: int   # 1-based within tier
    succeeded: bool
    output: str = ""     # Whatever the attempt produced — used by the
                         # loop for logging + the next attempt's context.


@dataclass
class EscalationResult:
    """End-of-escalation summary."""
    succeeded: bool = False
    final_tier_label: Optional[str] = None
    final_tier_index: Optional[int] = None
    total_attempts: int = 0
    attempts: List[TierAttemptResult] = field(default_factory=list)
    final_output: str = ""


def escalate(
    tiers: List[TierSpec[T]],
    attempt_fn: Callable[[T, int, int, Optional[str]], "AttemptOutcome"],
    on_status: Optional[Callable[[str], None]] = None,
) -> EscalationResult:
    """Run the escalation loop.

    Parameters
    ----------
    tiers:
        Ordered list of tiers. The loop runs tier 0's full
        ``attempts`` budget first; if every attempt fails, advances
        to tier 1, etc.
    attempt_fn:
        Callable that performs one attempt. Signature is
        ``(agent, tier_index, attempt_index, prior_output) ->
        AttemptOutcome``. ``prior_output`` is the output of the most
        recent failed attempt (across tiers), so an attempt can use
        prior failures' messages as context. ``None`` on the very
        first attempt.

        The function MUST NOT raise. Wrap any internal exceptions
        and return ``AttemptOutcome(succeeded=False, output=str(e))``.
    on_status:
        Optional logger called once per tier entry + once per attempt.

    Returns
    -------
    ``EscalationResult`` describing the full chain — succeeded flag,
    which tier succeeded (if any), how many attempts ran in total,
    the full per-attempt history.
    """
    if not tiers:
        raise ValueError("escalate: tiers list must be non-empty")
    result = EscalationResult()
    prior_output: Optional[str] = None

    for tier_index, tier in enumerate(tiers):
        if on_status is not None:
            try:
                on_status(
                    f"escalate: entering tier {tier_index} "
                    f"({tier.label!r}, {tier.attempts} attempt(s))"
                )
            except Exception:
                pass
        try:
            agent = tier.factory()
        except Exception as e:
            # Factory failed — record + try next tier rather than crash.
            record = TierAttemptResult(
                tier_label=tier.label, tier_index=tier_index,
                attempt_index=0, succeeded=False,
                output=f"tier factory raised: {type(e).__name__}: {e}",
            )
            result.attempts.append(record)
            result.total_attempts += 1
            prior_output = record.output
            continue

        for attempt_index in range(1, tier.attempts + 1):
            result.total_attempts += 1
            outcome = attempt_fn(
                agent, tier_index, attempt_index, prior_output,
            )
            record = TierAttemptResult(
                tier_label=tier.label,
                tier_index=tier_index,
                attempt_index=attempt_index,
                succeeded=outcome.succeeded,
                output=outcome.output,
            )
            result.attempts.append(record)
            prior_output = outcome.output

            if on_status is not None:
                try:
                    on_status(
                        f"escalate: tier {tier_index} "
                        f"attempt {attempt_index}/{tier.attempts} → "
                        f"{'PASS' if outcome.succeeded else 'fail'}"
                    )
                except Exception:
                    pass

            if outcome.succeeded:
                result.succeeded = True
                result.final_tier_label = tier.label
                result.final_tier_index = tier_index
                result.final_output = outcome.output
                return result

    # Every tier exhausted.
    result.final_output = prior_output or ""
    if tiers:
        result.final_tier_label = tiers[-1].label
        result.final_tier_index = len(tiers) - 1
    return result


@dataclass
class AttemptOutcome:
    """Return value from an ``attempt_fn``. Kept as a separate type
    so callers can construct it explicitly instead of returning
    bare tuples."""
    succeeded: bool
    output: str = ""
