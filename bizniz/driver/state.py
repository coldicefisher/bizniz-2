"""Per-run / per-milestone state with sub-phase tracking.

State lives at ``<project_root>/.bizniz/runs/<job_id>/`` as JSON
(was ``<project_root>/docs/runs/<job_id>/`` before 2026-05-16; the
new path keeps ``docs/`` reserved for human-readable engineering
docs). Readers fall back to the legacy path for existing projects
via ``bizniz/driver/runs_paths.resolve_runs_root``. Sub-phase
granularity means resume can pick up at the exact point a prior run
exited — e.g. if M1 finished `implement` but `review_initial` didn't
write, resume runs `review_initial` for M1 next, not the whole M1.

Phases per milestone (in order):

  enrich           QualityEngineer.enrich → EnrichedSpec
  implement        Engineer.implement → EngineerResult
  review_initial   QE.review + CodeReviewer.review (parallel call ok)
  repair_iter_0    Engineer.repair after first review (if needed)
  repair_iter_1    Engineer.repair after iter_0 review
  repair_iter_2    Engineer.repair after iter_1 review
  review_final     terminal review (after last repair, if any)
  integration_api  API integration tests for backend services
  integration_web  Web integration tests for frontend services
  done             milestone fully complete

Top-level (pre-milestone) phases:

  plan             Planner.plan → ProjectPlan
  architect        Architect.decompose → SystemArchitecture
  provision        Provisioner.provision → ProvisionResult
  auth             AuthAgent.configure → AuthAgentResult

The state module is concerned with persistence + ordering only — the
business logic in milestone_loop / pipeline decides what each phase
does.
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class SubPhase(str, Enum):
    """Per-milestone sub-phase identifiers.

    Order of declaration is also the canonical run order. ``DONE`` is
    terminal. The order is consulted by ``next_phase()`` for resume.
    """
    ENRICH = "enrich"
    IMPLEMENT = "implement"
    SMOKE = "smoke"
    REVIEW_INITIAL = "review_initial"
    REPAIR_ITER_0 = "repair_iter_0"
    REPAIR_ITER_1 = "repair_iter_1"
    REPAIR_ITER_2 = "repair_iter_2"
    REVIEW_FINAL = "review_final"
    INTEGRATION_API = "integration_api"
    INTEGRATION_WORKER = "integration_worker"
    INTEGRATION_WEB = "integration_web"
    # Post-integration phases. UX_REVIEW runs when the milestone
    # touched a frontend service; REFACTOR runs when the milestone
    # has ``refactor_after=True`` or is the final milestone.
    UX_REVIEW = "ux_review"
    REFACTOR = "refactor"
    # FINAL_TEST is the last gate before DONE — verifies the stack
    # is end-to-end shippable (no fixtures, no test data, just real
    # HTTP probes against the running services). Catches stack
    # damage from any prior phase (integration teardown, refactor
    # extracts that break imports, UX fixes that mis-wire a route).
    FINAL_TEST = "final_test"
    DONE = "done"


class TopPhase(str, Enum):
    """Top-level (pre-milestone) phase identifiers."""
    PLAN = "plan"
    ARCHITECT = "architect"
    PROVISION = "provision"
    AUTH = "auth"


_SUBPHASE_ORDER = list(SubPhase)


def next_subphase(current: Optional[SubPhase]) -> SubPhase:
    """Return the phase that should run after ``current``.

    ``None`` → first phase (ENRICH). ``DONE`` → DONE (no-op).
    """
    if current is None:
        return _SUBPHASE_ORDER[0]
    if current == SubPhase.DONE:
        return SubPhase.DONE
    idx = _SUBPHASE_ORDER.index(current)
    return _SUBPHASE_ORDER[idx + 1]


class MilestoneState:
    """JSON-backed per-milestone state.

    ``root`` points at ``<runs>/<job_id>/m<N>/``. Each completed sub-phase
    writes a `<phase>.json` artifact + records the phase in `status.json`.
    """

    def __init__(self, root: Path, milestone_index: int):
        self.root = root
        self.milestone_index = milestone_index
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def status_path(self) -> Path:
        return self.root / "status.json"

    def _read_status(self) -> Dict[str, Any]:
        if not self.status_path.exists():
            return {"completed": [], "current": None}
        try:
            return json.loads(self.status_path.read_text())
        except Exception:
            return {"completed": [], "current": None}

    def _write_status(self, status: Dict[str, Any]) -> None:
        self.status_path.write_text(json.dumps(status, indent=2, default=str))

    def completed_phases(self) -> List[SubPhase]:
        raw = self._read_status().get("completed") or []
        out: List[SubPhase] = []
        for r in raw:
            try:
                out.append(SubPhase(r))
            except Exception:
                continue
        return out

    def last_completed(self) -> Optional[SubPhase]:
        completed = self.completed_phases()
        if not completed:
            return None
        # Return the latest in declaration order (not chronological — phases
        # only progress forward).
        completed_set = set(completed)
        for ph in reversed(_SUBPHASE_ORDER):
            if ph in completed_set:
                return ph
        return None

    def is_done(self) -> bool:
        return SubPhase.DONE in self.completed_phases()

    def mark_phase(self, phase: SubPhase, payload: Optional[Any] = None) -> None:
        """Persist ``payload`` as ``<phase>.json`` and add ``phase`` to
        the completed list. Idempotent — re-marking same phase replaces
        artifact, doesn't duplicate in completed."""
        if payload is not None:
            self._write_artifact(phase, payload)
        status = self._read_status()
        completed = list(status.get("completed") or [])
        if phase.value not in completed:
            completed.append(phase.value)
        status["completed"] = completed
        status["current"] = phase.value
        status["updated_at"] = datetime.utcnow().isoformat()
        self._write_status(status)

    def _write_artifact(self, phase: SubPhase, payload: Any) -> None:
        path = self.root / f"{phase.value}.json"
        if hasattr(payload, "model_dump_json"):
            text = payload.model_dump_json(indent=2)
        elif isinstance(payload, (dict, list)):
            text = json.dumps(payload, indent=2, default=str)
        else:
            text = json.dumps({"value": str(payload)}, indent=2)
        path.write_text(text)

    def read_artifact(self, phase: SubPhase) -> Optional[Dict[str, Any]]:
        path = self.root / f"{phase.value}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None


