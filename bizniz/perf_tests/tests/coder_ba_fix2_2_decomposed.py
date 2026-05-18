"""Fat-fails A/B side B: Decomposer runs at runtime to produce a unit
list; each unit is wrapped as a CoderIssue (via the same shim
production uses, ``_unit_to_coder_issue``) and dispatched serially.

Differs from ``coder_be006_decomposed`` (which used pre-baked unit
data on disk) — for BA-fix2-2 the original M3 decomposition isn't
easily available (synthesized recovery issue), so we just run
Decomposer at scenario start and use its output. Both decomposed
and guideline-fat scenarios run Decomposer in the same way, on the
same fixture seed, so the comparison stays fair.

Same fixture as ``coder_ba_fix2_2_fat`` (workspace_seed is a relative
symlink). Same EXPECTED_PATTERNS / quality checks.
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

    # 2. Load parent issue.
    issue_data = json.loads((fixture_root / "issue.json").read_text())

    # 3. Imports.
    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.coder.claude_cli_coder import ClaudeCliCoder
    from bizniz.coder.types import Issue as CoderIssue
    from bizniz.decomposer.agent import Decomposer, DecomposerError
    from bizniz.driver.milestone_code_dispatcher import _unit_to_coder_issue
    from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec
    from bizniz.workspace.local_workspace import LocalWorkspace

    parent_issue = CoderIssue.model_validate(issue_data)

    backend_service = ServiceDefinition(
        name="backend", service_type="backend",
        framework="fastapi", language="python",
        description="recipes API + tags + search + filter",
        workspace_name="backend",
        port=8000,
    )
    arch = SystemArchitecture(
        project_name="recipe_v2 (fat-fails fixture, decomposed mode)",
        project_slug="coder_ba_fix2_2_decomposed",
        description="BA-fix2-2 dispatched per-unit serially",
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
            for ref in parent_issue.spec_refs
        ],
    )

    # 4. Decompose.
    decomp_client = ClaudeCliClient(model_name="claude-cli")
    decomposer = Decomposer(client=decomp_client)
    t_dec = time.time()
    decomp_error = ""
    units: List[Any] = []
    decomp_confidence = None
    try:
        decomp_result = decomposer.decompose(
            issue=parent_issue, service=backend_service, architecture=arch,
        )
        units = decomp_result.ordered_units
        decomp_confidence = decomp_result.confidence
    except DecomposerError as e:
        decomp_error = f"DecomposerError: {e}"
    except Exception as e:
        decomp_error = f"{type(e).__name__}: {e}"
    decomp_elapsed = time.time() - t_dec

    # 5. Construct Coder + dispatch per-unit.
    ws = LocalWorkspace(root=str(workspace))
    coder = ClaudeCliCoder(
        workspace=ws,
        compose_path="",
        target_service="backend",
        workspace_name="backend",
        runner="pytest",
        model_name="claude-cli",
    )

    per_unit: List[Dict[str, Any]] = []
    t_all = time.time()
    total_error = ""
    for u in units:
        unit_issue = _unit_to_coder_issue(unit=u, parent=parent_issue)
        t0 = time.time()
        unit_error = ""
        unit_dump: Dict[str, Any] = {}
        try:
            ur = coder.code_issue(
                issue=unit_issue, architecture=arch, enriched_spec=spec,
            )
            unit_dump = ur.model_dump()
        except Exception as e:
            unit_error = f"{type(e).__name__}: {e}"
            if not total_error:
                total_error = f"first failure at {u.id}: {unit_error}"
        per_unit.append({
            "unit_id": u.id,
            "summary": u.summary,
            "target_file": u.target_file,
            "kind": u.kind,
            "elapsed_s": time.time() - t0,
            "status": unit_dump.get("status"),
            "tier_used": unit_dump.get("tier_used"),
            "iterations_used": unit_dump.get("iterations_used"),
            "error": unit_error or None,
        })
    coder_elapsed = time.time() - t_all

    return {
        "mode": "decomposed_serial",
        "decomposer_elapsed_s": decomp_elapsed,
        "decomposer_error": decomp_error or None,
        "decomposer_confidence": decomp_confidence,
        "decomposer_unit_count": len(units),
        "coder_elapsed_s": coder_elapsed,
        "coder_calls": len(units),
        "coder_error": total_error or None,
        "per_unit": per_unit,
        "median_unit_s": (
            sorted(u["elapsed_s"] for u in per_unit)[len(per_unit) // 2]
            if per_unit else 0
        ),
        "total_elapsed_s": decomp_elapsed + coder_elapsed,
        "patterns": _match_patterns_across_targets(workspace),
        "quality": _validate_targets(workspace),
    }
