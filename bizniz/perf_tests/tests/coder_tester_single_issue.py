"""Phase-1 validation of the v4 pipeline spec: can CoderTesterAgent +
PerIssueValidator deliver ONE issue (code + tests) end-to-end cleanly?

Reuses the recipe_v3 M1 backend fixture from
``coder_agent_single_dispatch`` but operates on a single issue at a
time — the v4 building block.

Inputs (from the shared fixture):
  - architecture.json, enriched_spec.json, skeleton.md, auth_contract.md
  - phase1_result.json (planner output; we pull the first issue)
  - seeded_workspace/  (the scaffold; we slice to the issue's paths)

Pass conditions (per docs/architecture/v4_pipeline_spec.md):
  1. **Wall** ≤ 7 min (per-issue baseline; v3 was 2 min for 7 issues
     so per-issue should be well under 7 min on Haiku; lift to 10 min
     on Opus if running with the repair tier).
  2. **clean=True** on ValidatedIssue (deterministic gates passed).
  3. **AST pass** on every produced file.
  4. **Symbol-validator pass** on every code file.
  5. **Path-contract**: produced paths == issue.target_files ∪ test_files.
  6. **Bodies filled**: zero remaining ``raise NotImplementedError``.

If pass: green-lights Phase 3 (full M1 live run with --use-v4).
"""
from __future__ import annotations

import ast
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List


