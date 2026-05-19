"""Phase-1 validation of the v3 pipeline spec: can ServicePlanner emit a
real-code seeded scaffold alongside its issue specs?

Inputs are captured from recipe_v3_opus's M1 run (architect + enrich +
auth contract + skeleton.md), so we're testing on actual production
data — not a synthetic toy.

Pass conditions (per v3 spec docs/architecture/v3_pipeline_spec.md):
  1. **AST pass rate** — every seeded file parses cleanly. Target: 100%.
  2. **Symbol-validator pass rate** — every seeded Python file resolves
     imports + attribute access against the seeded workspace. Target: ≥80%
     (some cross-file references may not resolve until ALL seeded files
     are written to disk together; we'll see).
  3. **Target-file coverage** — every issue's ``target_files`` has a
     corresponding ``seeded_files`` entry. Target: 100%.
  4. **Wall cost** — ServicePlannerWithScaffold completes within 2× the
     production ServicePlanner baseline (~3 min). Target: ≤6 min.

Pass = (1) ∧ (3) ∧ (4) AND (2) ≥ 80%. If pass, the seeded-scaffold
idea is proven and the spec's phase 2 (CoderAgent + TesterAgent) is
worth building.

Runs on the backend service only (the hard case — 12 issues in
production). Frontend would be a follow-up validation.
"""
from __future__ import annotations

