"""Tier-strategy A/B: same bare-fat dispatch as
``coder_ba_fix2_2_fat`` but pinned to **Haiku 4.5** via
``--model=claude-haiku-4-5``.

Hypothesis: if Haiku's bare fat lands at parity quality on BA-fix2-2
(14/14 + AST + symbols clean), we can default Coder to Haiku and
escalate to Opus on stall. Roughly 4-5× cheaper per token on the
metered API; per-call wall is also typically much faster on Haiku.

If Haiku DROPS quality (misses the cross-user-tag-side-effect
property, hallucinates symbols, etc.) → the tier strategy doesn't
pencil out as a flat replacement, but Decomposer-on-Haiku might
recover the gap. That's the next experiment.

Fixture identical to ``coder_ba_fix2_2_fat`` (relative symlink to
fat's workspace_seed); only the model knob differs.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict


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

HAIKU_MODEL = "claude-haiku-4-5"


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
    from bizniz.coder.claude_cli_coder import ClaudeCliCoder
    from bizniz.coder.types import Issue as CoderIssue
    from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec
    from bizniz.workspace.local_workspace import LocalWorkspace

    issue = CoderIssue.model_validate(issue_data)

    arch = SystemArchitecture(
        project_name="recipe_v2 (fat-fails fixture, Haiku)",
        project_slug="coder_ba_fix2_2_fat_haiku",
        description="BA-fix2-2 single dispatch with Haiku Coder",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="recipes API + tags + search + filter",
                workspace_name="backend",
                port=8000,
            ),
        ],
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

    ws = LocalWorkspace(root=str(workspace))
    coder = ClaudeCliCoder(
        workspace=ws,
        compose_path="",
        target_service="backend",
        workspace_name="backend",
        runner="pytest",
        model_name=f"claude-cli ({HAIKU_MODEL})",
        # Pin the Claude CLI subprocess to Haiku for this scenario.
        # Same dispatch path as bare-fat — only the model differs.
        additional_args=["--model", HAIKU_MODEL],
    )

    t0 = time.time()
    error: str = ""
    result_dump: Dict[str, Any] = {}
    try:
        result = coder.code_issue(
            issue=issue, architecture=arch, enriched_spec=spec,
        )
        result_dump = result.model_dump()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    return {
        "mode": "fat_single_dispatch_haiku",
        "model": HAIKU_MODEL,
        "coder_elapsed_s": elapsed,
        "coder_calls": 1,
        "coder_error": error or None,
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
        "patterns": _match_patterns_across_targets(workspace),
        "quality": _validate_targets(workspace),
    }
