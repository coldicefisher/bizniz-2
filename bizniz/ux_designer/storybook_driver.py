"""End-to-end Storybook UX loop — Phase 6 orchestration.

Composes Phases 1-5 (discovery → server → capture → eval → fix →
re-eval → score) into a single ``StorybookDriver.run()`` call.
ProUXDesigner invokes this AFTER design_lock and BEFORE the
per-route loop; both loops run during a UX phase, and the run
report carries Storybook score alongside the per-route score.

The driver is the integration boundary — every step is delegated
to a Phase 1-5 module via constructor injection, so tests can
swap any layer for a fake without touching this driver.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.ux_designer.storybook_capture import (
    StoryCaptureResult, capture_stories,
)
from bizniz.ux_designer.storybook_discovery import (
    StoryCatalog, StoryEntry, discover_stories,
)
from bizniz.ux_designer.storybook_eval import (
    StoryEvalResult, StoryEvaluator,
)
from bizniz.ux_designer.storybook_fix import (
    StoryFixDispatcher, StoryFixResult,
)
from bizniz.ux_designer.storybook_score import (
    StorybookScore, compute_storybook_score,
)
from bizniz.ux_designer.storybook_server import (
    StorybookServer, storybook_server,
)


# ── Output schema ────────────────────────────────────────────────


class StoryRunRecord(BaseModel):
    """Per-story trace through the loop: every iter's eval + fix."""
    story_id: str
    name: str
    title: str
    iterations: List[StoryEvalResult] = Field(default_factory=list)
    fixes_applied: List[StoryFixResult] = Field(default_factory=list)
    final_score: Optional[int] = None
    final_stop_reason: str = ""


class StorybookRunResult(BaseModel):
    """End-of-loop summary."""
    skipped_reason: Optional[str] = None  # None = ran, else "no stories", etc.
    catalog_size: int = 0
    story_records: List[StoryRunRecord] = Field(default_factory=list)
    score: StorybookScore = Field(default_factory=StorybookScore)
    duration_s: float = 0.0
    server_base_url: Optional[str] = None
    server_started: bool = False


# ── Driver ───────────────────────────────────────────────────────


