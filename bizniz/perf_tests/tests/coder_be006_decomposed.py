"""A/B test, side B: Coder dispatches the 7 BE-006 unit issues
SERIALLY. Same workspace seed + same final scope as the fat side.

Counterpart: ``coder_be006_fat`` — single dispatch covering all 7
units' work.

The workspace accumulates file edits between unit dispatches —
each unit sees the cumulative output of the prior ones, exactly
as the dispatcher does in production.

Records per-unit timing in addition to the total, so we can see
both the multiplier cost AND per-unit cost distribution.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List


EXPECTED_PATTERNS = [
    (r"APIRouter\s*\(", "router instantiation"),
    (r'prefix\s*=\s*[\'"]/recipes[\'"]', "router prefix='/recipes'"),
    (r"@router\.post\s*\(", "POST decorator"),
    (r"@router\.get\s*\(\s*[\'\"]/mine[\'\"]\s*", "GET /mine decorator"),
    (r"@router\.get\s*\(\s*[\'\"]/\{recipe_id\}[\'\"]\s*", "GET /{recipe_id} decorator"),
    (r"@router\.put\s*\(", "PUT decorator"),
    (r"@router\.delete\s*\(", "DELETE decorator"),
    (r"require_roles\s*\(", "require_roles dependency"),
    (r"status\.HTTP_201_CREATED|201", "201 status code"),
    (r"status\.HTTP_204_NO_CONTENT|204", "204 status code"),
    (r"status_code\s*=\s*404|HTTPException\s*\(\s*status_code\s*=\s*404|status\.HTTP_404",
     "404 on miss"),
    (r"status_code\s*=\s*400|HTTPException\s*\(\s*status_code\s*=\s*400|status\.HTTP_400",
     "400 on malformed UUID"),
]


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # 1. Seed workspace.
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

    # 2. Load all 7 issues (ordered).
    issues_data = json.loads((fixture_root / "issues.json").read_text())

    # 3. Lazy imports.
    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.coder.claude_cli_coder import ClaudeCliCoder
    from bizniz.coder.types import Issue as CoderIssue
    from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec
    from bizniz.workspace.local_workspace import LocalWorkspace

    arch = SystemArchitecture(
        project_name="Coder A/B (decomposed)",
        project_slug="coder_be006_decomposed",
        description="7-unit serial CRUD router",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="API", workspace_name="backend",
                port=8000,
            ),
        ],
    )
    spec = EnrichedSpec(
        milestone_name="Recipe CRUD",
        capabilities=[
            CapabilitySpec(
                id="recipe_crud",
                name="Authenticated user can CRUD their recipes",
                description=(
                    "POST + GET list + GET one + PUT + DELETE under "
                    "/api/v1/recipes."
                ),
                test_scenarios=[
                    "POST happy → 201",
                    "GET /mine returns caller's only",
                    "GET miss → 404",
                    "PUT happy → 200",
                    "DELETE happy → 204",
                    "malformed UUID → 400",
                ],
            ),
        ],
    )

    ws = LocalWorkspace(root=str(workspace))
    coder = ClaudeCliCoder(
        workspace=ws,
        compose_path="",
        target_service="backend",
        workspace_name="backend",
        runner="pytest",
        model_name="claude-cli",
    )

    # 4. Dispatch the 7 units serially. Each one sees the previous's edits.
    per_unit: List[Dict[str, Any]] = []
    t_all = time.time()
    total_error: str = ""
    for issue_data in issues_data:
        issue = CoderIssue.model_validate(issue_data)
        t0 = time.time()
        unit_error = ""
        unit_dump: Dict[str, Any] = {}
        try:
            ur = coder.code_issue(
                issue=issue, architecture=arch, enriched_spec=spec,
            )
            unit_dump = ur.model_dump()
        except Exception as e:
            unit_error = f"{type(e).__name__}: {e}"
            if not total_error:
                total_error = (
                    f"first failure at {issue_data.get('id')}: {unit_error}"
                )
        per_unit.append({
            "unit_id": issue_data.get("id"),
            "title": issue_data.get("title"),
            "elapsed_s": time.time() - t0,
            "status": unit_dump.get("status"),
            "tier_used": unit_dump.get("tier_used"),
            "iterations_used": unit_dump.get("iterations_used"),
            "error": unit_error or None,
        })
    total_elapsed = time.time() - t_all

    # 5. Validate final file.
    target = workspace / "app" / "api" / "routes" / "recipes.py"
    target_exists = target.exists()
    target_size = target.stat().st_size if target_exists else 0
    text = target.read_text(encoding="utf-8", errors="replace") if target_exists else ""
    matched = {label: bool(re.search(p, text)) for p, label in EXPECTED_PATTERNS}
    pass_count = sum(matched.values())

    return {
        "mode": "decomposed_serial",
        "coder_elapsed_s": total_elapsed,
        "coder_calls": len(per_unit),
        "coder_error": total_error or None,
        "per_unit": per_unit,
        "median_unit_s": (
            sorted(u["elapsed_s"] for u in per_unit)[len(per_unit) // 2]
            if per_unit else 0
        ),
        "target_file": {
            "exists": target_exists,
            "size_bytes": target_size,
            "patterns_matched": matched,
            "match_rate": f"{pass_count}/{len(EXPECTED_PATTERNS)}",
        },
    }
