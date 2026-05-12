"""Unit tests for bizniz.mcp_server.server tool functions.

The MCP protocol layer is exercised end-to-end via Claude CLI in
functional tests; here we test the tool implementations as pure
Python functions against a fake project layout.
"""
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bizniz.mcp_server import server


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """Build a minimal project layout the tools can read against."""
    root = tmp_path / "myproject"
    root.mkdir()
    (root / ".bizniz").mkdir()
    (root / "docs" / "runs" / "20260512_120000" / "m1").mkdir(parents=True)
    (root / "AUTH_CONTRACT.md").write_text("# Auth Contract\nstub\n")

    # Build a coder_issues table the tools can query.
    db = root / ".bizniz" / "project.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE coder_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT, milestone_index INTEGER, service TEXT,
            issue_id TEXT, issue_index INTEGER DEFAULT 0,
            title TEXT, description TEXT DEFAULT '',
            language TEXT DEFAULT 'python',
            target_files TEXT DEFAULT '[]',
            test_files TEXT DEFAULT '[]',
            spec_refs TEXT DEFAULT '[]',
            depends_on TEXT DEFAULT '[]',
            success_criteria TEXT DEFAULT '[]',
            status TEXT DEFAULT 'pending',
            tiers_used TEXT DEFAULT '[]',
            last_test_output_tail TEXT DEFAULT ''
        );
    """)
    conn.executemany(
        "INSERT INTO coder_issues "
        "(job_id, milestone_index, service, issue_id, issue_index, title, "
        " target_files, test_files, status, last_test_output_tail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("j", 1, "backend", "BE-001", 0, "Make user model",
             '["app/user.py"]', '["tests/test_user.py"]', "passed",
             "TESTS PASSED\n2 passed"),
            ("j", 1, "backend", "BE-002", 1, "Make auth route",
             '["app/auth.py"]', '["tests/test_auth.py"]', "escalated",
             "TESTS PASSED\n5 passed"),
            ("j", 1, "frontend", "FE-001", 0, "Login form",
             '["src/Login.tsx"]', '[]', "running", ""),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv(server._PROJECT_ROOT_ENV, str(root))
    monkeypatch.setenv(server._JOB_ID_ENV, "20260512_120000")
    return root


class TestGetPriorIssues:
    def test_returns_all_for_milestone(self, project_root):
        rows = server.get_prior_issues(milestone=1)
        assert len(rows) == 3
        ids = {r["issue_id"] for r in rows}
        assert ids == {"BE-001", "BE-002", "FE-001"}

    def test_service_filter(self, project_root):
        rows = server.get_prior_issues(milestone=1, service="backend")
        assert len(rows) == 2
        assert all(r["service"] == "backend" for r in rows)

    def test_returns_status_and_files(self, project_root):
        rows = server.get_prior_issues(milestone=1, service="backend")
        be001 = next(r for r in rows if r["issue_id"] == "BE-001")
        assert be001["status"] == "passed"
        assert be001["target_files"] == ["app/user.py"]
        assert be001["test_files"] == ["tests/test_user.py"]

    def test_missing_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(server._PROJECT_ROOT_ENV, str(tmp_path))
        rows = server.get_prior_issues(milestone=1)
        assert len(rows) == 1
        assert "error" in rows[0]


class TestGetIssueTestOutput:
    def test_returns_output_tail(self, project_root):
        out = server.get_issue_test_output("BE-001")
        assert out["status"] == "passed"
        assert "TESTS PASSED" in out["output"]

    def test_missing_issue_returns_error(self, project_root):
        out = server.get_issue_test_output("DOES-NOT-EXIST")
        assert "error" in out


class TestReadAuthContract:
    def test_reads_contract(self, project_root):
        out = server.read_auth_contract()
        assert "Auth Contract" in out["contract"]

    def test_missing_contract_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(server._PROJECT_ROOT_ENV, str(tmp_path))
        out = server.read_auth_contract()
        assert "error" in out


class TestReadAuditFindings:
    def test_returns_review_initial(self, project_root):
        review = {"coverage": {"approved": True}, "code_review": {}}
        (project_root / "docs" / "runs" / "20260512_120000" /
         "m1" / "review_initial.json").write_text(json.dumps(review))
        out = server.read_audit_findings(milestone=1)
        assert out["coverage"]["approved"] is True

    def test_prefers_review_final(self, project_root):
        ms = project_root / "docs" / "runs" / "20260512_120000" / "m1"
        (ms / "review_initial.json").write_text(json.dumps({"phase": "initial"}))
        (ms / "review_final.json").write_text(json.dumps({"phase": "final"}))
        out = server.read_audit_findings(milestone=1)
        assert out["phase"] == "final"

    def test_no_artifact_returns_error(self, project_root):
        out = server.read_audit_findings(milestone=1)
        assert "error" in out


class TestValidatePythonImports:
    def test_clean_file_passes(self, project_root):
        (project_root / "good.py").write_text("import os\nprint(os.getcwd())\n")
        out = server.validate_python_imports(["good.py"])
        assert out["passed"] is True
        assert "PASSED" in out["rendered"]

    def test_unresolved_import_flagged(self, project_root):
        (project_root / "bad.py").write_text("import not_a_real_package\n")
        out = server.validate_python_imports(["bad.py"])
        assert out["passed"] is False
        assert out["unresolved_imports"]
        assert "not_a_real_package" in out["rendered"]

    def test_missing_file_returns_error(self, project_root):
        out = server.validate_python_imports(["does_not_exist.py"])
        assert "error" in out