class RunState:
    """Top-level run state — top-phase tracking + milestone factory.

    ``root`` points at ``<project>/docs/runs/<job_id>/``. Top-phase
    artifacts (plan.json, architecture.json, provision.json, auth.json)
    live at the root. Per-milestone state lives in ``m<N>/`` subdirs.
    """

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def status_path(self) -> Path:
        return self.root / "run_status.json"

    def _read_status(self) -> Dict[str, Any]:
        if not self.status_path.exists():
            return {"top_completed": [], "started_at": datetime.utcnow().isoformat()}
        try:
            return json.loads(self.status_path.read_text())
        except Exception:
            return {"top_completed": []}

    def _write_status(self, status: Dict[str, Any]) -> None:
        self.status_path.write_text(json.dumps(status, indent=2, default=str))

    def completed_top_phases(self) -> List[TopPhase]:
        raw = self._read_status().get("top_completed") or []
        out: List[TopPhase] = []
        for r in raw:
            try:
                out.append(TopPhase(r))
            except Exception:
                continue
        return out

    def is_top_phase_done(self, phase: TopPhase) -> bool:
        return phase in self.completed_top_phases()

    def mark_top_phase(self, phase: TopPhase, payload: Optional[Any] = None) -> None:
        if payload is not None:
            path = self.root / f"{phase.value}.json"
            if hasattr(payload, "model_dump_json"):
                text = payload.model_dump_json(indent=2)
            elif isinstance(payload, (dict, list)):
                text = json.dumps(payload, indent=2, default=str)
            else:
                text = json.dumps({"value": str(payload)}, indent=2)
            path.write_text(text)
        status = self._read_status()
        completed = list(status.get("top_completed") or [])
        if phase.value not in completed:
            completed.append(phase.value)
        status["top_completed"] = completed
        status["updated_at"] = datetime.utcnow().isoformat()
        self._write_status(status)

    def milestone(self, index: int) -> MilestoneState:
        """Return the MilestoneState for milestone ``index`` (1-based)."""
        return MilestoneState(self.root / f"m{index}", index)

    def first_unfinished_milestone(self, total: int) -> int:
        """1-based index of the first milestone not marked DONE.

        Returns ``total + 1`` if every milestone is done.
        """
        for i in range(1, total + 1):
            if not self.milestone(i).is_done():
                return i
        return total + 1
