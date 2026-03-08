"""
WorkspaceDB

A thin SQLite wrapper that lives inside a workspace at
    {workspace.root}/.bizniz/bizniz.db

Tables
------
problems         — problem statements submitted to the AutoEngineer
requirements     — business / functional / non-functional requirements
use_cases        — user-facing scenarios derived from a problem
issues           — discrete coding tasks dispatched to the CodingOrchestrator
"""

import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List

from bizniz.workspace.base_workspace import BaseWorkspace


class WorkspaceDB:

    def __init__(self, workspace: BaseWorkspace):
        db_dir = workspace.root / ".bizniz"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "bizniz.db"
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    # ── Schema ──────────────────────────────────────────────────────────────────

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS problems (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                statement   TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS requirements (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id  INTEGER NOT NULL REFERENCES problems(id),
                type        TEXT    NOT NULL CHECK(type IN ('business','functional','nonfunctional')),
                text        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS use_cases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id  INTEGER NOT NULL REFERENCES problems(id),
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issues (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id  INTEGER NOT NULL REFERENCES problems(id),
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open'
                                    CHECK(status IN ('open','in_progress','closed')),
                code_file   TEXT    NOT NULL,
                test_file   TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                closed_at   TEXT
            );
        """)
        self._conn.commit()

    # ── Problems ────────────────────────────────────────────────────────────────

    def save_problem(self, statement: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO problems (statement, created_at) VALUES (?, ?)",
            (statement, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_problem(self, problem_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM problems WHERE id = ?", (problem_id,)
        )
        return cur.fetchone()

    # ── Requirements ────────────────────────────────────────────────────────────

    def save_requirement(self, problem_id: int, req_type: str, text: str) -> int:
        """req_type must be 'business', 'functional', or 'nonfunctional'."""
        cur = self._conn.execute(
            "INSERT INTO requirements (problem_id, type, text, created_at) VALUES (?, ?, ?, ?)",
            (problem_id, req_type, text, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_requirements(self, problem_id: int, req_type: Optional[str] = None) -> List[sqlite3.Row]:
        if req_type:
            cur = self._conn.execute(
                "SELECT * FROM requirements WHERE problem_id = ? AND type = ?",
                (problem_id, req_type),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM requirements WHERE problem_id = ?", (problem_id,)
            )
        return cur.fetchall()

    # ── Use Cases ───────────────────────────────────────────────────────────────

    def save_use_case(self, problem_id: int, title: str, description: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO use_cases (problem_id, title, description, created_at) VALUES (?, ?, ?, ?)",
            (problem_id, title, description, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_use_cases(self, problem_id: int) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM use_cases WHERE problem_id = ?", (problem_id,)
        )
        return cur.fetchall()

    # ── Issues ──────────────────────────────────────────────────────────────────

    def save_issue(
        self,
        problem_id: int,
        title: str,
        description: str,
        code_file: str,
        test_file: str,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO issues
               (problem_id, title, description, code_file, test_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (problem_id, title, description, code_file, test_file, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_issue(self, issue_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM issues WHERE id = ?", (issue_id,)
        )
        return cur.fetchone()

    def get_open_issues(self, problem_id: Optional[int] = None) -> List[sqlite3.Row]:
        if problem_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM issues WHERE problem_id = ? AND status = 'open'",
                (problem_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM issues WHERE status = 'open'"
            )
        return cur.fetchall()

    def update_issue_status(self, issue_id: int, status: str):
        self._conn.execute(
            "UPDATE issues SET status = ? WHERE id = ?", (status, issue_id)
        )
        self._conn.commit()

    def close_issue(self, issue_id: int):
        self._conn.execute(
            "UPDATE issues SET status = 'closed', closed_at = ? WHERE id = ?",
            (_now(), issue_id),
        )
        self._conn.commit()

    def get_issue_by_code_file(self, code_file: str) -> Optional[sqlite3.Row]:
        """Look up an issue by its code_file path."""
        cur = self._conn.execute(
            "SELECT * FROM issues WHERE code_file = ? ORDER BY id DESC LIMIT 1",
            (code_file,),
        )
        return cur.fetchone()

    def get_problem_for_issue(self, issue_id: int) -> Optional[str]:
        """Return the problem statement associated with an issue."""
        cur = self._conn.execute(
            """SELECT p.statement FROM problems p
               JOIN issues i ON i.problem_id = p.id
               WHERE i.id = ?""",
            (issue_id,),
        )
        row = cur.fetchone()
        return row["statement"] if row else None

    def get_context_for_code_file(self, code_file: str) -> Optional[dict]:
        """
        Look up an issue by code_file and return its full context:
        problem_statement, issue description, title, and test_file.
        Returns None if no matching issue exists.
        """
        cur = self._conn.execute(
            """SELECT i.id, i.title, i.description, i.code_file, i.test_file,
                      p.statement as problem_statement
               FROM issues i
               JOIN problems p ON i.problem_id = p.id
               WHERE i.code_file = ?
               ORDER BY i.id DESC LIMIT 1""",
            (code_file,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
