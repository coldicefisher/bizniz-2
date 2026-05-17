"""Per-story score aggregation for the Storybook UX loop — Phase 5.

Rolls per-story eval results up to a Storybook-level score that
mirrors the per-route ``compute_app_score`` shape used elsewhere
in ProUXDesigner.

Why a separate module: the unit changes (primitives vs routes),
the bottleneck terminology changes (laggards by story_id vs by
route), and not_reviewable handling is different (capture-mismatch
is per-route only). Keeping this isolated makes both surfaces
testable in isolation.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from pydantic import BaseModel, Field

from bizniz.ux_designer.storybook_eval import StoryEvalResult


# ── Output schema ────────────────────────────────────────────────


class StorybookScore(BaseModel):
    """Storybook-level rollup of per-story scores."""
    mean: Optional[float] = None
    min: Optional[int] = None
    min_story_id: Optional[str] = None
    min_title: Optional[str] = None
    passing: int = 0
    failing_story_ids: List[str] = Field(default_factory=list)
    not_evaluable_story_ids: List[str] = Field(default_factory=list)
    covered: int = 0
    total: int = 0


# ── Aggregator ───────────────────────────────────────────────────


def compute_storybook_score(
    eval_results: Iterable[StoryEvalResult],
    acceptable_score: int = 7,
) -> StorybookScore:
    """Aggregate per-story eval results into a Storybook score.

    A story is **not_evaluable** when ``overall_score == 0`` and the
    summary indicates the eval couldn't run (no capture, no vision
    JSON). These are excluded from mean / min / passing-count
    calculations so they don't drag the metric down for reasons
    unrelated to the primitive's quality. They're surfaced via
    ``not_evaluable_story_ids`` so the operator can act on them.

    Returns ``StorybookScore`` with:

    - ``mean`` — average ``overall_score`` across evaluable stories,
      rounded to 2 decimals; ``None`` if no evaluable stories.
    - ``min``, ``min_story_id``, ``min_title`` — the laggard primitive
      (the one to fix next).
    - ``passing`` — count of stories at or above ``acceptable_score``.
    - ``failing_story_ids`` — laggards sorted ascending by score.
    - ``not_evaluable_story_ids`` — capture / eval failures.
    - ``covered`` — number of evaluable stories.
    - ``total`` — total stories considered (incl. not_evaluable).
    """
    results = list(eval_results)
    total = len(results)
    if total == 0:
        return StorybookScore()

    evaluable: List[StoryEvalResult] = []
    not_evaluable: List[str] = []
    for r in results:
        if _is_not_evaluable(r):
            not_evaluable.append(r.story_id)
        else:
            evaluable.append(r)

    if not evaluable:
        return StorybookScore(
            not_evaluable_story_ids=not_evaluable,
            total=total,
        )

    mean = sum(r.overall_score for r in evaluable) / len(evaluable)
    laggard = min(evaluable, key=lambda r: r.overall_score)
    passing = sum(1 for r in evaluable if r.overall_score >= acceptable_score)
    failing = sorted(
        (r for r in evaluable if r.overall_score < acceptable_score),
        key=lambda r: r.overall_score,
    )
    return StorybookScore(
        mean=round(mean, 2),
        min=laggard.overall_score,
        min_story_id=laggard.story_id,
        min_title=laggard.title,
        passing=passing,
        failing_story_ids=[r.story_id for r in failing],
        not_evaluable_story_ids=not_evaluable,
        covered=len(evaluable),
        total=total,
    )


def _is_not_evaluable(r: StoryEvalResult) -> bool:
    """A story is not_evaluable when the eval pipeline couldn't
    actually score it — distinguished from "scored 0 because the
    primitive is bad" by the summary text."""
    if r.overall_score > 0:
        return False
    needles = (
        "no capture available",
        "no parseable JSON",
        "no parseable json",
        "vision call returned no parseable",
    )
    summary = (r.summary or "").lower()
    return any(n.lower() in summary for n in needles)
