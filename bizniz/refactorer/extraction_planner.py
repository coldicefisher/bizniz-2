"""Extraction planner — Phase E.

Consumes the CPD ``CPDReport`` (Phase A) and decides what to
extract to ``core/`` (Phase B's scaffold). Outputs a list of
``ExtractionPlan`` records the executor (Phase F) applies.

Heuristics for "what's a good extraction candidate":

1. **Cross-service** — the duplicate must appear in 2+ distinct
   service workspace dirs (not just 2+ files in the same service).
   Same-service dupes are within-service refactor work, not core.
2. **Single language** — duplicates must be all-Python or all-TS;
   we don't try to translate cross-language.
3. **Not in vendored / generated dirs** — paths under ``.pkgs/``,
   ``node_modules/``, ``dist/``, ``__pycache__/`` are excluded.
4. **Not in test code** — test duplicates are usually fixture
   patterns, not business logic. (The anti-pattern scanner handles
   test-code smells separately.)

Each candidate gets a ``risk_score`` 0-1 (lower = safer to extract):

- More files = higher risk (more imports to rewrite)
- Larger token count = higher risk (more LOC moving)
- Files in different services = lower risk (extraction is the point)
- Within-file duplicates = higher risk (probably feature-internal,
  not shared business logic)

The planner does NOT call an LLM. It's pure analytical filtering
of the CPD output. The executor + Refactorer agent (later phases)
use LLM judgment to decide HOW to extract and self-rate confidence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Literal, Optional, Set

from pydantic import BaseModel, Field

from bizniz.refactorer.cpd import CPDReport, DuplicateBlock


# Paths matching any of these segments are excluded from extraction.
_EXCLUDED_PATH_SEGMENTS: Set[str] = {
    ".pkgs", "node_modules", "dist", "build", ".bizniz",
    "__pycache__", ".venv", "venv", ".git",
    # Test code is excluded — test duplicates are usually fixture
    # patterns, handled by the anti-pattern scanner instead.
    "tests", "test", "conftest",
}


# Path segments commonly used as service workspace roots in bizniz
# projects. The planner uses these to bucket files by service —
# duplicates that all map to the SAME service aren't cross-service.
def _service_for_path(path: str, project_root: Optional[Path] = None) -> str:
    """Return a stable identifier for which service a path belongs to.

    Heuristic: take the first directory segment that isn't ``core``
    or one of the excluded names. For paths outside a recognizable
    project layout, return ``"unknown"``.
    """
    p = Path(path)
    if project_root is not None:
        try:
            rel = p.relative_to(project_root)
        except ValueError:
            return "unknown"
    else:
        rel = p
    for seg in rel.parts:
        # Skip filesystem root marker.
        if seg in ("/", "\\"):
            continue
        if seg == "core":
            return "core"
        if seg in _EXCLUDED_PATH_SEGMENTS:
            continue
        if seg.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
            continue
        return seg
    return "unknown"


def _is_excluded(path: str) -> bool:
    parts = Path(path).parts
    return any(seg in _EXCLUDED_PATH_SEGMENTS for seg in parts)


def _language_for_path(path: str) -> str:
    if path.endswith(".py"):
        return "python"
    if path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        return "typescript"
    return "unknown"


# ── Output schema ────────────────────────────────────────────────


Disposition = Literal["extract", "skip", "manual_review"]


class ExtractionPlan(BaseModel):
    """One proposed extraction the executor will apply."""
    duplicate_hash: str
    language: str  # "python" or "typescript"
    services_involved: List[str] = Field(default_factory=list)
    source_files: List[str] = Field(default_factory=list)
    token_count: int = 0
    files_count: int = 0
    instance_count: int = 0
    suggested_core_path: str = Field(
        description=(
            "Path RELATIVE to core/<lang>/ where this block should land. "
            "The executor uses this as a hint; the LLM-driven naming step "
            "can refine it."
        ),
    )
    risk_score: float = Field(default=0.5, ge=0.0, le=1.0)
    disposition: Disposition = "extract"
    notes: List[str] = Field(default_factory=list)


class ExtractionPlanReport(BaseModel):
    """All planned extractions + per-disposition counts."""
    plans: List[ExtractionPlan] = Field(default_factory=list)
    skipped_duplicates_count: int = 0
    total_duplicates_considered: int = 0

    def extract_plans(self) -> List[ExtractionPlan]:
        return [p for p in self.plans if p.disposition == "extract"]

    def manual_review_plans(self) -> List[ExtractionPlan]:
        return [p for p in self.plans if p.disposition == "manual_review"]


# ── Planner ──────────────────────────────────────────────────────


def _risk_score(
    files_count: int,
    instance_count: int,
    token_count: int,
    services_count: int,
) -> float:
    """Heuristic risk score 0-1 (lower = safer to extract).

    - 5+ files = higher risk (5 import rewrites)
    - Within-file (services_count == 1) = high risk
    - Single service involved = moderate risk
    - 2-3 services = sweet spot for extraction → low risk
    """
    risk = 0.0
    # Within-file only — extraction probably not the right move.
    if services_count <= 1:
        risk += 0.6
    elif services_count == 2:
        risk += 0.1  # the canonical "extract" case
    elif services_count >= 5:
        risk += 0.3  # broad extraction, more import surface
    # File-count risk.
    if files_count > 5:
        risk += 0.2
    elif files_count > 10:
        risk += 0.3
    # Token count — very large blocks may have feature-specific bits
    # mixed in with the shared logic; safer to surface for human.
    if token_count > 200:
        risk += 0.2
    return min(1.0, risk)


def _suggested_core_path(
    duplicate: DuplicateBlock, language: str,
) -> str:
    """Suggest a relative core/ path based on the file name(s)
    common to the occurrences."""
    if not duplicate.occurrences:
        return "shared.py" if language == "python" else "shared.ts"
    # Use the first occurrence's file name as a starting point.
    first = duplicate.occurrences[0]
    name = Path(first.path).stem
    # Light heuristic: if the name contains common business-logic
    # words, route to a known subpackage; else generic shared.
    name_lower = name.lower()
    if "company" in name_lower or "contact" in name_lower or "deal" in name_lower:
        if language == "python":
            return f"business/{name_lower}.py"
        return f"business/{name_lower}.ts"
    if "model" in name_lower or "schema" in name_lower or "dto" in name_lower:
        if language == "python":
            return f"dtos/{name_lower}.py"
        return f"dtos/{name_lower}.ts"
    if language == "python":
        return f"shared/{name_lower}.py"
    return f"shared/{name_lower}.ts"


def plan_extractions(
    cpd_report: CPDReport,
    project_root: Optional[Path] = None,
    min_files: int = 2,
    min_services: int = 2,
    high_risk_threshold: float = 0.6,
) -> ExtractionPlanReport:
    """Convert a ``CPDReport`` into an ``ExtractionPlanReport``.

    - Only blocks meeting ``min_files`` AND ``min_services`` thresholds
      become extraction candidates.
    - Duplicates in excluded paths (.pkgs, node_modules, tests, etc.)
      are skipped silently.
    - Cross-language blocks (impossible from CPD output but
      defensively checked) are skipped.
    - Risk score ≥ ``high_risk_threshold`` flips disposition to
      ``manual_review`` so a human looks before the executor moves
      anything.
    """
    plans: List[ExtractionPlan] = []
    skipped = 0
    total = 0

    for dup in cpd_report.duplicates:
        total += 1
        # Filter excluded paths.
        valid_occurrences = [
            o for o in dup.occurrences if not _is_excluded(o.path)
        ]
        if len(valid_occurrences) < min_files:
            skipped += 1
            continue

        # Language check — block must be all-one-language.
        languages = {_language_for_path(o.path) for o in valid_occurrences}
        if len(languages) != 1 or "unknown" in languages:
            skipped += 1
            continue
        language = next(iter(languages))

        # Service bucketing.
        services = sorted({
            _service_for_path(o.path, project_root)
            for o in valid_occurrences
        })
        services_clean = [s for s in services if s not in ("unknown", "core")]
        if len(services_clean) < min_services and len(valid_occurrences) < 2:
            skipped += 1
            continue
        # If the duplicate is entirely within one service, still
        # consider it but mark it as manual_review — within-service
        # refactor is different work.
        is_cross_service = len(services_clean) >= min_services

        source_files = sorted({o.path for o in valid_occurrences})
        risk = _risk_score(
            files_count=len(source_files),
            instance_count=dup.total_instances,
            token_count=dup.token_count,
            services_count=len(services_clean),
        )

        notes: List[str] = []
        disposition: Disposition = "extract"
        if not is_cross_service:
            disposition = "manual_review"
            notes.append(
                "Duplicate is within one service — within-service "
                "refactor, not a core-lib extraction."
            )
        if risk >= high_risk_threshold and disposition == "extract":
            disposition = "manual_review"
            notes.append(
                f"Risk score {risk:.2f} ≥ threshold "
                f"{high_risk_threshold:.2f} — surface for human review."
            )

        plans.append(ExtractionPlan(
            duplicate_hash=dup.shingle_hash,
            language=language,
            services_involved=services_clean,
            source_files=source_files,
            token_count=dup.token_count,
            files_count=dup.files_count,
            instance_count=dup.total_instances,
            suggested_core_path=_suggested_core_path(dup, language),
            risk_score=risk,
            disposition=disposition,
            notes=notes,
        ))

    # Sort: extractable first (lowest risk first), then manual_review.
    plans.sort(
        key=lambda p: (p.disposition != "extract", p.risk_score),
    )

    return ExtractionPlanReport(
        plans=plans,
        skipped_duplicates_count=skipped,
        total_duplicates_considered=total,
    )
