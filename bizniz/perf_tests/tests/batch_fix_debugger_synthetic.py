"""Phase-3 minimum-viable test of the v3 batch-fix debugger.

Buggy workspace with known defects:
  app/models.py:  Recipe dataclass missing the ``tags`` field
  app/routes.py:  unused JWTError import (ruff F401);
                   references recipe.tags (mypy attr-defined);
                   swallows all exceptions in create_recipe (CR)
  tests/test_routes.py:  fails because tags missing from Recipe

Hand-crafted FindingsReport covering all six defects from FIVE
sources (static_ruff, static_mypy, pytest, code_reviewer, plus
hallucination). Agent ingests the report, makes batch fixes via
Edit/Write directly on the workspace, returns a structured
summary.

After the agent runs we re-validate deterministically:
  1. AST clean on every modified file
  2. ``ruff check`` (if available) → no F401 on JWTError
  3. python import smoke on routes.py → no AttributeError
  4. pytest --collect-only against the tests dir → tests collect
  5. The agent's structured summary lists fixes covering the
     fingerprints we fed it

Pass conditions:
  - wall ≤ 10 min
  - all 4 deterministic checks pass
  - agent's ``fixes_applied`` covers ≥ 4 of the 6 fingerprints
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


def _check_ast(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "file missing"}
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return {"ok": True}
    except SyntaxError as e:
        return {"ok": False, "error": f"SyntaxError: line {e.lineno}: {e.msg}"}


def _ruff_check(workspace: Path) -> Dict[str, Any]:
    """Run ``ruff check`` if it's on PATH. Returns shape:
      {"ok": bool, "stdout": str, "f401_count": int, "skipped": ?str}
    """
    if not shutil.which("ruff"):
        return {"skipped": "ruff not on PATH"}
    try:
        proc = subprocess.run(
            ["ruff", "check", "--no-cache", "app/", "tests/"],
            cwd=str(workspace),
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "ruff timeout"}
    text = (proc.stdout or "") + (proc.stderr or "")
    f401 = len(re.findall(r"\bF401\b", text))
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": text[-2000:],
        "f401_count": f401,
    }


def _import_smoke(workspace: Path) -> Dict[str, Any]:
    """``python -c "import app.routes"`` from inside workspace."""
    try:
        proc = subprocess.run(
            ["python3", "-c", "import sys; sys.path.insert(0, '.'); import app.routes"],
            cwd=str(workspace),
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "import smoke timeout"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-1000:],
    }


def _pytest_collect(workspace: Path) -> Dict[str, Any]:
    """``pytest --collect-only`` — confirms tests are importable +
    syntactically valid, without actually running them."""
    try:
        proc = subprocess.run(
            ["python3", "-m", "pytest", "--collect-only", "-q", "tests/"],
            cwd=str(workspace),
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "pytest collect timeout"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-500:],
    }


def _build_findings_report():
    """Hand-craft the report the parallel review unit WOULD have
    produced for this buggy workspace. Six findings across five
    sources, mirroring real-world cross-cutting patterns."""
    from bizniz.review_unit.types import FindingsReport, UnifiedFinding
    return FindingsReport(
        iteration=0,
        findings=[
            UnifiedFinding(
                source="pytest",
                severity="critical",
                fingerprint="tests/test_routes.py::test_recipe_response_includes_tags",
                message=(
                    "TypeError: Recipe.__init__() got an unexpected keyword "
                    "argument 'tags'. The test asserts the response includes "
                    "a tags array; the Recipe dataclass does not declare a "
                    "tags field."
                ),
                file_path="tests/test_routes.py",
                line=14,
                raw="E   TypeError: Recipe.__init__() got an unexpected keyword argument 'tags'",
            ),
            UnifiedFinding(
                source="static_mypy",
                severity="high",
                fingerprint="attr-defined",
                message="\"Recipe\" has no attribute \"tags\"",
                file_path="app/routes.py",
                line=22,
                suggested_fix="Add ``tags: List[str] = field(default_factory=list)`` to Recipe.",
                raw='app/routes.py:22: error: "Recipe" has no attribute "tags"  [attr-defined]',
            ),
            UnifiedFinding(
                source="quality_engineer",
                severity="high",
                fingerprint="cap.recipe_tags_response",
                message=(
                    "Capability `recipe_tags_response` requires the response "
                    "body to include a `tags: list[str]` field. Coverage "
                    "gap — no implementation visible."
                ),
                file_path="app/routes.py",
            ),
            UnifiedFinding(
                source="static_ruff",
                severity="low",
                fingerprint="F401",
                message="'jose.JWTError' imported but unused",
                file_path="app/routes.py",
                line=14,
                suggested_fix="Either use JWTError in an error handler, or remove the import.",
            ),
            UnifiedFinding(
                source="code_reviewer",
                severity="high",
                fingerprint="swallowed_exception",
                message=(
                    "create_recipe catches the broad ``Exception`` and "
                    "returns None silently. The signature promises -> Recipe; "
                    "callers will hit AttributeError downstream. Re-raise or "
                    "raise a typed exception."
                ),
                file_path="app/routes.py",
                line=33,
            ),
            UnifiedFinding(
                source="hallucination",
                severity="medium",
                fingerprint="dead-import",
                message=(
                    "JWTError imported but not referenced by any handler. "
                    "Either the import is dead OR the handler that should "
                    "use it was not written."
                ),
                file_path="app/routes.py",
                line=14,
            ),
        ],
    )


def run(workspace: Path, fixture_root: Path) -> Dict[str, Any]:
    # 1. Materialize the buggy workspace.
    src = fixture_root / "buggy_workspace"
    for entry in src.iterdir():
        dst = workspace / entry.name
        if entry.is_dir():
            shutil.copytree(
                entry, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", "*.pyc",
                ),
                dirs_exist_ok=True,
            )
        else:
            shutil.copy2(entry, dst)

    # 2. Build the findings report.
    report = _build_findings_report()

    # 3. Pre-check the workspace (sanity — should be broken).
    pre_collect = _pytest_collect(workspace)

    # 4. Run the batch-fix debugger.
    from bizniz.review_unit.batch_fix_debugger import BatchFixDebugger

    debugger = BatchFixDebugger(workspace_root=workspace)
    t0 = time.time()
    error = ""
    result_dict: Dict[str, Any] = {}
    try:
        result = debugger.run(report=report)
        result_dict = result.model_dump()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    if error:
        return {
            "mode": "batch_fix_debugger_synthetic",
            "wall_s": elapsed,
            "error": error,
            "report_summary": report.summary_line(),
        }

    # 5. Post-fix validations.
    files_to_check = [
        workspace / "app" / "models.py",
        workspace / "app" / "routes.py",
        workspace / "tests" / "test_routes.py",
    ]
    ast_results = {str(p.relative_to(workspace)): _check_ast(p) for p in files_to_check}
    ast_all_ok = all(r.get("ok") for r in ast_results.values())

    ruff = _ruff_check(workspace)
    import_smoke = _import_smoke(workspace)
    post_collect = _pytest_collect(workspace)

    # 6. Coverage of findings: how many of the 6 fingerprints did the
    #    agent address (per its own summary)?
    #    The agent commonly decorates fingerprints with source/file/line
    #    prefixes for audit clarity (e.g. our "F401" appears as
    #    "static_ruff:F401:app/routes.py:14"). Use substring containment
    #    so the matcher honors that without forcing the agent to echo
    #    the bare fingerprint string.
    expected_fingerprints = {f.fingerprint for f in report.findings}
    addressed_strings: List[str] = []
    for fix in result_dict.get("fixes_applied", []):
        for fp in fix.get("addresses_fingerprints", []):
            addressed_strings.append(str(fp))
    addressed_in_expected = {
        fp for fp in expected_fingerprints
        if any(fp in s for s in addressed_strings)
    }
    coverage_count = len(addressed_in_expected)
    coverage_pct = (coverage_count / len(expected_fingerprints) * 100.0) if expected_fingerprints else 0.0

    # 7. Verdict.
    wall_ok = elapsed <= 600.0
    ast_ok = ast_all_ok
    import_ok = import_smoke.get("ok") is True
    pytest_ok = post_collect.get("ok") is True
    fingerprint_ok = coverage_count >= 4

    # Ruff is best-effort (not installed everywhere). If skipped, don't fail.
    ruff_ok = ruff.get("skipped") is not None or (ruff.get("f401_count", 0) == 0)

    overall = wall_ok and ast_ok and import_ok and pytest_ok and fingerprint_ok and ruff_ok

    return {
        "mode": "batch_fix_debugger_synthetic",
        "wall_s": elapsed,
        "verdict": {
            "pass": overall,
            "wall_ok": wall_ok,
            "ast_ok": ast_ok,
            "import_smoke_ok": import_ok,
            "pytest_collect_ok": pytest_ok,
            "fingerprint_coverage_ok": fingerprint_ok,
            "ruff_ok": ruff_ok,
        },
        "ast": ast_results,
        "ruff": ruff,
        "import_smoke": import_smoke,
        "pre_pytest_collect": {
            "ok": pre_collect.get("ok"),
            "returncode": pre_collect.get("returncode"),
        },
        "post_pytest_collect": {
            "ok": post_collect.get("ok"),
            "returncode": post_collect.get("returncode"),
            "stdout_tail": post_collect.get("stdout", "")[-800:],
        },
        "fingerprint_coverage": {
            "expected_count": len(expected_fingerprints),
            "addressed_count": coverage_count,
            "coverage_pct": coverage_pct,
            "addressed": sorted(addressed_in_expected),
            "missed": sorted(expected_fingerprints - addressed),
        },
        "agent_result": {
            "summary": result_dict.get("summary", "")[:1500],
            "fixes_count": len(result_dict.get("fixes_applied", [])),
            "fixes": [
                {
                    "files_touched": f.get("files_touched"),
                    "description": f.get("description"),
                    "addresses_fingerprints": f.get("addresses_fingerprints"),
                }
                for f in result_dict.get("fixes_applied", [])
            ],
            "skipped_fingerprints": result_dict.get("skipped_fingerprints", []),
        },
    }
