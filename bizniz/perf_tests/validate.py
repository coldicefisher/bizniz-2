"""Quality validation helpers for perf-test outputs.

Standalone — runs against any file on disk, so it can be applied
LIVE during a test scenario OR post-hoc against a completed run's
artifact. Two checks today:

1. **AST parse** — does Python compile to AST? Catches silent
   syntax-error failures where the Coder claimed success but
   produced malformed code.

2. **Symbol validation** — runs the existing
   ``bizniz.coder.symbol_validator`` against the file, scoped to
   the run's workspace as the resolution root. Catches
   hallucinated imports, attribute access on classes that don't
   define the attr, etc.

Both checks are deterministic + cheap. Future additions (test-
suite runs, AST diff against a reference) can layer on top
without changing this API.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def validate_output(
    target_path: Path,
    workspace_root: Path,
) -> Dict[str, Any]:
    """Run AST + symbol-validator checks on a produced file.

    Returns a flat dict suitable for embedding in a test
    scenario's return value or a post-hoc result.json
    augmentation.
    """
    out: Dict[str, Any] = {
        "target_path": str(target_path),
        "exists": target_path.exists(),
    }

    if not target_path.exists():
        out["ast_parse"] = {"ok": False, "error": "file not found"}
        out["symbol_validation"] = {"skipped": "file not found"}
        return out

    try:
        source = target_path.read_text(encoding="utf-8")
    except Exception as e:
        out["ast_parse"] = {
            "ok": False, "error": f"read error: {type(e).__name__}: {e}",
        }
        out["symbol_validation"] = {"skipped": "read error"}
        return out

    out["file_bytes"] = len(source)
    out["line_count"] = source.count("\n") + 1

    # AST parse.
    ast_result = _check_ast(source, str(target_path))
    out["ast_parse"] = ast_result

    if not ast_result.get("ok"):
        out["symbol_validation"] = {"skipped": "syntax error blocks symbol check"}
        return out

    # Symbol validation.
    out["symbol_validation"] = _check_symbols(target_path, workspace_root)
    return out


def _check_ast(source: str, label: str) -> Dict[str, Any]:
    """Parse ``source`` with Python's ast module. Returns
    ``{ok: bool, error?: str, top_level_defs?: List[str]}``."""
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as e:
        return {
            "ok": False,
            "error": f"SyntaxError: line {e.lineno} col {e.offset}: {e.msg}",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top_level.append(t.id)
    return {
        "ok": True,
        "top_level_defs": top_level,
    }


def _check_symbols(
    target_path: Path, workspace_root: Path,
) -> Dict[str, Any]:
    """Run bizniz's existing symbol_validator. Returns a
    summary-friendly dict."""
    try:
        from bizniz.coder.symbol_validator import validate_python_file
    except Exception as e:
        return {"skipped": f"import error: {type(e).__name__}: {e}"}

    try:
        report = validate_python_file(
            file_path=target_path,
            workspace_root=workspace_root,
        )
    except Exception as e:
        return {"skipped": f"validator raised: {type(e).__name__}: {e}"}

    return {
        "passed": report.passed,
        "resolved_count": report.resolved_count,
        "unresolved_count": len(report.unresolved),
        "unresolved_attribute_count": len(report.unresolved_attributes),
        "syntax_error_count": len(report.syntax_errors),
        "unresolved_first_5": [
            f"line {u.line} [{u.kind}] {u.symbol}: {u.reason}"
            for u in report.unresolved[:5]
        ],
        "unresolved_attribute_first_5": [
            f"line {a.line} {a.var}.{a.attribute}"
            for a in report.unresolved_attributes[:5]
        ],
    }


# ── Post-hoc CLI ─────────────────────────────────────────────────


def validate_existing_run(
    run_dir: Path, target_relpath: str = "app/api/routes/recipes.py",
) -> Dict[str, Any]:
    """Apply ``validate_output`` to a completed run's workspace.

    ``run_dir`` should be the per-run directory (the one containing
    ``result.json`` + ``workspace/``). ``target_relpath`` is the
    file under workspace/ to validate.

    Augments result.json with a new ``quality`` block and writes
    it back. Returns the new ``quality`` block.
    """
    result_path = run_dir / "result.json"
    if not result_path.exists():
        return {"error": f"no result.json at {result_path}"}
    workspace = run_dir / "workspace"
    target = workspace / target_relpath
    quality = validate_output(target, workspace)

    existing = json.loads(result_path.read_text())
    existing["quality"] = quality
    result_path.write_text(json.dumps(existing, indent=2, default=str))
    return quality
