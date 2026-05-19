"""Phase 2c — v3 pipeline scaling validation at production milestone scope.

Chains ``ServicePlannerWithScaffold`` → ``CoderAgentV3`` on captured
recipe_v2 M3 inputs (Tags + search + filter, ~10 backend parent issues
in production — Decomposer expanded to 24+ units, but we test the
un-decomposed shape per the v3 spec which dropped Decomposer).

This is the load-bearing scaling test: Phase 2a validated single-dispatch
on a 7-issue milestone. Does it hold on a 10-issue production-class
milestone with denser cross-file dependencies (tags + recipes + repos +
routes + schemas all interlocked)?

Steps:
  1. ServicePlannerWithScaffold on M3 backend → issues + seeded scaffold
  2. Validate scaffold: AST 100%, coverage 100%, wall ≤ 8 min
  3. CoderAgentV3 fills the scaffold → filled files
  4. Materialize on FastAPI skeleton workspace
  5. Validate fill: AST 100%, symbol ≥ 80%, coverage 100%, bodies 100%,
     no drift, wall ≤ 20 min

Combined pass = both steps pass. Combined wall budget: ≤ 25 min.
Compare to recipe_v2 M3's production wall (Decomposer-on path consumed
hours per the prior decomposer A/B data).
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Set


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
        "unresolved_first_3": [
            f"line {u.line} [{u.kind}] {u.symbol}: {u.reason}"
            for u in report.unresolved[:3]
        ],
    }


def _bodies_filled_count(content: str) -> int:
    return len(re.findall(r"raise\s+NotImplementedError\b", content))


def _drift_check(seed_content: str, filled_content: str) -> Dict[str, Any]:
    try:
        seed_tree = ast.parse(seed_content)
        filled_tree = ast.parse(filled_content)
    except SyntaxError:
        return {"skipped": "ast parse failed"}
    def names(tree):
        n: Set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                n.add(node.name)
        return n
    seed_n = names(seed_tree)
    filled_n = names(filled_tree)
    removed = seed_n - filled_n
    return {
        "no_drift": len(removed) == 0,
        "removed": sorted(removed),
        "added": sorted(filled_n - seed_n),
    }


def _coverage(issues: List[Dict], filled_paths: Set[str]) -> Dict[str, Any]:
    missing: Dict[str, List[str]] = {}
    covered = 0
    total = 0
    for issue in issues:
        iid = issue.get("id", "?")
        for tf in issue.get("target_files", []):
            total += 1
            if tf in filled_paths:
                covered += 1
            else:
                missing.setdefault(iid, []).append(tf)
    return {
        "total": total,
        "covered": covered,
        "missing_by_issue": missing,
        "coverage_pct": (covered / total * 100.0) if total else 0.0,
    }


def _materialize(workspace: Path, files: List[Dict], skeleton_root: Path) -> None:
    if skeleton_root.exists():
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
    for f in files:
        dest = workspace / f["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # 1. Load fixture.
    arch_data = json.loads((fixture_root / "architecture.json").read_text())
    spec_data = json.loads((fixture_root / "enriched_spec.json").read_text())
    skeleton_md = (fixture_root / "skeleton.md").read_text()
    auth_contract = (fixture_root / "auth_contract.md").read_text()

    from bizniz.architect.types import SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.coder.agent_v3 import CoderAgentV3, FilledFile
    from bizniz.quality_engineer.types import EnrichedSpec
    from bizniz.service_planner.scaffolded import ServicePlannerWithScaffold

    architecture = SystemArchitecture.model_validate(arch_data)
    enriched_spec = EnrichedSpec.model_validate(spec_data)
    backend = next(
        (s for s in architecture.services if s.name == "backend"), None,
    )
    if backend is None:
        return {"error": "backend service not found"}

    # ── STEP 1: ServicePlannerWithScaffold ────────────────────────
    client = ClaudeCliClient(model_name="claude-cli:claude-opus-4-7")
    planner = ServicePlannerWithScaffold(client=client)

    t_planner_start = time.time()
    planner_error = ""
    issues_data: List[Dict] = []
    seeded_data: List[Dict] = []
    try:
        plan_result = planner.plan_service(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=backend,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )
        issues_data = [i.model_dump() for i in plan_result.issues]
        seeded_data = [s.model_dump() for s in plan_result.seeded_files]
    except Exception as e:
        planner_error = f"{type(e).__name__}: {e}"
    planner_wall = time.time() - t_planner_start

    if planner_error:
        return {
            "mode": "v3_chain_recipe_v2_m3",
            "stage": "service_planner",
            "planner_wall_s": planner_wall,
            "error": planner_error,
        }

    # Step 1 validation.
    planner_ast_pass = 0
    planner_ast_results: Dict[str, Any] = {}
    for sf in seeded_data:
        r = _check_ast(sf["content"], sf["path"])
        planner_ast_results[sf["path"]] = r
        if r.get("ok"):
            planner_ast_pass += 1
    planner_ast_pct = planner_ast_pass / len(seeded_data) * 100.0 if seeded_data else 0.0
    planner_cov = _coverage(issues_data, {sf["path"] for sf in seeded_data})

    planner_pass = (
        planner_wall <= 480.0
        and planner_ast_pct >= 100.0
        and planner_cov["coverage_pct"] >= 100.0
    )

    # ── STEP 2: CoderAgentV3 fills the scaffold ───────────────────
    from bizniz.coder.types import Issue
    issues = [Issue.model_validate(i) for i in issues_data]
    seeded_files = [FilledFile(path=sf["path"], content=sf["content"]) for sf in seeded_data]

    agent = CoderAgentV3(client=client)
    t_coder_start = time.time()
    coder_error = ""
    filled: List[Dict] = []
    try:
        fill_result = agent.fill_milestone(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=backend,
            issues=issues,
            seeded_files=seeded_files,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )
        filled = [ff.model_dump() for ff in fill_result.filled_files]
    except Exception as e:
        coder_error = f"{type(e).__name__}: {e}"
    coder_wall = time.time() - t_coder_start

    if coder_error:
        return {
            "mode": "v3_chain_recipe_v2_m3",
            "stage": "coder_agent",
            "planner_wall_s": planner_wall,
            "coder_wall_s": coder_wall,
            "planner_pass": planner_pass,
            "issue_count": len(issues),
            "seeded_file_count": len(seeded_data),
            "error": coder_error,
        }

    # Step 2 validation.
    backend_root = workspace / "backend"
    backend_root.mkdir(parents=True, exist_ok=True)
    skeleton_root = Path.home() / "bizniz-skeleton-fastapi"
    _materialize(backend_root, filled, skeleton_root)

    coder_ast_pass = 0
    coder_ast_results: Dict[str, Any] = {}
    bodies_clean = 0
    body_per_file: Dict[str, int] = {}
    no_drift = 0
    drift_eligible = 0
    drift_per_file: Dict[str, Any] = {}
    seed_by_path = {sf["path"]: sf["content"] for sf in seeded_data}
    for ff in filled:
        path = ff["path"]
        content = ff["content"]
        r = _check_ast(content, path)
        coder_ast_results[path] = r
        if r.get("ok"):
            coder_ast_pass += 1
        body_count = _bodies_filled_count(content)
        body_per_file[path] = body_count
        if body_count == 0:
            bodies_clean += 1
        if path in seed_by_path:
            drift_eligible += 1
            d = _drift_check(seed_by_path[path], content)
            drift_per_file[path] = d
            if d.get("no_drift"):
                no_drift += 1

    coder_ast_pct = coder_ast_pass / len(filled) * 100.0 if filled else 0.0
    bodies_pct = bodies_clean / len(filled) * 100.0 if filled else 0.0
    drift_ok = (no_drift == drift_eligible) if drift_eligible else True

    symbol_pass = 0
    symbol_results: Dict[str, Any] = {}
    py_files = [ff for ff in filled if ff["path"].endswith(".py")]
    for ff in py_files:
        r = _check_symbols(backend_root / ff["path"], backend_root)
        symbol_results[ff["path"]] = r
        if r.get("passed"):
            symbol_pass += 1
    symbol_pct = symbol_pass / len(py_files) * 100.0 if py_files else 0.0

    coder_cov = _coverage(issues_data, {ff["path"] for ff in filled})

    coder_pass = (
        coder_wall <= 1200.0
        and coder_ast_pct >= 100.0
        and symbol_pct >= 80.0
        and coder_cov["coverage_pct"] >= 100.0
        and bodies_pct >= 100.0
        and drift_ok
    )

    combined_wall = planner_wall + coder_wall
    combined_pass = planner_pass and coder_pass and (combined_wall <= 1500.0)

    return {
        "mode": "v3_chain_recipe_v2_m3",
        "service": "backend",
        "model": "claude-cli:claude-opus-4-7",
        "combined_wall_s": combined_wall,
        "issue_count": len(issues),
        "seeded_file_count": len(seeded_data),
        "filled_file_count": len(filled),
        "verdict": {
            "combined_pass": combined_pass,
            "planner_pass": planner_pass,
            "coder_pass": coder_pass,
        },
        "planner": {
            "wall_s": planner_wall,
            "ast_pass_pct": planner_ast_pct,
            "coverage_pct": planner_cov["coverage_pct"],
            "ast_results": planner_ast_results,
            "coverage": planner_cov,
        },
        "coder": {
            "wall_s": coder_wall,
            "ast_pass_pct": coder_ast_pct,
            "symbol_pass_pct": symbol_pct,
            "coverage_pct": coder_cov["coverage_pct"],
            "bodies_clean_pct": bodies_pct,
            "drift_ok": drift_ok,
            "ast_results": coder_ast_results,
            "symbol_results": symbol_results,
            "coverage": coder_cov,
            "bodies": body_per_file,
            "drift": drift_per_file,
        },
        "issues_preview": [
            {"id": i.id, "title": i.title, "target_files": i.target_files}
            for i in issues
        ],
        "seeded_files_preview": [
            {"path": sf["path"], "bytes": len(sf["content"])}
            for sf in seeded_data
        ],
        "filled_files_preview": [
            {"path": ff["path"], "bytes": len(ff["content"])}
            for ff in filled
        ],
    }
