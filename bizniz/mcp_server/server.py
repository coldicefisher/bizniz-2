"""bizniz_mcp — MCP server exposing cross-issue context to Claude.

Claude CLI launches this as a subprocess (via ``--mcp-config``) and
calls into it during a Coder session. The tools below give Claude
on-demand access to:

  - prior issues' status + last test output (cross-issue memory the
    Coder's narrow context intentionally excludes)
  - the deterministic Python symbol validator (so Claude can
    self-check imports/attributes before declaring done)
  - the milestone's review findings (so Claude can see what the
    CodeReviewer already flagged and avoid repeating those patterns)
  - the auth contract on disk (FA endpoints, test users, password
    rules — already pre-injected into the prompt but exposed here
    too in case Claude wants to re-fetch)

The server reads two env vars:

  BIZNIZ_PROJECT_ROOT  absolute path to the project (where AUTH_CONTRACT.md
                       and .bizniz/project.db live)
  BIZNIZ_JOB_ID        the run job_id (used to find docs/runs/<job>/
                       artifacts). Optional — if missing, the server
                       finds the newest run dir.

ClaudeCliCoder writes a temp mcp-config.json that launches this
server with those env vars set per-issue.

Why narrow-context-by-default + MCP-on-demand (Architecture C in
the pluggable backend plan): the narrow context keeps the
hallucination firewall intact; the MCP lets Claude pull cross-issue
knowledge when it needs to, without paying for a wider default
prompt every call.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

# FastMCP is the high-level decorator-based server in the official
# Python MCP SDK. See https://github.com/modelcontextprotocol/python-sdk
from mcp.server.fastmcp import FastMCP


_PROJECT_ROOT_ENV = "BIZNIZ_PROJECT_ROOT"
_JOB_ID_ENV = "BIZNIZ_JOB_ID"


def _project_root() -> Path:
    """Resolve the project root from env or fall back to CWD."""
    val = os.environ.get(_PROJECT_ROOT_ENV)
    if val:
        return Path(val).expanduser().resolve()
    return Path.cwd().resolve()


def _runs_root(project_root: Path) -> Path:
    return project_root / "docs" / "runs"


def _resolve_run_dir(project_root: Path) -> Optional[Path]:
    """Either ``BIZNIZ_JOB_ID``'s dir, or the newest run dir, or None."""
    jid = os.environ.get(_JOB_ID_ENV)
    rr = _runs_root(project_root)
    if not rr.exists():
        return None
    if jid:
        candidate = rr / jid
        if candidate.exists():
            return candidate
    runs = sorted(
        [p for p in rr.iterdir() if p.is_dir()],
        key=lambda p: p.name, reverse=True,
    )
    return runs[0] if runs else None


# ── Server ──────────────────────────────────────────────────────────────


mcp = FastMCP("bizniz")


@mcp.tool(
    name="get_prior_issues",
    description=(
        "List issues for a milestone with their status, title, and "
        "files written. Returns a list of dicts. Use this to see what "
        "other issues in the current milestone have already done so "
        "you don't duplicate or contradict their work. ``milestone`` "
        "defaults to 1 (M1)."
    ),
)
def get_prior_issues(milestone: int = 1, service: Optional[str] = None) -> list[dict]:
    """Query coder_issues DB rows for ``milestone``. Optionally filter
    by ``service`` (e.g. ``backend``, ``frontend``)."""
    root = _project_root()
    db_path = root / ".bizniz" / "project.db"
    if not db_path.exists():
        return [{"error": f"project DB not found at {db_path}"}]
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if service:
            cur = conn.execute(
                "SELECT issue_id, title, status, target_files, test_files, service "
                "FROM coder_issues WHERE milestone_index=? AND service=? "
                "ORDER BY issue_index",
                (milestone, service),
            )
        else:
            cur = conn.execute(
                "SELECT issue_id, title, status, target_files, test_files, service "
                "FROM coder_issues WHERE milestone_index=? ORDER BY issue_index",
                (milestone,),
            )
        rows = cur.fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        out.append({
            "issue_id": r["issue_id"],
            "title": r["title"],
            "status": r["status"],
            "service": r["service"],
            "target_files": json.loads(r["target_files"] or "[]"),
            "test_files": json.loads(r["test_files"] or "[]"),
        })
    return out


