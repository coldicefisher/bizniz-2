"""Phase-2a validation of the v3 pipeline spec: can CoderAgentV3 fill
all milestone issues in one single dispatch against the seeded scaffold?

Inputs (captured from Phase 1's recipe_v3 M1 backend run):
  - architecture.json, enriched_spec.json, skeleton.md, auth_contract.md
  - phase1_result.json (Phase 1's issues + seeded paths/preview)
  - seeded_workspace/  (Phase 1's actual scaffold files on disk)

Pass conditions (per v3 spec docs/architecture/v3_pipeline_spec.md):
  1. **Wall** ≤ 15 min. (Per-issue baseline ≈ 3 min × 7 issues = 21 min;
     we need to beat that. Production Opus baseline 1h 35m for 12 issues.)
  2. **AST pass rate** = 100% on every filled file.
  3. **Symbol-validator pass rate** ≥ 80% against the skeleton-rooted
     workspace.
  4. **Coverage** = 100% — every issue's target_files appears in the
     filled_files output.
  5. **Bodies filled** = 100% — zero remaining ``raise NotImplementedError``
     across filled files (the seed used these as stubs).
  6. **Contract drift** = none — top-level function signatures, class
     names, and route registrations from the seed appear unchanged in
     the filled output. (Heuristic check: AST function/class name set
     ⊆ seed's set, no removals.)

Pass = (1) ∧ (2) ∧ (4) ∧ (5) ∧ (6), AND (3) ≥ 80%.

If pass, Phase 2a is green-lit. Phase 2b (TesterAgent in isolation,
when we formally split code/test writers) and Phase 2c (parallel
composition) follow.
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
        "unresolved_first_5": [
            f"line {u.line} [{u.kind}] {u.symbol}: {u.reason}"
            for u in report.unresolved[:5]
        ],
    }


def _bodies_filled(content: str) -> int:
    """Count remaining NotImplementedError instances. Target: 0."""
    return len(re.findall(r"raise\s+NotImplementedError\b", content))


def _drift_check(seed_content: str, filled_content: str) -> Dict[str, Any]:
    """Heuristic: every top-level function/class in the seed must
    appear in the filled file. (Extras are fine — new helpers.)"""
    try:
        seed_tree = ast.parse(seed_content)
        filled_tree = ast.parse(filled_content)
    except SyntaxError:
        return {"skipped": "ast parse failed on one side"}
    def names(tree):
        n: Set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                n.add(node.name)
        return n
    seed_names = names(seed_tree)
    filled_names = names(filled_tree)
    removed = seed_names - filled_names
    added = filled_names - seed_names
    return {
        "no_drift": len(removed) == 0,
        "removed": sorted(removed),
        "added": sorted(added),
        "seed_count": len(seed_names),
        "filled_count": len(filled_names),
    }


def _coverage(
    issues: List[Dict],
    filled_paths: Set[str],
) -> Dict[str, Any]:
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
        "total_target_files": total,
        "covered": covered,
        "missing_by_issue": missing,
        "coverage_pct": (covered / total * 100.0) if total else 0.0,
    }


def _materialize(
    workspace: Path,
    filled_files: List[Dict],
    skeleton_root: Path,
) -> None:
    """Layer filled files onto the skeleton workspace."""
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
    for ff in filled_files:
        dest = workspace / ff["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(ff["content"], encoding="utf-8")


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # 1. Load captured Phase 1 inputs + outputs.
    arch_data = json.loads((fixture_root / "architecture.json").read_text())
    spec_data = json.loads((fixture_root / "enriched_spec.json").read_text())
    skeleton_md = (fixture_root / "skeleton.md").read_text()
    auth_contract = (fixture_root / "auth_contract.md").read_text()

    phase1 = json.loads((fixture_root / "phase1_result.json").read_text())
    phase1_scenario = phase1.get("scenario_result", {})
    issue_specs = phase1_scenario.get("issues_preview", [])

    # Load full issue specs (we have only previews in result.json — the
    # full Issue objects need to be reconstructed. For this test we
    # rebuild from the previews + the issue's structure as our prompt
    # is permissive about which fields are present).
    from bizniz.coder.types import Issue
    issues = []
    for ip in issue_specs:
        issues.append(Issue(
            id=ip["id"],
            title=ip["title"],
            description=f"Issue {ip['id']}: {ip['title']}",
            service="backend",
            language="python",
            target_files=ip["target_files"],
            test_files=[],
            success_criteria=[],
            spec_refs=[],
            depends_on=[],
        ))

    # Seeded files: read from disk under the fixture's seeded_workspace/.
    seed_workspace = fixture_root / "seeded_workspace"
    from bizniz.coder.agent_v3 import FilledFile
    seeded_files: List[FilledFile] = []
    for py_file in sorted(seed_workspace.rglob("*.py")):
        rel = py_file.relative_to(seed_workspace)
        seeded_files.append(FilledFile(
            path=str(rel),
            content=py_file.read_text(encoding="utf-8"),
        ))

    # 2. Imports + setup.
    from bizniz.architect.types import SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.coder.agent_v3 import CoderAgentV3
    from bizniz.quality_engineer.types import EnrichedSpec

    architecture = SystemArchitecture.model_validate(arch_data)
    enriched_spec = EnrichedSpec.model_validate(spec_data)
    backend_service = next(
        (s for s in architecture.services if s.name == "backend"), None,
    )
    if backend_service is None:
        return {"error": "backend service not found"}

    client = ClaudeCliClient(model_name="claude-cli:claude-opus-4-7")
    agent = CoderAgentV3(client=client)

    t0 = time.time()
    error = ""
    filled: List[Dict] = []
    try:
        result = agent.fill_milestone(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=backend_service,
            issues=issues,
            seeded_files=seeded_files,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )
        filled = [ff.model_dump() for ff in result.filled_files]
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    if error:
        return {
            "mode": "coder_agent_single_dispatch",
            "service": "backend",
            "wall_s": elapsed,
            "error": error,
        }

    # 3. AST + bodies + drift per filled file.
    ast_results: Dict[str, Any] = {}
    body_results: Dict[str, Any] = {}
    drift_results: Dict[str, Any] = {}
    ast_pass = 0
    bodies_clean = 0
    no_drift = 0
    drift_eligible = 0
    seed_by_path = {sf.path: sf.content for sf in seeded_files}
    for ff in filled:
        path = ff["path"]
        content = ff["content"]
        # AST
        r = _check_ast(content, path)
        ast_results[path] = r
        if r.get("ok"):
            ast_pass += 1
        # Bodies (count remaining NotImplementedError)
        body_count = _bodies_filled(content)
        body_results[path] = {"remaining_not_impl": body_count}
        if body_count == 0:
            bodies_clean += 1
        # Drift (only on files that were in the seed)
        if path in seed_by_path:
            drift_eligible += 1
            d = _drift_check(seed_by_path[path], content)
            drift_results[path] = d
            if d.get("no_drift"):
                no_drift += 1

    # 4. Materialize onto skeleton + symbol-validator.
    backend_root = workspace / "backend"
    backend_root.mkdir(parents=True, exist_ok=True)
    skeleton_root = Path.home() / "bizniz-skeleton-fastapi"
    _materialize(backend_root, filled, skeleton_root)

    symbol_results: Dict[str, Any] = {}
    symbol_pass = 0
    py_files = [ff for ff in filled if ff["path"].endswith(".py")]
    for ff in py_files:
        rel = ff["path"]
        r = _check_symbols(backend_root / rel, backend_root)
        symbol_results[rel] = r
        if r.get("passed"):
            symbol_pass += 1
    symbol_pct = symbol_pass / len(py_files) * 100.0 if py_files else 0.0

    # 5. Coverage.
    filled_paths_set = {ff["path"] for ff in filled}
    coverage = _coverage(
        [i.model_dump() for i in issues], filled_paths_set,
    )

    # 6. Verdict.
    ast_pct = ast_pass / len(filled) * 100.0 if filled else 0.0
    bodies_pct = bodies_clean / len(filled) * 100.0 if filled else 0.0
    drift_ok = (no_drift == drift_eligible) if drift_eligible else True

    wall_ok = elapsed <= 900.0  # ≤ 15 min
    ast_ok = ast_pct >= 100.0
    symbol_ok = symbol_pct >= 80.0
    coverage_ok = coverage["coverage_pct"] >= 100.0
    bodies_ok = bodies_pct >= 100.0
    overall_pass = wall_ok and ast_ok and coverage_ok and bodies_ok and drift_ok and symbol_ok

    return {
        "mode": "coder_agent_single_dispatch",
        "service": "backend",
        "wall_s": elapsed,
        "model": "claude-cli:claude-opus-4-7",
        "issue_count": len(issues),
        "seeded_file_count": len(seeded_files),
        "filled_file_count": len(filled),
        "verdict": {
            "pass": overall_pass,
            "wall_ok": wall_ok,
            "ast_ok": ast_ok,
            "symbol_ok": symbol_ok,
            "coverage_ok": coverage_ok,
            "bodies_ok": bodies_ok,
            "drift_ok": drift_ok,
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
        "bodies": {
            "all_clean_count": bodies_clean,
            "all_clean_pct": bodies_pct,
            "per_file": body_results,
        },
        "drift": {
            "no_drift_count": no_drift,
            "eligible_count": drift_eligible,
            "per_file": drift_results,
        },
        "coverage": coverage,
        "filled_files_preview": [
            {
                "path": ff["path"], "bytes": len(ff["content"]),
            }
            for ff in filled
        ],
    }
