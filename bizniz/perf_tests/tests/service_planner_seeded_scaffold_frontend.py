"""Frontend variant of the seeded-scaffold validation.

Same test shape as ``service_planner_seeded_scaffold`` but targets the
react/typescript frontend service to confirm cross-language generality.

AST validation for TypeScript uses ``tsc --noEmit`` against the seed +
skeleton workspace. Symbol-validator doesn't apply (Python-only); we
substitute a TS compile-clean check.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


def _check_ts_compile(workspace: Path) -> Dict[str, Any]:
    """Run ``tsc --noEmit`` over the seeded + skeleton workspace and
    classify the output. We use the project's own tsconfig.json so the
    same compile that runs in production is what gates here."""
    tsconfig = workspace / "tsconfig.json"
    if not tsconfig.exists():
        return {
            "skipped": "no tsconfig.json in workspace — cannot run tsc",
        }
    try:
        proc = subprocess.run(
            ["npx", "--yes", "tsc", "--noEmit", "-p", str(tsconfig)],
            capture_output=True, text=True, timeout=180,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "tsc timed out after 180s"}
    except FileNotFoundError:
        return {"skipped": "npx/tsc not found on PATH"}
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "error_count": output.count("error TS"),
        "output_tail": output[-3000:],
    }


def _coverage(issues: List[Dict], seeded_files: List[Dict]) -> Dict[str, Any]:
    seeded_paths = {sf["path"] for sf in seeded_files}
    missing: Dict[str, List[str]] = {}
    covered = 0
    total = 0
    for issue in issues:
        iid = issue.get("id", "?")
        for tf in issue.get("target_files", []):
            total += 1
            if tf in seeded_paths:
                covered += 1
            else:
                missing.setdefault(iid, []).append(tf)
    return {
        "total_target_files": total,
        "covered": covered,
        "missing_by_issue": missing,
        "coverage_pct": (covered / total * 100.0) if total else 0.0,
    }


def _materialize_seed(
    workspace: Path,
    seeded_files: List[Dict],
    skeleton_root: Path,
) -> None:
    if skeleton_root.exists():
        for entry in skeleton_root.iterdir():
            dst = workspace / entry.name
            if entry.is_dir():
                shutil.copytree(
                    entry, dst,
                    ignore=shutil.ignore_patterns(
                        "node_modules", "dist", ".vite", "*.log",
                    ),
                    dirs_exist_ok=True,
                )
            else:
                shutil.copy2(entry, dst)
    for sf in seeded_files:
        rel = sf["path"]
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(sf["content"], encoding="utf-8")


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    arch_data = json.loads((fixture_root / "architecture.json").read_text())
    spec_data = json.loads((fixture_root / "enriched_spec.json").read_text())
    skeleton_md = (fixture_root / "skeleton.md").read_text()
    auth_contract = (fixture_root / "auth_contract.md").read_text()

    from bizniz.architect.types import ServiceDefinition, SystemArchitecture
    from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
    from bizniz.quality_engineer.types import EnrichedSpec
    from bizniz.service_planner.scaffolded import ServicePlannerWithScaffold

    architecture = SystemArchitecture.model_validate(arch_data)
    enriched_spec = EnrichedSpec.model_validate(spec_data)
    frontend_service = next(
        (s for s in architecture.services if s.name == "frontend"),
        None,
    )
    if frontend_service is None:
        return {"error": "frontend service not found in captured architecture"}

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
            service=frontend_service,
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
            "mode": "service_planner_seeded_scaffold_frontend",
            "service": "frontend",
            "wall_s": elapsed,
            "error": error,
        }

    # Materialize the seed onto the react skeleton.
    frontend_root = workspace / "frontend"
    frontend_root.mkdir(parents=True, exist_ok=True)
    skeleton_root = Path.home() / "bizniz-skeleton-react"
    _materialize_seed(frontend_root, seeded_data, skeleton_root)

    # Coverage check (same as backend).
    coverage = _coverage(issues_data, seeded_data)

    # TS compile check (substitute for AST + symbol on backend).
    # ``tsc --noEmit`` catches both syntax errors AND type-resolution
    # errors in one pass — better than splitting into separate gates
    # for TypeScript.
    ts_compile = _check_ts_compile(frontend_root)

    wall_ok = elapsed <= 360.0
    ts_ok = ts_compile.get("ok") is True
    coverage_ok = coverage["coverage_pct"] >= 100.0
    overall_pass = wall_ok and ts_ok and coverage_ok

    return {
        "mode": "service_planner_seeded_scaffold_frontend",
        "service": "frontend",
        "wall_s": elapsed,
        "model": "claude-cli:claude-opus-4-7",
        "issue_count": len(issues_data),
        "seeded_file_count": len(seeded_data),
        "verdict": {
            "pass": overall_pass,
            "wall_ok": wall_ok,
            "ts_compile_ok": ts_ok,
            "coverage_ok": coverage_ok,
        },
        "ts_compile": ts_compile,
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
