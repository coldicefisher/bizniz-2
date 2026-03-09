"""
WorkspaceDB

A thin SQLite wrapper that lives inside a workspace at
    {workspace.root}/.bizniz/bizniz.db

Tables
------
problems              — problem statements submitted to the AutoEngineer
requirements          — business / functional / non-functional requirements
use_cases             — user-facing scenarios derived from a problem
issues                — discrete coding tasks dispatched to the CodingOrchestrator
architecture_plans    — project-level architecture produced by the engineer
architecture_namespaces — package/directory structure within the project
architecture_domain_models — shared types/classes used across modules
architecture_modules  — planned modules with class/function signatures
architecture_dependencies — import edges between modules
test_results          — per-file test pass/fail tracking for regression detection
environment_packages  — pip packages installed in the execution environment
environment_config    — key-value config for the execution environment
"""

import json
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
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id            INTEGER NOT NULL REFERENCES problems(id),
                title                 TEXT    NOT NULL,
                description           TEXT    NOT NULL,
                status                TEXT    NOT NULL DEFAULT 'open'
                                      CHECK(status IN ('open','in_progress','closed')),
                target_files_json     TEXT    NOT NULL DEFAULT '[]',
                test_files_json       TEXT    NOT NULL DEFAULT '[]',
                depends_on_json       TEXT    NOT NULL DEFAULT '[]',
                suggested_model       TEXT,
                created_at            TEXT    NOT NULL,
                closed_at             TEXT
            );

            -- Architecture planning tables

            CREATE TABLE IF NOT EXISTS architecture_plans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id      INTEGER NOT NULL REFERENCES problems(id),
                package_name    TEXT    NOT NULL,
                root_namespace  TEXT    NOT NULL,
                plan_json       TEXT    NOT NULL,
                version         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS architecture_namespaces (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id         INTEGER NOT NULL REFERENCES architecture_plans(id),
                namespace_path  TEXT    NOT NULL,
                purpose         TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,
                UNIQUE(plan_id, namespace_path)
            );

            CREATE TABLE IF NOT EXISTS architecture_domain_models (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id         INTEGER NOT NULL REFERENCES architecture_plans(id),
                namespace_id    INTEGER REFERENCES architecture_namespaces(id),
                class_name      TEXT    NOT NULL,
                filepath        TEXT    NOT NULL,
                definition_json TEXT    NOT NULL,
                is_generated    INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS architecture_modules (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id         INTEGER NOT NULL REFERENCES architecture_plans(id),
                namespace_id    INTEGER REFERENCES architecture_namespaces(id),
                filepath        TEXT    NOT NULL,
                class_name      TEXT,
                definition_json TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL,
                UNIQUE(plan_id, filepath, class_name)
            );

            CREATE TABLE IF NOT EXISTS architecture_dependencies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id         INTEGER NOT NULL REFERENCES architecture_plans(id),
                source_filepath TEXT    NOT NULL,
                target_filepath TEXT    NOT NULL,
                import_symbols  TEXT,
                created_at      TEXT    NOT NULL,
                UNIQUE(plan_id, source_filepath, target_filepath)
            );

            CREATE TABLE IF NOT EXISTS test_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id        INTEGER REFERENCES issues(id),
                test_filepath   TEXT    NOT NULL,
                passed          INTEGER NOT NULL,
                failed          INTEGER NOT NULL,
                total           INTEGER NOT NULL,
                run_at          TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS environment_packages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                package      TEXT    NOT NULL UNIQUE,
                version      TEXT,
                installed_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS environment_config (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT    NOT NULL UNIQUE,
                value       TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
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
        target_files: List[dict],
        test_files: List[str],
        depends_on: Optional[List[int]] = None,
        suggested_model: Optional[str] = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO issues
               (problem_id, title, description, target_files_json, test_files_json, depends_on_json, suggested_model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                problem_id, title, description,
                json.dumps(target_files),
                json.dumps(test_files),
                json.dumps(depends_on or []),
                suggested_model,
                _now(),
            ),
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

    def get_problem_for_issue(self, issue_id: int) -> Optional[str]:
        cur = self._conn.execute(
            """SELECT p.statement FROM problems p
               JOIN issues i ON i.problem_id = p.id
               WHERE i.id = ?""",
            (issue_id,),
        )
        row = cur.fetchone()
        return row["statement"] if row else None

    def get_context_for_code_file(self, code_path: str) -> Optional[dict]:
        """
        Look up the problem statement and architecture context for a code file.
        Returns a dict with 'problem_statement' and 'issue_description', or None.
        """
        # Find issues whose target_files_json contains this filepath
        cur = self._conn.execute(
            "SELECT i.*, p.statement AS problem_statement "
            "FROM issues i JOIN problems p ON i.problem_id = p.id "
            "WHERE i.target_files_json LIKE ?",
            (f'%{code_path}%',),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "problem_statement": row["problem_statement"],
            "issue_description": row["description"],
            "issue_title": row["title"],
        }

    # ── Architecture Plans ──────────────────────────────────────────────────────

    def save_architecture_plan(
        self,
        problem_id: int,
        package_name: str,
        root_namespace: str,
        plan_json: str,
    ) -> int:
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO architecture_plans
               (problem_id, package_name, root_namespace, plan_json, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (problem_id, package_name, root_namespace, plan_json, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_architecture_plan(self, problem_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_plans WHERE problem_id = ? ORDER BY version DESC LIMIT 1",
            (problem_id,),
        )
        return cur.fetchone()

    def update_architecture_plan(self, plan_id: int, plan_json: str):
        self._conn.execute(
            """UPDATE architecture_plans
               SET plan_json = ?, version = version + 1, updated_at = ?
               WHERE id = ?""",
            (plan_json, _now(), plan_id),
        )
        self._conn.commit()

    # ── Architecture Namespaces ─────────────────────────────────────────────────

    def save_namespace(self, plan_id: int, namespace_path: str, purpose: str) -> int:
        cur = self._conn.execute(
            """INSERT INTO architecture_namespaces (plan_id, namespace_path, purpose, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(plan_id, namespace_path) DO UPDATE SET purpose = excluded.purpose""",
            (plan_id, namespace_path, purpose, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_namespaces(self, plan_id: int) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_namespaces WHERE plan_id = ? ORDER BY namespace_path",
            (plan_id,),
        )
        return cur.fetchall()

    # ── Architecture Domain Models ──────────────────────────────────────────────

    def save_domain_model(
        self,
        plan_id: int,
        class_name: str,
        filepath: str,
        definition_json: str,
        namespace_id: Optional[int] = None,
    ) -> int:
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO architecture_domain_models
               (plan_id, namespace_id, class_name, filepath, definition_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, namespace_id, class_name, filepath, definition_json, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_domain_models(self, plan_id: int) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_domain_models WHERE plan_id = ? ORDER BY filepath",
            (plan_id,),
        )
        return cur.fetchall()

    def mark_domain_model_generated(self, model_id: int):
        self._conn.execute(
            "UPDATE architecture_domain_models SET is_generated = 1, updated_at = ? WHERE id = ?",
            (_now(), model_id),
        )
        self._conn.commit()

    # ── Architecture Modules ────────────────────────────────────────────────────

    def save_architecture_module(
        self,
        plan_id: int,
        filepath: str,
        definition_json: str,
        class_name: Optional[str] = None,
        namespace_id: Optional[int] = None,
    ) -> int:
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO architecture_modules
               (plan_id, namespace_id, filepath, class_name, definition_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(plan_id, filepath, class_name) DO UPDATE
               SET definition_json = excluded.definition_json, updated_at = excluded.updated_at""",
            (plan_id, namespace_id, filepath, class_name, definition_json, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_architecture_modules(self, plan_id: int) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_modules WHERE plan_id = ? ORDER BY filepath",
            (plan_id,),
        )
        return cur.fetchall()

    # ── Architecture Dependencies ───────────────────────────────────────────────

    def save_dependency(
        self,
        plan_id: int,
        source_filepath: str,
        target_filepath: str,
        import_symbols: Optional[str] = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO architecture_dependencies
               (plan_id, source_filepath, target_filepath, import_symbols, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(plan_id, source_filepath, target_filepath)
               DO UPDATE SET import_symbols = excluded.import_symbols""",
            (plan_id, source_filepath, target_filepath, import_symbols, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_dependencies(self, plan_id: int) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_dependencies WHERE plan_id = ? ORDER BY source_filepath",
            (plan_id,),
        )
        return cur.fetchall()

    def get_dependencies_for_module(self, plan_id: int, filepath: str) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_dependencies WHERE plan_id = ? AND source_filepath = ?",
            (plan_id, filepath),
        )
        return cur.fetchall()

    # ── Test Results ────────────────────────────────────────────────────────────

    def save_test_result(
        self,
        test_filepath: str,
        passed: int,
        failed: int,
        total: int,
        issue_id: Optional[int] = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO test_results (issue_id, test_filepath, passed, failed, total, run_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (issue_id, test_filepath, passed, failed, total, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_latest_test_results(self, issue_id: Optional[int] = None) -> List[sqlite3.Row]:
        if issue_id is not None:
            cur = self._conn.execute(
                """SELECT * FROM test_results WHERE issue_id = ?
                   ORDER BY run_at DESC""",
                (issue_id,),
            )
        else:
            # Latest result per test file
            cur = self._conn.execute(
                """SELECT tr.* FROM test_results tr
                   INNER JOIN (
                       SELECT test_filepath, MAX(run_at) as max_run
                       FROM test_results GROUP BY test_filepath
                   ) latest ON tr.test_filepath = latest.test_filepath
                              AND tr.run_at = latest.max_run
                   ORDER BY tr.test_filepath"""
            )
        return cur.fetchall()

    def get_passing_test_files(self) -> List[str]:
        """Return all test files whose latest run had zero failures."""
        cur = self._conn.execute(
            """SELECT tr.test_filepath FROM test_results tr
               INNER JOIN (
                   SELECT test_filepath, MAX(run_at) as max_run
                   FROM test_results GROUP BY test_filepath
               ) latest ON tr.test_filepath = latest.test_filepath
                          AND tr.run_at = latest.max_run
               WHERE tr.failed = 0"""
        )
        return [row["test_filepath"] for row in cur.fetchall()]

    # ── Environment Packages ─────────────────────────────────────────────────────

    def save_package(self, package: str, version: Optional[str] = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO environment_packages (package, version, installed_at)
               VALUES (?, ?, ?)
               ON CONFLICT(package) DO UPDATE SET version = excluded.version, installed_at = excluded.installed_at""",
            (package, version, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_packages(self) -> List[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM environment_packages ORDER BY package")
        return cur.fetchall()

    def remove_package(self, package: str):
        self._conn.execute("DELETE FROM environment_packages WHERE package = ?", (package,))
        self._conn.commit()

    # ── Environment Config ────────────────────────────────────────────────────

    def get_config(self, key: str) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT value FROM environment_config WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else None

    def set_config(self, key: str, value: str):
        self._conn.execute(
            """INSERT INTO environment_config (key, value, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, created_at = excluded.created_at""",
            (key, value, _now()),
        )
        self._conn.commit()

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