class StorybookDriver:
    """Drives the per-story UX loop end-to-end.

    All collaborators are constructor-injected so the driver is
    testable in isolation: ``server_factory``, ``capture_fn``,
    ``evaluator``, ``fix_dispatcher`` are all swappable.
    """

    def __init__(
        self,
        evaluator: StoryEvaluator,
        fix_dispatcher: StoryFixDispatcher,
        max_iterations: int = 3,
        acceptable_score: int = 7,
        on_status: Optional[Callable[[str], None]] = None,
        # Injection points (defaults use the production functions).
        discover_fn: Optional[Callable[[Path], StoryCatalog]] = None,
        server_factory: Optional[Callable[..., StorybookServer]] = None,
        capture_fn: Optional[Callable[..., List[StoryCaptureResult]]] = None,
    ) -> None:
        self._evaluator = evaluator
        self._fix_dispatcher = fix_dispatcher
        self._max_iterations = max_iterations
        self._acceptable_score = acceptable_score
        self._on_status = on_status
        self._discover_fn = discover_fn or discover_stories
        self._server_factory = server_factory or StorybookServer
        self._capture_fn = capture_fn or capture_stories

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    # ── Public entry point ───────────────────────────────────────

    def run(
        self,
        frontend_root: Path,
        screenshots_dir: Path,
        design_lock_json: Optional[str] = None,
    ) -> StorybookRunResult:
        """Run the full Storybook UX loop.

        - Discover stories under ``frontend_root``
        - Spin up Storybook dev server
        - Capture every story
        - For each story: eval → fix → re-eval up to ``max_iterations``
        - Aggregate scores → ``StorybookScore``

        Returns ``StorybookRunResult`` with a per-story trace. Never
        raises — failures at any layer surface as ``skipped_reason``
        or as per-story records with low scores.
        """
        t0 = time.time()

        # ── 1. Discovery ─────────────────────────────────────────
        catalog = self._discover_fn(frontend_root)
        if catalog.story_count == 0:
            reason = "no stories found"
            if catalog.discovery_warnings:
                reason = (
                    f"no stories found ({len(catalog.discovery_warnings)} "
                    f"parse warning(s))"
                )
            self._log(f"StorybookDriver: {reason} — skipping")
            return StorybookRunResult(
                skipped_reason=reason,
                catalog_size=0,
                duration_s=time.time() - t0,
            )

        self._log(
            f"StorybookDriver: discovered {catalog.story_count} story "
            f"(across {len(catalog.unique_titles)} component(s))"
        )

        # ── 2-5. Server up → capture → loop → score ──────────────
        # All the in-server work happens inside the context manager
        # so the server tears down even if a later step throws.
        records: List[StoryRunRecord] = []
        server_started = False
        server_base_url: Optional[str] = None
        try:
            server = self._server_factory(
                frontend_root=frontend_root,
                on_status=self._on_status,
            )
            with server:
                server_started = True
                server_base_url = server.base_url
                self._log(
                    f"StorybookDriver: server up at {server_base_url}"
                )
                records = self._run_loop(
                    catalog=catalog,
                    server_base_url=server_base_url,
                    screenshots_dir=screenshots_dir,
                    frontend_root=frontend_root,
                    design_lock_json=design_lock_json,
                )
        except Exception as e:
            # Server startup failure or unhandled exception in the
            # loop — log and return what we have rather than tank
            # the whole UX phase.
            self._log(
                f"StorybookDriver: aborted — "
                f"{type(e).__name__}: {e}"
            )
            return StorybookRunResult(
                skipped_reason=(
                    f"storybook driver aborted: "
                    f"{type(e).__name__}: {e}"
                ),
                catalog_size=catalog.story_count,
                story_records=records,
                duration_s=time.time() - t0,
                server_base_url=server_base_url,
                server_started=server_started,
            )

        score = self._aggregate_score(records)
        self._log(
            f"StorybookDriver: done — score mean={score.mean}, "
            f"passing={score.passing}/{score.covered}"
        )
        return StorybookRunResult(
            catalog_size=catalog.story_count,
            story_records=records,
            score=score,
            duration_s=time.time() - t0,
            server_base_url=server_base_url,
            server_started=server_started,
        )

    # ── Per-catalog loop ─────────────────────────────────────────

    def _run_loop(
        self,
        catalog: StoryCatalog,
        server_base_url: str,
        screenshots_dir: Path,
        frontend_root: Path,
        design_lock_json: Optional[str],
    ) -> List[StoryRunRecord]:
        # Initial capture of every story in one pass.
        captures = self._capture_fn(
            catalog=catalog,
            storybook_base_url=server_base_url,
            output_dir=screenshots_dir,
            on_status=self._on_status,
        )
        by_id = {c.story_id: c for c in captures}

        records: List[StoryRunRecord] = []
        for entry in catalog.stories:
            record = self._run_one_story(
                entry=entry,
                capture=by_id.get(entry.story_id),
                server_base_url=server_base_url,
                screenshots_dir=screenshots_dir,
                frontend_root=frontend_root,
                design_lock_json=design_lock_json,
            )
            records.append(record)
        return records

    def _run_one_story(
        self,
        entry: StoryEntry,
        capture: Optional[StoryCaptureResult],
        server_base_url: str,
        screenshots_dir: Path,
        frontend_root: Path,
        design_lock_json: Optional[str],
    ) -> StoryRunRecord:
        record = StoryRunRecord(
            story_id=entry.story_id,
            name=entry.name,
            title=entry.title,
        )
        if capture is None:
            # The capture step didn't return anything for this story.
            # Mark not-evaluable and move on.
            record.iterations.append(StoryEvalResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                summary="no capture returned by sidecar",
                stop_recommendation="stop",
            ))
            record.final_stop_reason = "no_capture"
            return record

        current_capture = capture
        for iteration in range(1, self._max_iterations + 1):
            eval_result = self._evaluator.evaluate(
                capture=current_capture,
                entry=entry,
                design_lock_json=design_lock_json,
                iteration=iteration,
            )
            record.iterations.append(eval_result)
            record.final_score = eval_result.overall_score

            if eval_result.stop_recommendation == "stop":
                record.final_stop_reason = (
                    "score_met_threshold"
                    if eval_result.overall_score >= self._acceptable_score
                    else "vision_said_stop"
                )
                break

            # Dispatch fix.
            fix_result = self._fix_dispatcher.dispatch(
                entry=entry,
                eval_result=eval_result,
                frontend_root=frontend_root,
                design_lock_json=design_lock_json,
            )
            record.fixes_applied.append(fix_result)

            if fix_result.status != "applied":
                # No edits applied — re-running eval would just
                # produce the same result. Stop.
                record.final_stop_reason = f"fix_{fix_result.status}"
                break

            if iteration == self._max_iterations:
                record.final_stop_reason = "max_iterations"
                break

            # Re-capture this single story for the next iteration.
            recap = self._capture_fn(
                catalog=_single_story_catalog(entry),
                storybook_base_url=server_base_url,
                output_dir=screenshots_dir,
                on_status=self._on_status,
            )
            current_capture = (
                recap[0]
                if recap and recap[0].success
                else current_capture
            )
        return record

    def _aggregate_score(
        self, records: List[StoryRunRecord],
    ) -> StorybookScore:
        # Take the LAST eval per story as the final score input.
        finals: List[StoryEvalResult] = []
        for r in records:
            if r.iterations:
                finals.append(r.iterations[-1])
        return compute_storybook_score(
            finals, acceptable_score=self._acceptable_score,
        )


def _single_story_catalog(entry: StoryEntry) -> StoryCatalog:
    """Wrap a single story in a one-entry catalog (for re-capture)."""
    return StoryCatalog(
        frontend_root=entry.stories_file.parent,
        stories=[entry],
    )
