"""A/B test, side A: Coder dispatches the FAT BE-006 issue in ONE call.

Same workspace seed as ``coder_be006_decomposed``. The fat issue
spec carries the FULL CRUD-router scope (POST + GET list + GET one
+ PUT + DELETE + UUID coercion) so Coder gets the same problem in
one shot.

Counterpart: ``coder_be006_decomposed`` — runs 7 unit issues in
sequence.

Compare the two via ``python -m bizniz.perf_tests compare
coder_be006_fat/<run> coder_be006_decomposed/<run>``.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict


# Same validators as coder_single_issue plus checks for the other
# CRUD endpoints + UUID coercion behavior.
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

    # 2. Load fat issue.
    issue_data = json.loads((fixture_root / "issue.json").read_text())

    # 3. Lazy imports.
    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.coder.claude_cli_coder import ClaudeCliCoder
    from bizniz.coder.types import Issue as CoderIssue
    from bizniz.quality_engineer.types import CapabilitySpec, EnrichedSpec
    from bizniz.workspace.local_workspace import LocalWorkspace

    issue = CoderIssue.model_validate(issue_data)

    arch = SystemArchitecture(
        project_name="Coder A/B (fat)",
        project_slug="coder_be006_fat",
        description="single-dispatch CRUD router",
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
                    "POST happy path → 201 + RecipeOut",
                    "GET /mine returns caller's recipes only",
                    "GET /{id} missing → 404",
                    "PUT happy path → 200 + RecipeOut",
                    "DELETE happy path → 204 empty body",
                    "malformed UUID path → 400",
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

    target = workspace / "app" / "api" / "routes" / "recipes.py"
    target_exists = target.exists()
    target_size = target.stat().st_size if target_exists else 0
    text = target.read_text(encoding="utf-8", errors="replace") if target_exists else ""
    matched = {label: bool(re.search(p, text)) for p, label in EXPECTED_PATTERNS}
    pass_count = sum(matched.values())

    from bizniz.perf_tests.validate import validate_output
    quality = validate_output(target_path=target, workspace_root=workspace)

    return {
        "mode": "fat_single_dispatch",
        "coder_elapsed_s": elapsed,
        "coder_calls": 1,
        "coder_error": error or None,
        "coder_result": {
            "status": result_dump.get("status"),
            "tier_used": result_dump.get("tier_used"),
            "iterations_used": result_dump.get("iterations_used"),
            "target_files_written": result_dump.get("target_files_written"),
            "summary": (result_dump.get("summary") or "")[:500],
        },
        "target_file": {
            "exists": target_exists,
            "size_bytes": target_size,
            "patterns_matched": matched,
            "match_rate": f"{pass_count}/{len(EXPECTED_PATTERNS)}",
        },
        "quality": quality,
    }