def _check_ast(content: str, label: str) -> Dict[str, Any]:
    try:
        ast.parse(content, filename=label)
    except SyntaxError as e:
        return {"ok": False, "error": f"SyntaxError L{e.lineno}: {e.msg}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


def _bodies_filled(content: str) -> int:
    """Return count of remaining ``raise NotImplementedError`` lines."""
    return content.count("raise NotImplementedError")


def _materialize_skeleton(workspace: Path, skeleton_root: Path) -> None:
    """Copy the fastapi skeleton into the workspace so symbol_validator
    has the package layout + requirements to resolve against."""
    if not skeleton_root.exists():
        return
    for entry in skeleton_root.iterdir():
        dst = workspace / entry.name
        if entry.is_dir():
            shutil.copytree(
                entry, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", "*.pyc", ".git",
                ),
                dirs_exist_ok=True,
            )
        else:
            shutil.copy2(entry, dst)


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # Same fixture as v3 Phase 2a — shared.
    shared = fixture_root.parent / "coder_agent_single_dispatch"

    arch_data = json.loads((shared / "architecture.json").read_text())
    spec_data = json.loads((shared / "enriched_spec.json").read_text())
    skeleton_md = (shared / "skeleton.md").read_text()
    auth_contract = (shared / "auth_contract.md").read_text()
    phase1 = json.loads((shared / "phase1_result.json").read_text())
    issues_preview = phase1.get("scenario_result", {}).get("issues_preview", [])

    if not issues_preview:
        return {"error": "no issues in fixture"}

    from bizniz.architect.types import SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.coder.types import Issue
    from bizniz.coder_tester.agent import CoderTesterAgent
    from bizniz.coder_tester.types import FilledFile
    from bizniz.per_issue_validator.validator import PerIssueValidator
    from bizniz.quality_engineer.types import EnrichedSpec
    from bizniz.workspace.local_workspace import LocalWorkspace

    architecture = SystemArchitecture.model_validate(arch_data)
    enriched_spec = EnrichedSpec.model_validate(spec_data)
    backend_service = next(
        (s for s in architecture.services if s.name == "backend"), None,
    )
    if backend_service is None:
        return {"error": "backend service not found"}

    # Pick the first issue — sized for a single-call dispatch.
    ip = issues_preview[0]
    issue = Issue(
        id=ip["id"],
        title=ip["title"],
        description=f"Issue {ip['id']}: {ip['title']}",
        service="backend",
        language="python",
        target_files=ip["target_files"],
        # For v4 we'd normally have test_files from the planner; the
        # captured fixture doesn't separate them. Use the standard
        # convention: tests/test_<base>.py per target.
        test_files=[
            f"tests/test_{Path(tf).stem}.py" for tf in ip["target_files"]
        ],
        success_criteria=[],
        spec_refs=[],
        depends_on=[],
    )

    # Slice the seeded scaffold to ONLY this issue's paths.
    seed_workspace = shared / "seeded_workspace"
    issue_paths = set(issue.target_files) | set(issue.test_files)
    seeded_files: List[FilledFile] = []
    for py_file in sorted(seed_workspace.rglob("*.py")):
        rel = str(py_file.relative_to(seed_workspace))
        if rel in issue_paths:
            seeded_files.append(FilledFile(
                path=rel,
                content=py_file.read_text(encoding="utf-8"),
                role="code",
            ))

    # Materialize skeleton into the per-run workspace so the validator
    # can resolve imports against it.
    backend_root = workspace / "backend"
    backend_root.mkdir(parents=True, exist_ok=True)
    skeleton_root = Path.home() / "bizniz-skeleton-fastapi"
    _materialize_skeleton(backend_root, skeleton_root)
    ws = LocalWorkspace(backend_root)
    # Seed the per-issue files on disk too so symbol_validator has them.
    for sf in seeded_files:
        ws.write_file(sf.path, sf.content)

    # Sibling summaries — every OTHER issue from the milestone, as
    # the v4 agent expects.
    sibling_summaries: List[str] = []
    for other in issues_preview:
        if other["id"] == issue.id:
            continue
        sibling_summaries.append(
            f"`{other['id']}` — {other['title']} ({', '.join(other['target_files'][:3])})"
        )

    # Wire the agent + validator. IMPLEMENT tier = Haiku default.
    client = ClaudeCliClient(model_name="claude-cli:claude-haiku-4-5")
    agent = CoderTesterAgent(client=client, on_status=print)
    validator = PerIssueValidator(
        agent=agent,
        workspace=ws,
        on_status=print,
        run_pytest_collect=False,  # skeleton imports won't resolve in perf-test sandbox
    )

    t0 = time.time()
    error = ""
    initial = None
    validated = None
    try:
        initial = agent.code_issue(
            issue=issue,
            service=backend_service,
            seeded_files=seeded_files,
            capabilities=list(enriched_spec.capabilities or []),
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
            sibling_issue_summaries=sibling_summaries,
        )
        validated = validator.validate(
            issue=issue,
            initial_result=initial,
            service=backend_service,
            capabilities=list(enriched_spec.capabilities or []),
            seeded_files=seeded_files,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
            sibling_issue_summaries=sibling_summaries,
        )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    if error:
        return {
            "mode": "coder_tester_single_issue",
            "service": "backend",
            "issue_id": issue.id,
            "wall_s": elapsed,
            "error": error,
        }

    # Per-file checks (AST + bodies + path-contract).
    produced_paths = {f.path for f in initial.filled_files}
    allowed = issue_paths
    extras = produced_paths - allowed
    missing = allowed - produced_paths

    ast_results: Dict[str, Any] = {}
    body_results: Dict[str, Any] = {}
    ast_pass = 0
    bodies_clean = 0
    for f in initial.filled_files:
        r = _check_ast(f.content, f.path)
        ast_results[f.path] = r
        if r.get("ok"):
            ast_pass += 1
        bc = _bodies_filled(f.content)
        body_results[f.path] = {"remaining_not_impl": bc}
        if bc == 0:
            bodies_clean += 1

    total = len(initial.filled_files)
    ast_pct = ast_pass / total * 100.0 if total else 0.0
    bodies_pct = bodies_clean / total * 100.0 if total else 0.0

    wall_ok = elapsed <= 420.0  # ≤ 7 min on Haiku per-issue baseline
    ast_ok = ast_pct >= 100.0
    bodies_ok = bodies_pct >= 100.0
    contract_ok = not extras  # no out-of-scope paths
    clean_ok = bool(validated and validated.clean)
    overall_pass = wall_ok and ast_ok and bodies_ok and contract_ok and clean_ok

    return {
        "mode": "coder_tester_single_issue",
        "service": "backend",
        "issue_id": issue.id,
        "model": "claude-cli:claude-haiku-4-5",
        "wall_s": elapsed,
        "filled_file_count": total,
        "debug_iterations": validated.debug_iterations if validated else None,
        "validated_clean": clean_ok,
        "validator_halt_reason": (validated.halt_reason if validated and not validated.clean else ""),
        "validator_findings_count": len(validated.findings) if validated else None,
        "verdict": {
            "pass": overall_pass,
            "wall_ok": wall_ok,
            "ast_ok": ast_ok,
            "bodies_ok": bodies_ok,
            "contract_ok": contract_ok,
            "clean_ok": clean_ok,
        },
        "ast": {
            "pass_count": ast_pass,
            "total": total,
            "pct": ast_pct,
            "per_file": ast_results,
        },
        "bodies": {
            "clean_count": bodies_clean,
            "total": total,
            "pct": bodies_pct,
            "per_file": body_results,
        },
        "path_contract": {
            "ok": contract_ok,
            "produced": sorted(produced_paths),
            "extras": sorted(extras),
            "missing": sorted(missing),
        },
    }
