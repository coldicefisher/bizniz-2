"""
BiznizDB — Unified database for all bizniz data.

Supports MySQL (production) and SQLite (testing/development).
All tables live in a single database, scoped by project_id.

Usage::

    db = BiznizDB("mysql://user:pass@localhost/bizniz")
    project_scope = db.for_project("my-project")
    workspace_scope = db.for_workspace("my-project", "backend")
"""

import datetime
import sqlite3
from urllib.parse import urlparse


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class BiznizDB:
    """
    Central database connection manager.

    Connects to MySQL or SQLite depending on the database_url scheme.
    Provides scoped accessors for project-level and workspace-level data.
    """

    def __init__(self, database_url: str = "sqlite:///:memory:"):
        self._database_url = database_url

        if database_url.startswith("mysql"):
            self._backend = "mysql"
            self._connect_mysql(database_url)
        else:
            self._backend = "sqlite"
            self._connect_sqlite(database_url)

        self._create_tables()

    @property
    def backend(self) -> str:
        return self._backend

    # ── Connection ────────────────────────────────────────────────────────────────

    def _connect_mysql(self, url: str):
        import pymysql
        import pymysql.cursors

        parsed = urlparse(url)
        self._conn = pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username or "root",
            password=parsed.password or "",
            database=parsed.path.lstrip("/"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def _connect_sqlite(self, url: str):
        if url.startswith("sqlite:///"):
            path = url[len("sqlite:///"):]
        elif url in ("sqlite:///:memory:", ":memory:"):
            path = ":memory:"
        else:
            path = url
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    # ── SQL helpers ───────────────────────────────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()):
        """Execute SQL with automatic placeholder conversion (? → %s for MySQL)."""
        if self._backend == "mysql":
            sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def _commit(self):
        self._conn.commit()

    def _upsert_sql(self, table, columns, conflict_cols, update_cols):
        """Generate backend-specific upsert SQL.

        Returns SQL with ? placeholders (converted by _execute for MySQL).
        """
        placeholders = ", ".join(["?"] * len(columns))
        col_str = ", ".join(columns)

        if self._backend == "mysql":
            updates = ", ".join(f"{c} = VALUES({c})" for c in update_cols)
            updates += ", id = LAST_INSERT_ID(id)"
            sql = (
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {updates}"
            )
        else:
            conflict_str = ", ".join(conflict_cols)
            updates = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict_str}) DO UPDATE SET {updates}"
            )

        return sql

    # ── Schema ────────────────────────────────────────────────────────────────────

    def _create_tables(self):
        if self._backend == "mysql":
            auto_pk = "INT PRIMARY KEY AUTO_INCREMENT"
        else:
            auto_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

        tables = [
            # ── Workspace-scoped tables ──────────────────────────────────────

            f"""CREATE TABLE IF NOT EXISTS problems (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                statement       TEXT NOT NULL,
                created_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS requirements (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                problem_id      INT NOT NULL,
                type            VARCHAR(32) NOT NULL
                                CHECK(type IN ('business','functional','nonfunctional')),
                text            TEXT NOT NULL,
                created_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS use_cases (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                problem_id      INT NOT NULL,
                title           VARCHAR(255) NOT NULL,
                description     TEXT NOT NULL,
                created_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS issues (
                id                  {auto_pk},
                project_id          VARCHAR(255) NOT NULL,
                service_name        VARCHAR(255) NOT NULL,
                problem_id          INT NOT NULL,
                title               VARCHAR(255) NOT NULL,
                description         TEXT NOT NULL,
                status              VARCHAR(32) NOT NULL DEFAULT 'open'
                                    CHECK(status IN ('open','in_progress','closed')),
                target_files_json   TEXT NOT NULL DEFAULT '[]',
                test_files_json     TEXT NOT NULL DEFAULT '[]',
                depends_on_json     TEXT NOT NULL DEFAULT '[]',
                suggested_model     VARCHAR(64),
                test_setup_hint     TEXT NOT NULL DEFAULT '',
                created_at          VARCHAR(64) NOT NULL,
                closed_at           VARCHAR(64)
            )""",

            f"""CREATE TABLE IF NOT EXISTS architecture_plans (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                problem_id      INT NOT NULL,
                package_name    VARCHAR(255) NOT NULL,
                root_namespace  VARCHAR(255) NOT NULL,
                plan_json       TEXT NOT NULL,
                version         INT NOT NULL DEFAULT 1,
                created_at      VARCHAR(64) NOT NULL,
                updated_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS architecture_namespaces (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                plan_id         INT NOT NULL,
                namespace_path  VARCHAR(255) NOT NULL,
                purpose         TEXT NOT NULL,
                created_at      VARCHAR(64) NOT NULL,
                UNIQUE(plan_id, namespace_path)
            )""",

            f"""CREATE TABLE IF NOT EXISTS architecture_domain_models (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                plan_id         INT NOT NULL,
                namespace_id    INT,
                class_name      VARCHAR(255) NOT NULL,
                filepath        VARCHAR(512) NOT NULL,
                definition_json TEXT NOT NULL,
                is_generated    INT NOT NULL DEFAULT 0,
                created_at      VARCHAR(64) NOT NULL,
                updated_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS architecture_modules (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                plan_id         INT NOT NULL,
                namespace_id    INT,
                filepath        VARCHAR(512) NOT NULL,
                class_name      VARCHAR(255),
                definition_json TEXT NOT NULL,
                created_at      VARCHAR(64) NOT NULL,
                updated_at      VARCHAR(64) NOT NULL,
                UNIQUE(plan_id, filepath, class_name)
            )""",

            f"""CREATE TABLE IF NOT EXISTS architecture_dependencies (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                plan_id         INT NOT NULL,
                source_filepath VARCHAR(512) NOT NULL,
                target_filepath VARCHAR(512) NOT NULL,
                import_symbols  TEXT,
                created_at      VARCHAR(64) NOT NULL,
                UNIQUE(plan_id, source_filepath, target_filepath)
            )""",

            f"""CREATE TABLE IF NOT EXISTS test_results (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                issue_id        INT,
                test_filepath   VARCHAR(512) NOT NULL,
                passed          INT NOT NULL,
                failed          INT NOT NULL,
                total           INT NOT NULL,
                run_at          VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS environment_packages (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                package         VARCHAR(255) NOT NULL,
                version         VARCHAR(64),
                installed_at    VARCHAR(64) NOT NULL,
                UNIQUE(project_id, service_name, package)
            )""",

            f"""CREATE TABLE IF NOT EXISTS environment_config (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                config_key      VARCHAR(255) NOT NULL,
                value           TEXT NOT NULL,
                created_at      VARCHAR(64) NOT NULL,
                UNIQUE(project_id, service_name, config_key)
            )""",

            # ── Project-scoped tables ────────────────────────────────────────

            f"""CREATE TABLE IF NOT EXISTS services (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                name            VARCHAR(255) NOT NULL,
                service_type    VARCHAR(64) NOT NULL,
                framework       VARCHAR(64) NOT NULL,
                language        VARCHAR(64) NOT NULL,
                workspace_path  VARCHAR(512) NOT NULL,
                image_name      VARCHAR(255),
                status          VARCHAR(32) NOT NULL DEFAULT 'open'
                                CHECK(status IN ('open','building','ready','failed')),
                created_at      VARCHAR(64) NOT NULL,
                updated_at      VARCHAR(64) NOT NULL,
                UNIQUE(project_id, name)
            )""",

            f"""CREATE TABLE IF NOT EXISTS architecture_snapshots (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                snapshot_json   TEXT NOT NULL,
                version         INT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                created_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS issue_log (
                id                {auto_pk},
                project_id        VARCHAR(255) NOT NULL,
                service_name      VARCHAR(255) NOT NULL,
                issue_title       VARCHAR(255) NOT NULL,
                issue_description TEXT NOT NULL,
                status            VARCHAR(32) NOT NULL DEFAULT 'open'
                                  CHECK(status IN ('open','in_progress','closed','failed')),
                strategy_used     VARCHAR(64),
                iterations        INT,
                created_at        VARCHAR(64) NOT NULL,
                closed_at         VARCHAR(64)
            )""",

            f"""CREATE TABLE IF NOT EXISTS build_log (
                id              {auto_pk},
                project_id      VARCHAR(255) NOT NULL,
                service_name    VARCHAR(255) NOT NULL,
                event_type      VARCHAR(32) NOT NULL
                                CHECK(event_type IN ('image_build','package_install','rebuild')),
                success         INT NOT NULL,
                detail          TEXT NOT NULL DEFAULT '',
                created_at      VARCHAR(64) NOT NULL
            )""",

            f"""CREATE TABLE IF NOT EXISTS drift_events (
                id                {auto_pk},
                project_id        VARCHAR(255) NOT NULL,
                service_name      VARCHAR(255) NOT NULL,
                drift_files_json  TEXT NOT NULL,
                resolution        TEXT NOT NULL DEFAULT '',
                created_at        VARCHAR(64) NOT NULL
            )""",
        ]

        cur = self._conn.cursor()
        for ddl in tables:
            cur.execute(ddl)
        self._conn.commit()

    # ── Scope factories ───────────────────────────────────────────────────────────

    def for_project(self, project_id: str) -> "ProjectScope":
        from bizniz.db.project_scope import ProjectScope
        return ProjectScope(self, project_id)

    def for_workspace(self, project_id: str, service_name: str) -> "WorkspaceScope":
        from bizniz.db.workspace_scope import WorkspaceScope
        return WorkspaceScope(self, project_id, service_name)

    # ── Lifecycle ─────────────────────────────────────────────────────────────────

    def close(self):
        try:
            self._conn.commit()
        except Exception:
            pass
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
