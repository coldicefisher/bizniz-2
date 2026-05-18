"""Fat-fails A/B side C: Decomposer runs first to produce a unit-list
guideline; that list is appended (advisory, not literal) to the
``BA-fix2-2`` description; then ONE fat Coder dispatch consumes the
augmented issue.

Same fixture + same expected-pattern coverage as
``coder_ba_fix2_2_fat`` / ``coder_ba_fix2_2_decomposed``. Adds
``decomposer_*`` fields to the result so the three sides are
directly comparable.

Hypothesis under this fixture: if Decomposer has ANY real value-add,
it should show here — the BE-006 fixture didn't trip fat at all, so
any planning win was invisible. Here fat is expected to struggle.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List


EXPECTED_PATTERNS = [
    (r"selectinload\s*\(", "selectinload used"),
    (r"list_recipes_for_owner\s*\([^)]*q\s*[:=]", "list_recipes_for_owner q param"),
    (r"list_recipes_for_owner\s*\([^)]*required_tag_ids", "list_recipes_for_owner required_tag_ids"),
    (r"_escape_like_pattern\s*\(", "_escape_like_pattern called"),
    (r"@router\.get\s*\(\s*[\'\"]/tags[\'\"]", "GET /tags handler"),
    (r"_normalize_tag_param\s*\(", "_normalize_tag_param helper"),
    (r"payload\.tags", "payload.tags read in POST/PUT"),
    (r"upsert_tags_for_owner\s*\(", "upsert_tags_for_owner called"),
    (r"link_tags_to_recipe\s*\(", "link_tags_to_recipe called"),
    (r"replace_tag_links_for_recipe\s*\(", "replace_tag_links_for_recipe called"),
    (r"list_tags_with_counts_for_owner\s*\(", "list_tags_with_counts_for_owner called"),
    (r"find_tag_id_by_name_for_owner\s*\(", "find_tag_id_by_name_for_owner called"),
    (r"recipe_tags_changed", "recipe_tags_changed audit event"),
    (r"Query\s*\(", "Query() default — query-param wiring"),
]

TARGETS = [
    "app/repositories/recipes.py",
    "app/api/routes/recipes.py",
]


def _format_guideline(units: List[Any]) -> str:
    lines = [
        "",
        "## Suggested implementation breakdown (advisory)",
        "",
        "The following ordered units are a planning aid — implement the "
        "entire scope in the listed target files; do NOT split into "
        "separate files per unit unless the original target_files list "
        "above already calls for it. Use this list to make sure no "
        "sub-piece is forgotten.",
        "",
    ]
    for i, u in enumerate(units, 1):
        kind = getattr(u, "kind", "")
        deps = getattr(u, "depends_on", []) or []
        deps_str = f" (depends on: {', '.join(deps)})" if deps else ""
        lines.append(f"{i}. **{u.id}** [{kind}] — {u.summary}{deps_str}")
        desc = getattr(u, "description", None)
        if desc:
            short = desc if len(desc) < 600 else desc[:600] + "…"
            lines.append(f"   - {short}")
        notes = getattr(u, "notes", None)
        if notes:
            lines.append(f"   - _note:_ {notes}")
    return "\n".join(lines)


def _validate_targets(workspace: Path) -> Dict[str, Any]:
    from bizniz.perf_tests.validate import validate_output
    return {rel: validate_output(workspace / rel, workspace) for rel in TARGETS}


def _match_patterns_across_targets(workspace: Path) -> Dict[str, Any]:
    text = ""
    for rel in TARGETS:
        p = workspace / rel
        if p.exists():
            text += p.read_text(encoding="utf-8", errors="replace") + "\n"
    matched = {label: bool(re.search(p, text)) for p, label in EXPECTED_PATTERNS}
    return {
        "matched": matched,
        "match_rate": f"{sum(matched.values())}/{len(EXPECTED_PATTERNS)}",
    }


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # 1. Seed.
    seed_dir = fixture_root / "workspace_seed"
    for entry in seed_dir.iterdir():
        dst = workspace / entry.name
        if entry.is_dir():
            shutil.copytree(
                entry, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", "*.pyc", ".git",
                ),
            )
        else:
            shutil.copy2(entry, dst)

    # 2. Load issue.
    issue_data = json.loads((fixture_root / "issue.json").read_text())

    # 3. Imports.
    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.coder.claude_cli_coder import ClaudeCliCoder
    from bizniz.coder.types import Issue as CoderIssue
    from bizniz.decomposer.agent import Decomposer, DecomposerError
    from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec
    from bizniz.workspace.local_workspace import LocalWorkspace

    issue = CoderIssue.model_validate(issue_data)

    backend_service = ServiceDefinition(
        name="backend", service_type="backend",
        framework="fastapi", language="python",
        description="recipes API + tags + search + filter",
        workspace_name="backend",
        port=8000,
    )
    arch = SystemArchitecture(
        project_name="recipe_v2 (fat-fails fixture, guideline mode)",
        project_slug="coder_ba_fix2_2_fat_with_guideline",
        description="BA-fix2-2 with decomposer-as-guideline",
        services=[backend_service],
    )
    spec = EnrichedSpec(
        milestone_name="Tags + search + filter (M3 wiring)",
        capabilities=[
            CapabilitySpec(
                id=ref, name=ref.replace("_", " ").title(),
                description=f"Capability {ref} per the issue spec.",
                test_scenarios=[],
            )
            for ref in issue.spec_refs
        ],
    )

    # 4. Decompose first.
    decomp_client = ClaudeCliClient(model_name="claude-cli")
    decomposer = Decomposer(client=decomp_client)
    t_dec = time.time()
    decomp_error = ""
    units: List[Any] = []
    decomp_confidence = None
    try:
        decomp_result = decomposer.decompose(
            issue=issue, service=backend_service, architecture=arch,
        )
        units = decomp_result.ordered_units
        decomp_confidence = decomp_result.confidence
    except DecomposerError as e:
        decomp_error = f"DecomposerError: {e}"
    except Exception as e:
        decomp_error = f"{type(e).__name__}: {e}"
    decomp_elapsed = time.time() - t_dec

    # 5. Build augmented issue.
    if units:
        guideline = _format_guideline(units)
        augmented_issue = issue.model_copy(
            update={"description": issue.description + "\n" + guideline}
        )
    else:
        augmented_issue = issue

    # 6. Dispatch one fat Coder call.
    ws = LocalWorkspace(root=str(workspace))
    coder = ClaudeCliCoder(
        workspace=ws,
        compose_path="",
        target_service="backend",
        workspace_name="backend",
        runner="pytest",
        model_name="claude-cli",
    )

    t_cod = time.time()
    coder_error = ""
    result_dump: Dict[str, Any] = {}
    try:
        result = coder.code_issue(
            issue=augmented_issue, architecture=arch, enriched_spec=spec,
        )
        result_dump = result.model_dump()
    except Exception as e:
        coder_error = f"{type(e).__name__}: {e}"
    coder_elapsed = time.time() - t_cod

    return {
        "mode": "guideline_fat",
        "decomposer_elapsed_s": decomp_elapsed,
        "decomposer_error": decomp_error or None,
        "decomposer_confidence": decomp_confidence,
        "decomposer_unit_count": len(units),
        "decomposer_units": [
            {
                "id": u.id, "summary": u.summary, "kind": u.kind,
                "target_file": u.target_file, "depends_on": u.depends_on,
            }
            for u in units
        ],
        "coder_elapsed_s": coder_elapsed,
        "coder_calls": 1,
        "coder_error": coder_error or None,
        "coder_result": {
            "status": result_dump.get("status"),
            "tier_used": result_dump.get("tier_used"),
            "iterations_used": result_dump.get("iterations_used"),
            "target_files_written": result_dump.get("target_files_written"),
            "test_files_written": result_dump.get("test_files_written"),
            "summary": (result_dump.get("summary") or "")[:500],
            "unresolved_symbols_at_exit": result_dump.get(
                "unresolved_symbols_at_exit", []
            ),
            "last_test_output_tail": (
                result_dump.get("last_test_output_tail") or ""
            )[-2000:],
        },
        "total_elapsed_s": decomp_elapsed + coder_elapsed,
        "patterns": _match_patterns_across_targets(workspace),
        "quality": _validate_targets(workspace),
    }
