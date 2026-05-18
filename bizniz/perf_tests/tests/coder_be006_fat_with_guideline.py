"""A/B test, side C: Decomposer runs FIRST to produce a unit-order
guideline, then ONE fat Coder dispatch consumes that guideline as
advisory context.

Hypothesis: Decomposer's planning value is real, but its dispatch
multiplier (7 calls, +900s) is the cost. Bundling the unit list as
a single-call guideline pays the Decomposer cost ONCE (~26s) and
keeps the fat-side wall clock.

Counterparts:
- ``coder_be006_fat`` — bare fat (no Decomposer in the loop)
- ``coder_be006_decomposed`` — 7 serial dispatches

The guideline is appended to the issue description with a clear
"advisory, not literal" marker so the LLM treats it as a suggested
breakdown, not a mandate to produce 7 files.

Fixture: ``coder_be006_fat_with_guideline/`` — issue.json is a copy
of fat's, workspace_seed is a symlink to fat's. Single source of
truth for the BE-006 scope.
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


def _format_guideline(units: List[Any]) -> str:
    lines = [
        "",
        "## Suggested implementation breakdown (advisory)",
        "",
        "The following ordered units are a planning aid — implement "
        "the entire scope in ONE file as described above; do NOT split "
        "into separate files per unit. Use this list to make sure no "
        "sub-piece is forgotten.",
        "",
    ]
    for i, u in enumerate(units, 1):
        kind = getattr(u, "kind", "")
        deps = getattr(u, "depends_on", []) or []
        deps_str = f" (depends on: {', '.join(deps)})" if deps else ""
        lines.append(f"{i}. **{u.id}** [{kind}] — {u.summary}{deps_str}")
        if getattr(u, "description", None):
            lines.append(f"   - {u.description}")
        notes = getattr(u, "notes", None)
        if notes:
            lines.append(f"   - _note:_ {notes}")
    return "\n".join(lines)


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
        description="API", workspace_name="backend",
        port=8000,
    )
    arch = SystemArchitecture(
        project_name="Coder A/B (guideline-fat)",
        project_slug="coder_be006_fat_with_guideline",
        description="decomposer-as-guideline + single-dispatch CRUD router",
        services=[backend_service],
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

    # 4. Decompose first.
    decomp_client = ClaudeCliClient(model_name="claude-cli")
    decomposer = Decomposer(client=decomp_client)
    t_dec = time.time()
    decomp_error = ""
    units: List[Any] = []
    decomp_confidence = None
    try:
        decomp_result = decomposer.decompose(
            issue=issue,
            service=backend_service,
            architecture=arch,
        )
        units = decomp_result.ordered_units
        decomp_confidence = decomp_result.confidence
    except DecomposerError as e:
        decomp_error = f"DecomposerError: {e}"
    except Exception as e:
        decomp_error = f"{type(e).__name__}: {e}"
    decomp_elapsed = time.time() - t_dec

    # 5. Build the guideline-augmented issue.
    if units:
        guideline = _format_guideline(units)
        augmented_issue = issue.model_copy(
            update={"description": issue.description + "\n" + guideline}
        )
    else:
        # Decomposer failed — fall back to bare fat. Still useful
        # data: we measure what happens when the planning step
        # blows up.
        augmented_issue = issue

    # 6. Dispatch ONE fat Coder call with the guideline-augmented issue.
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

    # 7. Validate.
    target = workspace / "app" / "api" / "routes" / "recipes.py"
    target_exists = target.exists()
    target_size = target.stat().st_size if target_exists else 0
    text = target.read_text(encoding="utf-8", errors="replace") if target_exists else ""
    matched = {label: bool(re.search(p, text)) for p, label in EXPECTED_PATTERNS}
    pass_count = sum(matched.values())

    from bizniz.perf_tests.validate import validate_output
    quality = validate_output(target_path=target, workspace_root=workspace)

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
            "summary": (result_dump.get("summary") or "")[:500],
        },
        "total_elapsed_s": decomp_elapsed + coder_elapsed,
        "target_file": {
            "exists": target_exists,
            "size_bytes": target_size,
            "patterns_matched": matched,
            "match_rate": f"{pass_count}/{len(EXPECTED_PATTERNS)}",
        },
        "quality": quality,
    }
