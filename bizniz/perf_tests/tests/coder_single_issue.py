"""Baseline microbenchmark: time ONE Coder dispatch on ONE known-
good issue.

Fixture: ``bizniz/perf_tests/fixtures/coder_single_issue/``

  - ``workspace_seed/`` — bizniz-skeleton-fastapi + Recipe model +
    RecipeCreate/RecipeOut schemas + a services helper module.
    Represents the state after BE-001..BE-005 in a recipe_v2-class
    project; what Coder would actually see if it were dispatched
    on BE-006-U2 in production.
  - ``issue.json`` — the CoderIssue spec for "Implement POST
    /api/recipes (create)".

The scenario:

  1. Copy ``workspace_seed/`` to the per-run workspace.
  2. Construct a minimal SystemArchitecture + EnrichedSpec stub.
  3. Dispatch ClaudeCliCoder on the issue.
  4. Measure wall-clock of ``code_issue()``.
  5. Validate the produced ``app/api/routes/recipes.py`` contains
     the expected structural markers (router decl, POST handler,
     require_roles, ensure_local_user).

Returns the timing + validation dict to the runner. The runner
writes ``result.json`` and tags the bizniz repo
``perf/coder_single_issue/run-N``.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict


# Expected markers in the produced recipes.py — checks the Coder
# produced the right shape, not byte-for-byte identical code.
EXPECTED_PATTERNS = [
    (r"APIRouter\s*\(", "router instantiation"),
    (r'prefix\s*=\s*[\'"]/recipes[\'"]', "router prefix='/recipes'"),
    (r"@router\.post\s*\(", "POST decorator"),
    (r"async\s+def\s+\w+_endpoint?\s*\(|async\s+def\s+create_recipe\w*",
     "async POST handler"),
    (r"require_roles\s*\(", "require_roles dependency"),
    (r"ensure_local_user\s*\(", "ensure_local_user call"),
    (r"create_recipe\s*\(", "create_recipe call"),
    (r"RecipeCreate", "RecipeCreate type"),
    (r"RecipeOut", "RecipeOut response_model"),
    (r"status\.HTTP_201_CREATED|201", "201 created status"),
]


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    """Entry point invoked by ``bizniz.perf_tests.runner``.

    ``workspace`` is the per-run directory (already created, empty).
    ``fixture_root`` is the test's fixture directory.
    """
    # 1. Seed the workspace.
    seed_dir = fixture_root / "workspace_seed"
    if not seed_dir.exists():
        return {"error": f"fixture missing workspace_seed: {seed_dir}"}
    # Use the workspace dir directly — runner created it empty.
    for entry in seed_dir.iterdir():
        if entry.is_dir():
            shutil.copytree(
                entry, workspace / entry.name,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache",
                    "*.pyc", ".git",
                ),
            )
        else:
            shutil.copy2(entry, workspace / entry.name)

    # 2. Load the issue spec.
    issue_path = fixture_root / "issue.json"
    issue_data = json.loads(issue_path.read_text())

    # 3. Import the bizniz types lazily (perf_tests/__init__.py
    # stays import-light).
    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.coder.claude_cli_coder import ClaudeCliCoder
    from bizniz.coder.types import Issue as CoderIssue
    from bizniz.quality_engineer.types import (
        CapabilitySpec, EnrichedSpec,
    )
    from bizniz.workspace.local_workspace import LocalWorkspace

    issue = CoderIssue.model_validate(issue_data)

    # 4. Minimal Architecture + EnrichedSpec stubs.
    arch = SystemArchitecture(
        project_name="Coder Microbench",
        project_slug="coder_microbench",
        description="single-issue Coder perf test",
        services=[
            ServiceDefinition(
                name="backend",
                service_type="backend",
                framework="fastapi",
                language="python",
                description="API",
                workspace_name="backend",
                port=8000,
            ),
        ],
    )
    spec = EnrichedSpec(
        milestone_name="Recipe CRUD",
        capabilities=[
            CapabilitySpec(
                id="create_recipe",
                name="Authenticated user can create a recipe",
                description="POST /api/v1/recipes accepts a payload and stores it.",
                test_scenarios=[
                    "happy path: valid payload + valid JWT → 201 + RecipeOut",
                    "unauthenticated request → 401",
                    "missing role → 403",
                    "payload with owner_id → 422 (extra='forbid')",
                ],
            ),
        ],
    )

    # 5. Construct workspace + Coder.
    # ClaudeCliCoder edits files under workspace.root.
    ws = LocalWorkspace(root=str(workspace))
    coder = ClaudeCliCoder(
        workspace=ws,
        compose_path="",  # not used for our purposes
        target_service="backend",
        workspace_name="backend",
        runner="pytest",
        model_name="claude-cli",
    )

    # 6. Dispatch — measure.
    t0 = time.time()
    error: str = ""
    coder_result_dump: Dict[str, Any] = {}
    try:
        result = coder.code_issue(
            issue=issue,
            architecture=arch,
            enriched_spec=spec,
        )
        coder_result_dump = result.model_dump()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    coder_elapsed_s = time.time() - t0

    # 7. Validate the produced recipes.py against expected patterns.
    target_path = workspace / "app" / "api" / "routes" / "recipes.py"
    matched_patterns: Dict[str, bool] = {}
    target_exists = target_path.exists()
    target_size = target_path.stat().st_size if target_exists else 0
    if target_exists:
        text = target_path.read_text(encoding="utf-8", errors="replace")
        for pattern, label in EXPECTED_PATTERNS:
            matched_patterns[label] = bool(re.search(pattern, text))
    else:
        for _pat, label in EXPECTED_PATTERNS:
            matched_patterns[label] = False

    pass_count = sum(1 for v in matched_patterns.values() if v)
    total_patterns = len(EXPECTED_PATTERNS)

    from bizniz.perf_tests.validate import validate_output
    quality = validate_output(target_path=target_path, workspace_root=workspace)

    return {
        "coder_elapsed_s": coder_elapsed_s,
        "coder_error": error or None,
        "coder_result": {
            "status": coder_result_dump.get("status"),
            "tier_used": coder_result_dump.get("tier_used"),
            "iterations_used": coder_result_dump.get("iterations_used"),
            "target_files_written": coder_result_dump.get(
                "target_files_written"
            ),
            "summary": (coder_result_dump.get("summary") or "")[:500],
        },
        "target_file": {
            "exists": target_exists,
            "size_bytes": target_size,
            "patterns_matched": matched_patterns,
            "match_rate": f"{pass_count}/{total_patterns}",
        },
        "quality": quality,
    }
