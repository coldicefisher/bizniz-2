"""
WorkspaceScope — Scoped accessor for workspace-level data.

Drop-in replacement for WorkspaceDB, backed by the unified BiznizDB.
All queries are automatically scoped to (project_id, service_name).
"""

import json
from typing import Optional, List

from bizniz.db.bizniz_db import _now

if False:  # TYPE_CHECKING
    from bizniz.db.bizniz_db import BiznizDB


class WorkspaceScope:

    def __init__(self, db: "BiznizDB", project_id: str, service_name: str):
        self._db = db
        self._project_id = project_id
        self._service_name = service_name

    def _execute(self, sql, params=()):
        return self._db._execute(sql, params)

    def _commit(self):
        self._db._commit()

    # ── Problems ──────────────────────────────────────────────────────────────────

    def save_problem(self, statement: str) -> int:
        cur = self._execute(
            "INSERT INTO problems (project_id, service_name, statement, created_at) VALUES (?, ?, ?, ?)",
            (self._project_id, self._service_name, statement, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_problem(self, problem_id: int):
        cur = self._execute(
            "SELECT * FROM problems WHERE id = ? AND project_id = ? AND service_name = ?",
            (problem_id, self._project_id, self._service_name),
        )
        return cur.fetchone()

    # ── Requirements ──────────────────────────────────────────────────────────────

    def save_requirement(self, problem_id: int, req_type: str, text: str) -> int:
        cur = self._execute(
            """INSERT INTO requirements
               (project_id, service_name, problem_id, type, text, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (self._project_id, self._service_name, problem_id, req_type, text, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_requirements(self, problem_id: int, req_type: Optional[str] = None):
        if req_type:
            cur = self._execute(
                "SELECT * FROM requirements WHERE problem_id = ? AND project_id = ? AND service_name = ? AND type = ?",
                (problem_id, self._project_id, self._service_name, req_type),
            )
        else:
            cur = self._execute(
                "SELECT * FROM requirements WHERE problem_id = ? AND project_id = ? AND service_name = ?",
                (problem_id, self._project_id, self._service_name),
            )
        return cur.fetchall()

    # ── Use Cases ─────────────────────────────────────────────────────────────────

    def save_use_case(self, problem_id: int, title: str, description: str) -> int:
        cur = self._execute(
            """INSERT INTO use_cases
               (project_id, service_name, problem_id, title, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (self._project_id, self._service_name, problem_id, title, description, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_use_cases(self, problem_id: int):
        cur = self._execute(
            "SELECT * FROM use_cases WHERE problem_id = ? AND project_id = ? AND service_name = ?",
            (problem_id, self._project_id, self._service_name),
        )
        return cur.fetchall()

    # ── Issues ────────────────────────────────────────────────────────────────────

    def save_issue(
        self,
        problem_id: int,
        title: str,
        description: str,
        target_files: List[dict],
        test_files: List[str],
        depends_on: Optional[list] = None,
        suggested_model: Optional[str] = None,
        test_setup_hint: str = "",
    ) -> int:
        cur = self._execute(
            """INSERT INTO issues
               (project_id, service_name, problem_id, title, description,
                target_files_json, test_files_json, depends_on_json, suggested_model,
                test_setup_hint, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._project_id, self._service_name,
                problem_id, title, description,
                json.dumps(target_files),
                json.dumps(test_files),
                json.dumps(depends_on or []),
                suggested_model,
                test_setup_hint,
                _now(),
            ),
        )
        self._commit()
        return cur.lastrowid

    def get_issue(self, issue_id: int):
        cur = self._execute(
            "SELECT * FROM issues WHERE id = ? AND project_id = ? AND service_name = ?",
            (issue_id, self._project_id, self._service_name),
        )
        return cur.fetchone()

    def get_open_issues(self, problem_id: Optional[int] = None):
        if problem_id is not None:
            cur = self._execute(
                "SELECT * FROM issues WHERE problem_id = ? AND project_id = ? AND service_name = ? AND status = 'open'",
                (problem_id, self._project_id, self._service_name),
            )
        else:
            cur = self._execute(
                "SELECT * FROM issues WHERE project_id = ? AND service_name = ? AND status = 'open'",
                (self._project_id, self._service_name),
            )
        return cur.fetchall()

    def update_issue_depends_on(self, issue_id: int, depends_on: List[int]):
        self._execute(
            "UPDATE issues SET depends_on_json = ? WHERE id = ? AND project_id = ? AND service_name = ?",
            (json.dumps(depends_on), issue_id, self._project_id, self._service_name),
        )
        self._commit()

    def update_issue_status(self, issue_id: int, status: str):
        self._execute(
            "UPDATE issues SET status = ? WHERE id = ? AND project_id = ? AND service_name = ?",
            (status, issue_id, self._project_id, self._service_name),
        )
        self._commit()

    def close_issue(self, issue_id: int):
        self._execute(
            "UPDATE issues SET status = 'closed', closed_at = ? WHERE id = ? AND project_id = ? AND service_name = ?",
            (_now(), issue_id, self._project_id, self._service_name),
        )
        self._commit()

    def get_problem_for_issue(self, issue_id: int) -> Optional[str]:
        cur = self._execute(
            """SELECT p.statement FROM problems p
               JOIN issues i ON i.problem_id = p.id
                    AND i.project_id = p.project_id
                    AND i.service_name = p.service_name
               WHERE i.id = ? AND i.project_id = ? AND i.service_name = ?""",
            (issue_id, self._project_id, self._service_name),
        )
        row = cur.fetchone()
        return row["statement"] if row else None

    def get_context_for_code_file(self, code_path: str) -> Optional[dict]:
        cur = self._execute(
            """SELECT i.*, p.statement AS problem_statement
               FROM issues i
               JOIN problems p ON i.problem_id = p.id
                    AND i.project_id = p.project_id
                    AND i.service_name = p.service_name
               WHERE i.target_files_json LIKE ?
                 AND i.project_id = ? AND i.service_name = ?""",
            (f'%{code_path}%', self._project_id, self._service_name),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "problem_statement": row["problem_statement"],
            "issue_description": row["description"],
            "issue_title": row["title"],
        }

    # ── Architecture Plans ────────────────────────────────────────────────────────

    def save_architecture_plan(
        self, problem_id: int, package_name: str, root_namespace: str, plan_json: str,
    ) -> int:
        now = _now()
        cur = self._execute(
            """INSERT INTO architecture_plans
               (project_id, service_name, problem_id, package_name, root_namespace,
                plan_json, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (self._project_id, self._service_name, problem_id, package_name, root_namespace, plan_json, now, now),
        )
        self._commit()
        return cur.lastrowid

    def get_architecture_plan(self, problem_id: int):
        cur = self._execute(
            """SELECT * FROM architecture_plans
               WHERE problem_id = ? AND project_id = ? AND service_name = ?
               ORDER BY version DESC LIMIT 1""",
            (problem_id, self._project_id, self._service_name),
        )
        return cur.fetchone()

    def update_architecture_plan(self, plan_id: int, plan_json: str):
        self._execute(
            """UPDATE architecture_plans
               SET plan_json = ?, version = version + 1, updated_at = ?
               WHERE id = ? AND project_id = ? AND service_name = ?""",
            (plan_json, _now(), plan_id, self._project_id, self._service_name),
        )
        self._commit()

    # ── Architecture Namespaces ───────────────────────────────────────────────────

    def save_namespace(self, plan_id: int, namespace_path: str, purpose: str) -> int:
        sql = self._db._upsert_sql(
            "architecture_namespaces",
            ["project_id", "service_name", "plan_id", "namespace_path", "purpose", "created_at"],
            ["plan_id", "namespace_path"],
            ["purpose"],
        )
        cur = self._execute(
            sql, (self._project_id, self._service_name, plan_id, namespace_path, purpose, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_namespaces(self, plan_id: int):
        cur = self._execute(
            """SELECT * FROM architecture_namespaces
               WHERE plan_id = ? AND project_id = ? AND service_name = ?
               ORDER BY namespace_path""",
            (plan_id, self._project_id, self._service_name),
        )
        return cur.fetchall()

    # ── Architecture Domain Models ────────────────────────────────────────────────

    def save_domain_model(
        self,
        plan_id: int,
        class_name: str,
        filepath: str,
        definition_json: str,
        namespace_id: Optional[int] = None,
    ) -> int:
        now = _now()
        cur = self._execute(
            """INSERT INTO architecture_domain_models
               (project_id, service_name, plan_id, namespace_id, class_name,
                filepath, definition_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._project_id, self._service_name, plan_id, namespace_id,
             class_name, filepath, definition_json, now, now),
        )
        self._commit()
        return cur.lastrowid

    def get_domain_models(self, plan_id: int):
        cur = self._execute(
            """SELECT * FROM architecture_domain_models
               WHERE plan_id = ? AND project_id = ? AND service_name = ?
               ORDER BY filepath""",
            (plan_id, self._project_id, self._service_name),
        )
        return cur.fetchall()

    def mark_domain_model_generated(self, model_id: int):
        self._execute(
            """UPDATE architecture_domain_models
               SET is_generated = 1, updated_at = ?
               WHERE id = ? AND project_id = ? AND service_name = ?""",
            (_now(), model_id, self._project_id, self._service_name),
        )
        self._commit()

    # ── Architecture Modules ──────────────────────────────────────────────────────

    def save_architecture_module(
        self,
        plan_id: int,
        filepath: str,
        definition_json: str,
        class_name: Optional[str] = None,
        namespace_id: Optional[int] = None,
    ) -> int:
        now = _now()
        sql = self._db._upsert_sql(
            "architecture_modules",
            ["project_id", "service_name", "plan_id", "namespace_id", "filepath",
             "class_name", "definition_json", "created_at", "updated_at"],
            ["plan_id", "filepath", "class_name"],
            ["definition_json", "updated_at"],
        )
        cur = self._execute(
            sql,
            (self._project_id, self._service_name, plan_id, namespace_id,
             filepath, class_name, definition_json, now, now),
        )
        self._commit()
        return cur.lastrowid

    def get_architecture_modules(self, plan_id: int):
        cur = self._execute(
            """SELECT * FROM architecture_modules
               WHERE plan_id = ? AND project_id = ? AND service_name = ?
               ORDER BY filepath""",
            (plan_id, self._project_id, self._service_name),
        )
        return cur.fetchall()

    # ── Architecture Dependencies ─────────────────────────────────────────────────

    def save_dependency(
        self,
        plan_id: int,
        source_filepath: str,
        target_filepath: str,
        import_symbols: Optional[str] = None,
    ) -> int:
        sql = self._db._upsert_sql(
            "architecture_dependencies",
            ["project_id", "service_name", "plan_id", "source_filepath",
             "target_filepath", "import_symbols", "created_at"],
            ["plan_id", "source_filepath", "target_filepath"],
            ["import_symbols"],
        )
        cur = self._execute(
            sql,
            (self._project_id, self._service_name, plan_id,
             source_filepath, target_filepath, import_symbols, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_dependencies(self, plan_id: int):
        cur = self._execute(
            """SELECT * FROM architecture_dependencies
               WHERE plan_id = ? AND project_id = ? AND service_name = ?
               ORDER BY source_filepath""",
            (plan_id, self._project_id, self._service_name),
        )
        return cur.fetchall()

    def get_dependencies_for_module(self, plan_id: int, filepath: str):
        cur = self._execute(
            """SELECT * FROM architecture_dependencies
               WHERE plan_id = ? AND source_filepath = ?
                 AND project_id = ? AND service_name = ?""",
            (plan_id, filepath, self._project_id, self._service_name),
        )
        return cur.fetchall()

    # ── Test Results ──────────────────────────────────────────────────────────────

    def save_test_result(
        self,
        test_filepath: str,
        passed: int,
        failed: int,
        total: int,
        issue_id: Optional[int] = None,
    ) -> int:
        cur = self._execute(
            """INSERT INTO test_results
               (project_id, service_name, issue_id, test_filepath, passed, failed, total, run_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._project_id, self._service_name, issue_id, test_filepath,
             passed, failed, total, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_latest_test_results(self, issue_id: Optional[int] = None):
        if issue_id is not None:
            cur = self._execute(
                """SELECT * FROM test_results
                   WHERE issue_id = ? AND project_id = ? AND service_name = ?
                   ORDER BY run_at DESC""",
                (issue_id, self._project_id, self._service_name),
            )
        else:
            cur = self._execute(
                """SELECT tr.* FROM test_results tr
                   INNER JOIN (
                       SELECT test_filepath, MAX(run_at) as max_run
                       FROM test_results
                       WHERE project_id = ? AND service_name = ?
                       GROUP BY test_filepath
                   ) latest ON tr.test_filepath = latest.test_filepath
                              AND tr.run_at = latest.max_run
                   WHERE tr.project_id = ? AND tr.service_name = ?
                   ORDER BY tr.test_filepath""",
                (self._project_id, self._service_name,
                 self._project_id, self._service_name),
            )
        return cur.fetchall()

    def get_passing_test_files(self) -> List[str]:
        cur = self._execute(
            """SELECT tr.test_filepath FROM test_results tr
               INNER JOIN (
                   SELECT test_filepath, MAX(run_at) as max_run
                   FROM test_results
                   WHERE project_id = ? AND service_name = ?
                   GROUP BY test_filepath
               ) latest ON tr.test_filepath = latest.test_filepath
                          AND tr.run_at = latest.max_run
               WHERE tr.failed = 0
                 AND tr.project_id = ? AND tr.service_name = ?""",
            (self._project_id, self._service_name,
             self._project_id, self._service_name),
        )
        return [row["test_filepath"] for row in cur.fetchall()]

    # ── Environment Packages ──────────────────────────────────────────────────────

    def save_package(self, package: str, version: Optional[str] = None) -> int:
        sql = self._db._upsert_sql(
            "environment_packages",
            ["project_id", "service_name", "package", "version", "installed_at"],
            ["project_id", "service_name", "package"],
            ["version", "installed_at"],
        )
        cur = self._execute(
            sql, (self._project_id, self._service_name, package, version, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_packages(self):
        cur = self._execute(
            "SELECT * FROM environment_packages WHERE project_id = ? AND service_name = ? ORDER BY package",
            (self._project_id, self._service_name),
        )
        return cur.fetchall()

    def remove_package(self, package: str):
        self._execute(
            "DELETE FROM environment_packages WHERE package = ? AND project_id = ? AND service_name = ?",
            (package, self._project_id, self._service_name),
        )
        self._commit()

    # ── Environment Config ────────────────────────────────────────────────────────

    def get_config(self, key: str) -> Optional[str]:
        cur = self._execute(
            "SELECT value FROM environment_config WHERE config_key = ? AND project_id = ? AND service_name = ?",
            (key, self._project_id, self._service_name),
        )
        row = cur.fetchone()
        return row["value"] if row else None

    def set_config(self, key: str, value: str):
        sql = self._db._upsert_sql(
            "environment_config",
            ["project_id", "service_name", "config_key", "value", "created_at"],
            ["project_id", "service_name", "config_key"],
            ["value", "created_at"],
        )
        self._execute(
            sql, (self._project_id, self._service_name, key, value, _now()),
        )
        self._commit()

    # ── Lifecycle ─────────────────────────────────────────────────────────────────

    def close(self):
        pass  # Shared connection; closed by BiznizDB

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