import ast
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _check_ast(content: str, label: str) -> Dict[str, Any]:
    try:
        tree = ast.parse(content, filename=label)
    except SyntaxError as e:
        return {
            "ok": False,
            "error": f"SyntaxError: line {e.lineno} col {e.offset}: {e.msg}",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level.append(node.name)
    return {"ok": True, "top_level_defs": top_level}


def _materialize_seed(
    workspace: Path,
    seeded_files: List[Dict],
    skeleton_root: Path,
) -> None:
    """Realistic seed materialization: copy the FastAPI skeleton into
    workspace first (so requirements.txt + skeleton-shipped files
    exist), then layer the seeded scaffold on top. This mirrors what
    happens in production — the seed lands on a provisioned
    skeleton-based workspace, not bare ground."""
    if skeleton_root.exists():
        import shutil as _shutil
        for entry in skeleton_root.iterdir():
            dst = workspace / entry.name
            if entry.is_dir():
                _shutil.copytree(
                    entry, dst,
                    ignore=_shutil.ignore_patterns(
                        "__pycache__", ".pytest_cache", "*.pyc", ".git",
                    ),
                    dirs_exist_ok=True,
                )
            else:
                _shutil.copy2(entry, dst)
    for sf in seeded_files:
        rel = sf["path"]
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(sf["content"], encoding="utf-8")


def _check_symbols(target_path: Path, workspace_root: Path) -> Dict[str, Any]:
    try:
        from bizniz.coder.symbol_validator import validate_python_file
    except Exception as e:
        return {"skipped": f"import error: {type(e).__name__}: {e}"}
    try:
        report = validate_python_file(
            file_path=target_path, workspace_root=workspace_root,
        )
    except Exception as e:
        return {"skipped": f"validator raised: {type(e).__name__}: {e}"}
    return {
        "passed": report.passed,
        "resolved_count": report.resolved_count,
        "unresolved_count": len(report.unresolved),
        "unresolved_attr_count": len(report.unresolved_attributes),
        "syntax_error_count": len(report.syntax_errors),
        "unresolved_first_5": [
            f"line {u.line} [{u.kind}] {u.symbol}: {u.reason}"
            for u in report.unresolved[:5]
        ],
    }


def _coverage(issues: List[Dict], seeded_files: List[Dict]) -> Dict[str, Any]:
    """Verify every issue's target_files has a matching seeded file."""
    seeded_paths = {sf["path"] for sf in seeded_files}
    missing: Dict[str, List[str]] = {}
    covered = 0
    total_targets = 0
    for issue in issues:
        iid = issue.get("id", "?")
        for tf in issue.get("target_files", []):
            total_targets += 1
            if tf in seeded_paths:
                covered += 1
            else:
                missing.setdefault(iid, []).append(tf)
    return {
        "total_target_files": total_targets,
        "covered": covered,
        "missing_by_issue": missing,
        "coverage_pct": (
            (covered / total_targets * 100.0) if total_targets else 0.0
        ),
    }


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # 1. Load captured inputs.
    arch_data = json.loads((fixture_root / "architecture.json").read_text())
    spec_data = json.loads((fixture_root / "enriched_spec.json").read_text())
    skeleton_md = (fixture_root / "skeleton.md").read_text()
    auth_contract = (fixture_root / "auth_contract.md").read_text()

    # 2. Lazy imports — keep perf_tests/__init__.py import-light.
    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.quality_engineer.types import EnrichedSpec
    from bizniz.service_planner.scaffolded import ServicePlannerWithScaffold

    architecture = SystemArchitecture.model_validate(arch_data)
    enriched_spec = EnrichedSpec.model_validate(spec_data)
    backend_service = next(
        (s for s in architecture.services if s.name == "backend"),
        None,
    )
    if backend_service is None:
        return {"error": "backend service not found in captured architecture"}

    # 3. Dispatch — Opus per the v3 spec's tier assignment.
    client = ClaudeCliClient(model_name="claude-cli:claude-opus-4-7")
    planner = ServicePlannerWithScaffold(client=client)

    t0 = time.time()
    error = ""
    issues_data: List[Dict] = []
    seeded_data: List[Dict] = []
    try:
        result = planner.plan_service(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=backend_service,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )
        issues_data = [i.model_dump() for i in result.issues]
        seeded_data = [s.model_dump() for s in result.seeded_files]
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    if error:
        return {
            "mode": "service_planner_seeded_scaffold",
            "service": "backend",
            "wall_s": elapsed,
            "error": error,
        }

    # 4. AST check every seeded file.
    ast_results: Dict[str, Any] = {}
    ast_pass = 0
    for sf in seeded_data:
        r = _check_ast(sf["content"], sf["path"])
        ast_results[sf["path"]] = r
        if r.get("ok"):
            ast_pass += 1
    ast_pct = ast_pass / len(seeded_data) * 100.0 if seeded_data else 0.0

    # 5. Materialize the seed onto a real skeleton-based workspace so
    #    symbol_validator can resolve external deps via requirements.txt
    #    and skeleton-shipped symbols.
    backend_root = workspace / "backend"
    backend_root.mkdir(parents=True, exist_ok=True)
    skeleton_root = Path.home() / "bizniz-skeleton-fastapi"
    _materialize_seed(backend_root, seeded_data, skeleton_root)

    # 6. Symbol check every Python file in the seed.
    symbol_results: Dict[str, Any] = {}
    symbol_pass = 0
    py_files = [sf for sf in seeded_data if sf["path"].endswith(".py")]
    for sf in py_files:
        rel = sf["path"]
        r = _check_symbols(backend_root / rel, backend_root)
        symbol_results[rel] = r
        if r.get("passed"):
            symbol_pass += 1
    symbol_pct = symbol_pass / len(py_files) * 100.0 if py_files else 0.0

    # 7. Coverage: every issue's target_files appear in seeded paths.
    coverage = _coverage(issues_data, seeded_data)

    # 8. Pass verdict per spec.
    wall_ok = elapsed <= 360.0  # ≤ 6 min target
    ast_ok = ast_pct >= 100.0
    symbol_ok = symbol_pct >= 80.0
    coverage_ok = coverage["coverage_pct"] >= 100.0
    overall_pass = wall_ok and ast_ok and coverage_ok and symbol_ok

    return {
        "mode": "service_planner_seeded_scaffold",
        "service": "backend",
        "wall_s": elapsed,
        "model": "claude-cli:claude-opus-4-7",
        "issue_count": len(issues_data),
        "seeded_file_count": len(seeded_data),
        "py_file_count": len(py_files),
        "verdict": {
            "pass": overall_pass,
            "wall_ok": wall_ok,
            "ast_ok": ast_ok,
            "symbol_ok": symbol_ok,
            "coverage_ok": coverage_ok,
        },
        "ast": {
            "pass_count": ast_pass,
            "pass_pct": ast_pct,
            "results": ast_results,
        },
        "symbols": {
            "pass_count": symbol_pass,
            "pass_pct": symbol_pct,
            "results": symbol_results,
        },
        "coverage": coverage,
        "issues_preview": [
            {
                "id": i["id"], "title": i["title"],
                "target_files": i["target_files"],
            }
            for i in issues_data
        ],
        "seeded_files_preview": [
            {
                "path": sf["path"],
                "bytes": len(sf["content"]),
                "rationale": sf.get("rationale", "")[:200],
            }
            for sf in seeded_data
        ],
    }