@mcp.tool(
    name="get_issue_test_output",
    description=(
        "Return the tail of the last pytest output for an issue (the "
        "``last_test_output_tail`` field stored when the issue finished). "
        "Useful when you're debugging a test that depends on something "
        "another issue's tests already covered."
    ),
)
def get_issue_test_output(issue_id: str, milestone: int = 1) -> dict:
    """Return ``{issue_id, status, output}`` for ``issue_id`` in
    ``milestone``. Returns ``{error}`` if not found."""
    root = _project_root()
    db_path = root / ".bizniz" / "project.db"
    if not db_path.exists():
        return {"error": f"project DB not found at {db_path}"}
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT issue_id, status, last_test_output_tail "
            "FROM coder_issues WHERE issue_id=? AND milestone_index=?",
            (issue_id, milestone),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"error": f"issue {issue_id} not found in milestone {milestone}"}
    return {
        "issue_id": row["issue_id"],
        "status": row["status"],
        "output": row["last_test_output_tail"] or "",
    }


@mcp.tool(
    name="validate_python_imports",
    description=(
        "Run the deterministic AST-walk validator on one or more Python "
        "files. Catches hallucinated imports + attribute access on "
        "known classes (``settings.foo_bar`` when only ``foo_baz`` "
        "exists). Returns a structured report. CALL THIS before you "
        "declare an issue done — much cheaper than running tests just "
        "to discover an unresolved import."
    ),
)
def validate_python_imports(file_paths: list[str]) -> dict:
    """Run ``bizniz.coder.symbol_validator.validate_files`` over the
    given workspace-relative paths. Resolves them under
    ``BIZNIZ_PROJECT_ROOT``."""
    root = _project_root()
    # Inserting the bizniz repo path lets the validator import. We
    # assume the env's PYTHONPATH already has bizniz; otherwise the
    # MCP server itself wouldn't have started.
    from bizniz.coder.symbol_validator import validate_files
    abs_paths = []
    for p in file_paths:
        # Project-rooted paths (``backend/app/foo.py``) or workspace-
        # relative (``app/foo.py``) — try project root first, then
        # the parent of the file if it exists.
        candidate = (root / p).resolve()
        if candidate.exists():
            abs_paths.append(candidate)
        else:
            return {"error": f"file not found: {p} (looked under {root})"}
    if not abs_paths:
        return {"error": "no file paths given"}
    # Workspace root for the validator: use the deepest common
    # ancestor of all paths, falling back to project root.
    common = abs_paths[0].parent
    while not all(str(p).startswith(str(common)) for p in abs_paths):
        if common == common.parent:
            common = root
            break
        common = common.parent
    report = validate_files(abs_paths, common)
    return {
        "passed": report.passed,
        "rendered": report.render(),
        "unresolved_imports": [
            {"file": str(u.file), "line": u.line, "symbol": u.symbol,
             "reason": u.reason}
            for u in report.unresolved
        ],
        "unresolved_attributes": [
            {"file": a.file, "line": a.line, "var": a.var,
             "class": a.class_name, "attribute": a.attribute,
             "available": a.available[:20]}
            for a in report.unresolved_attributes
        ],
    }


@mcp.tool(
    name="read_audit_findings",
    description=(
        "Return the latest QualityEngineer + CodeReviewer findings for "
        "a milestone (read from review_initial.json or review_final.json). "
        "Use this to avoid re-introducing patterns the reviewer already "
        "flagged. Returns ``{coverage, code_review}`` or ``{error}``."
    ),
)
def read_audit_findings(milestone: int = 1) -> dict:
    """Read the most recent review artifact for ``milestone``. Tries
    ``review_final.json`` first, falls back to ``review_initial.json``."""
    root = _project_root()
    run_dir = _resolve_run_dir(root)
    if run_dir is None:
        return {"error": "no run dir found under docs/runs/"}
    ms_dir = run_dir / f"m{milestone}"
    for name in ("review_final.json", "review_initial.json"):
        p = ms_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception as e:
                return {"error": f"could not parse {name}: {e}"}
    return {"error": f"no review artifact found in {ms_dir}"}


@mcp.tool(
    name="read_auth_contract",
    description=(
        "Return the project's AUTH_CONTRACT.md. The contract documents "
        "FusionAuth endpoints, test users, password policy, JWT "
        "validation, and the registration pitfall. Use this when "
        "you're writing code that touches login, register, role-change, "
        "or JWT validation flows."
    ),
)
def read_auth_contract() -> dict:
    """Return ``{contract: str}`` or ``{error: str}``."""
    root = _project_root()
    p = root / "AUTH_CONTRACT.md"
    if not p.exists():
        return {"error": f"AUTH_CONTRACT.md not at {p}"}
    return {"contract": p.read_text()}


def main() -> int:
    """Stdio entrypoint. Claude CLI launches us via ``--mcp-config``."""
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
