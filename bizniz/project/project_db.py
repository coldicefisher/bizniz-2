"""
ProjectDB

A thin SQLite wrapper that lives inside a project at
    {project.root}/.bizniz/project.db

Tables
------
services                — registered services in the project
architecture_snapshots  — architecture snapshot history
issue_log               — issues across all services
build_log               — build events (image builds, package installs, rebuilds)
drift_events            — file-drift detection events
"""

from __future__ import annotations

import json
import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from bizniz.project.project import Project


class ProjectDB:

    def __init__(self, project: "Project"):
        db_dir = project.root / ".bizniz"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "project.db"
        self._db_dir = db_dir
        self._conn = sqlite3.connect(str(self._db_path), timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        self._ensure_writable()

    def _ensure_writable(self):
        """Ensure DB file and directory are writable (Docker may change permissions)."""
        try:
            import os
            os.chmod(str(self._db_dir), 0o777)
            os.chmod(str(self._db_path), 0o666)
            for suffix in ["-journal", "-wal", "-shm"]:
                journal = self._db_path.with_suffix(self._db_path.suffix + suffix)
                if journal.exists():
                    os.chmod(str(journal), 0o666)
        except OSError:
            pass

    # ── Schema ──────────────────────────────────────────────────────────────────

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS services (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL UNIQUE,
                service_type    TEXT    NOT NULL,
                framework       TEXT    NOT NULL,
                language        TEXT    NOT NULL,
                workspace_path  TEXT    NOT NULL,
                image_name      TEXT,
                status          TEXT    NOT NULL DEFAULT 'open'
                                CHECK(status IN ('open','building','ready','failed')),
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS architecture_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_json   TEXT    NOT NULL,
                version         INTEGER NOT NULL,
                description     TEXT    NOT NULL DEFAULT '',
                created_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issue_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name      TEXT    NOT NULL,
                issue_title       TEXT    NOT NULL,
                issue_description TEXT    NOT NULL,
                status            TEXT    NOT NULL DEFAULT 'open'
                                  CHECK(status IN ('open','in_progress','closed','failed')),
                strategy_used     TEXT,
                iterations        INTEGER,
                created_at        TEXT    NOT NULL,
                closed_at         TEXT
            );

            CREATE TABLE IF NOT EXISTS build_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name    TEXT    NOT NULL,
                event_type      TEXT    NOT NULL
                                CHECK(event_type IN ('image_build','package_install','rebuild')),
                success         INTEGER NOT NULL,
                detail          TEXT    NOT NULL DEFAULT '',
                created_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS drift_events (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name      TEXT    NOT NULL,
                drift_files_json  TEXT    NOT NULL,
                resolution        TEXT    NOT NULL DEFAULT '',
                created_at        TEXT    NOT NULL
            );

            -- One row per architect.build() invocation (or any other
            -- top-level unit of work). Cost rollups aggregate api_calls
            -- by job_id; this table is just the lightweight index.
            CREATE TABLE IF NOT EXISTS jobs (
                id                  TEXT    PRIMARY KEY,
                project_slug        TEXT    NOT NULL,
                problem_statement   TEXT    NOT NULL DEFAULT '',
                status              TEXT    NOT NULL DEFAULT 'running'
                                    CHECK(status IN ('running','succeeded','failed','cancelled')),
                started_at          TEXT    NOT NULL,
                finished_at         TEXT,
                total_calls         INTEGER NOT NULL DEFAULT 0,
                total_input_tokens  INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost          REAL    NOT NULL DEFAULT 0.0,
                metadata_json       TEXT
            );

            -- One row per AI call. Tagged with job/service/issue/phase so
            -- any rollup is just a GROUP BY away.
            CREATE TABLE IF NOT EXISTS api_calls (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          TEXT,
                timestamp       TEXT    NOT NULL,
                agent           TEXT,
                model           TEXT    NOT NULL,
                service_name    TEXT,
                issue_id        INTEGER,
                phase           TEXT,
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                input_cost      REAL    NOT NULL DEFAULT 0.0,
                output_cost     REAL    NOT NULL DEFAULT 0.0,
                total_cost      REAL    NOT NULL DEFAULT 0.0,
                priced          INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_api_calls_job ON api_calls(job_id);
            CREATE INDEX IF NOT EXISTS idx_api_calls_issue ON api_calls(issue_id);
            CREATE INDEX IF NOT EXISTS idx_api_calls_service ON api_calls(service_name);
            CREATE INDEX IF NOT EXISTS idx_api_calls_model ON api_calls(model);
        """)
        self._conn.commit()

    # ── Services ────────────────────────────────────────────────────────────────

    def save_service(
        self,
        name: str,
        service_type: str,
        framework: str,
        language: str,
        workspace_path: str,
        image_name: Optional[str] = None,
    ) -> int:
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO services
               (name, service_type, framework, language, workspace_path, image_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   service_type = excluded.service_type,
                   framework = excluded.framework,
                   language = excluded.language,
                   workspace_path = excluded.workspace_path,
                   image_name = excluded.image_name,
                   status = 'open',
                   updated_at = excluded.updated_at""",
            (name, service_type, framework, language, workspace_path, image_name, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_service_status(self, name: str, status: str):
        self._conn.execute(
            "UPDATE services SET status = ?, updated_at = ? WHERE name = ?",
            (status, _now(), name),
        )
        self._conn.commit()

    def update_service_image(self, name: str, image_name: str):
        self._conn.execute(
            "UPDATE services SET image_name = ?, updated_at = ? WHERE name = ?",
            (image_name, _now(), name),
        )
        self._conn.commit()

    def get_services(self) -> List[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM services ORDER BY name")
        return cur.fetchall()

    def get_service(self, name: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM services WHERE name = ?", (name,)
        )
        return cur.fetchone()

    # ── Architecture Snapshots ──────────────────────────────────────────────────

    def save_architecture_snapshot(
        self, snapshot_json: str, description: str = ""
    ) -> int:
        # Auto-increment version based on current max
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS max_ver FROM architecture_snapshots"
        )
        next_version = cur.fetchone()["max_ver"] + 1
        cur = self._conn.execute(
            """INSERT INTO architecture_snapshots
               (snapshot_json, version, description, created_at)
               VALUES (?, ?, ?, ?)""",
            (snapshot_json, next_version, description, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_architecture_changes(self) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_snapshots ORDER BY version"
        )
        return cur.fetchall()

    def get_latest_architecture(self) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM architecture_snapshots ORDER BY version DESC LIMIT 1"
        )
        return cur.fetchone()

    # ── Issue Log ───────────────────────────────────────────────────────────────

    def log_issue(
        self,
        service_name: str,
        title: str,
        description: str,
        status: str = "open",
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO issue_log
               (service_name, issue_title, issue_description, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (service_name, title, description, status, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_issue(
        self,
        issue_id: int,
        status: str,
        strategy_used: Optional[str] = None,
        iterations: Optional[int] = None,
    ):
        self._conn.execute(
            """UPDATE issue_log
               SET status = ?, strategy_used = COALESCE(?, strategy_used),
                   iterations = COALESCE(?, iterations)
               WHERE id = ?""",
            (status, strategy_used, iterations, issue_id),
        )
        self._conn.commit()

    def close_issue(
        self,
        issue_id: int,
        strategy_used: Optional[str] = None,
        iterations: Optional[int] = None,
    ):
        self._conn.execute(
            """UPDATE issue_log
               SET status = 'closed', closed_at = ?,
                   strategy_used = COALESCE(?, strategy_used),
                   iterations = COALESCE(?, iterations)
               WHERE id = ?""",
            (_now(), strategy_used, iterations, issue_id),
        )
        self._conn.commit()

    def get_all_issues(self, service_name: Optional[str] = None) -> List[sqlite3.Row]:
        if service_name is not None:
            cur = self._conn.execute(
                "SELECT * FROM issue_log WHERE service_name = ? ORDER BY created_at",
                (service_name,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM issue_log ORDER BY created_at"
            )
        return cur.fetchall()

    def get_open_issues(self, service_name: Optional[str] = None) -> List[sqlite3.Row]:
        if service_name is not None:
            cur = self._conn.execute(
                "SELECT * FROM issue_log WHERE service_name = ? AND status = 'open' ORDER BY created_at",
                (service_name,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM issue_log WHERE status = 'open' ORDER BY created_at"
            )
        return cur.fetchall()

    # ── Build Log ───────────────────────────────────────────────────────────────

    def log_build_event(
        self,
        service_name: str,
        event_type: str,
        success: bool,
        detail: str = "",
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO build_log
               (service_name, event_type, success, detail, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (service_name, event_type, int(success), detail, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_build_log(self, service_name: Optional[str] = None) -> List[sqlite3.Row]:
        if service_name is not None:
            cur = self._conn.execute(
                "SELECT * FROM build_log WHERE service_name = ? ORDER BY created_at",
                (service_name,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM build_log ORDER BY created_at"
            )
        return cur.fetchall()

    # ── Drift Events ────────────────────────────────────────────────────────────

    def log_drift(
        self,
        service_name: str,
        drift_files: list,
        resolution: str = "",
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO drift_events
               (service_name, drift_files_json, resolution, created_at)
               VALUES (?, ?, ?, ?)""",
            (service_name, json.dumps(drift_files), resolution, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_drift_events(self, service_name: Optional[str] = None) -> List[sqlite3.Row]:
        if service_name is not None:
            cur = self._conn.execute(
                "SELECT * FROM drift_events WHERE service_name = ? ORDER BY created_at",
                (service_name,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM drift_events ORDER BY created_at"
            )
        return cur.fetchall()

    # ── Jobs + AI cost ──────────────────────────────────────────────────────────

    def start_job(
        self,
        job_id: str,
        project_slug: str,
        problem_statement: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        """Open a new job row. Idempotent — re-calling with the same id is a no-op."""
        existing = self._conn.execute(
            "SELECT id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if existing:
            return job_id
        self._conn.execute(
            """INSERT INTO jobs
               (id, project_slug, problem_statement, status, started_at, metadata_json)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (
                job_id, project_slug,
                (problem_statement or "")[:1000],
                _now(),
                json.dumps(metadata) if metadata else None,
            ),
        )
        self._conn.commit()
        return job_id

    def finish_job(
        self,
        job_id: str,
        status: str = "succeeded",
    ) -> None:
        """Mark a job done and refresh its rollup totals from api_calls."""
        if status not in ("succeeded", "failed", "cancelled", "running"):
            status = "succeeded"
        totals = self._conn.execute(
            """SELECT
                 COUNT(*)               AS calls,
                 COALESCE(SUM(input_tokens), 0)  AS in_tok,
                 COALESCE(SUM(output_tokens), 0) AS out_tok,
                 COALESCE(SUM(total_cost), 0.0)  AS cost
               FROM api_calls WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        self._conn.execute(
            """UPDATE jobs SET
                 status              = ?,
                 finished_at         = ?,
                 total_calls         = ?,
                 total_input_tokens  = ?,
                 total_output_tokens = ?,
                 total_cost          = ?
               WHERE id = ?""",
            (status, _now(),
             int(totals["calls"]), int(totals["in_tok"]),
             int(totals["out_tok"]), float(totals["cost"]),
             job_id),
        )
        self._conn.commit()

    def save_api_call(self, record) -> int:
        """Persist one CallRecord. Accepts either a CallRecord dataclass
        from bizniz.cost.tracker or any object with the same attribute
        surface (timestamp, agent, model, input_tokens, output_tokens,
        duration_ms, problem_id, issue_id, cost, plus optional job_id /
        service_name / phase tags).
        """
        cost = getattr(record, "cost", None)
        cur = self._conn.execute(
            """INSERT INTO api_calls
               (job_id, timestamp, agent, model, service_name, issue_id, phase,
                input_tokens, output_tokens, duration_ms,
                input_cost, output_cost, total_cost, priced)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                getattr(record, "job_id", None),
                getattr(record, "timestamp", _now()),
                getattr(record, "agent", None),
                getattr(record, "model", "unknown"),
                getattr(record, "service_name", None),
                getattr(record, "issue_id", None),
                getattr(record, "phase", None),
                int(getattr(record, "input_tokens", 0) or 0),
                int(getattr(record, "output_tokens", 0) or 0),
                int(getattr(record, "duration_ms", 0) or 0),
                float(getattr(cost, "input_cost", 0.0) or 0.0),
                float(getattr(cost, "output_cost", 0.0) or 0.0),
                float(getattr(cost, "total_cost", 0.0) or 0.0),
                1 if (cost is None or getattr(cost, "priced", True)) else 0,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_jobs(self, limit: int = 50) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,),
        )
        return cur.fetchall()

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return cur.fetchone()

    def cost_by_issue(self, job_id: Optional[str] = None) -> List[sqlite3.Row]:
        """Rollup: per-issue cost + tokens (optionally scoped to a job)."""
        if job_id is None:
            cur = self._conn.execute(
                """SELECT issue_id,
                          COUNT(*)                AS calls,
                          SUM(input_tokens)       AS input_tokens,
                          SUM(output_tokens)      AS output_tokens,
                          SUM(total_cost)         AS total_cost
                   FROM api_calls
                   WHERE issue_id IS NOT NULL
                   GROUP BY issue_id
                   ORDER BY total_cost DESC"""
            )
        else:
            cur = self._conn.execute(
                """SELECT issue_id,
                          COUNT(*)                AS calls,
                          SUM(input_tokens)       AS input_tokens,
                          SUM(output_tokens)      AS output_tokens,
                          SUM(total_cost)         AS total_cost
                   FROM api_calls
                   WHERE issue_id IS NOT NULL AND job_id = ?
                   GROUP BY issue_id
                   ORDER BY total_cost DESC""",
                (job_id,),
            )
        return cur.fetchall()

    def cost_by_service(self, job_id: Optional[str] = None) -> List[sqlite3.Row]:
        if job_id is None:
            cur = self._conn.execute(
                """SELECT service_name,
                          COUNT(*)             AS calls,
                          SUM(total_cost)      AS total_cost
                   FROM api_calls
                   WHERE service_name IS NOT NULL
                   GROUP BY service_name
                   ORDER BY total_cost DESC"""
            )
        else:
            cur = self._conn.execute(
                """SELECT service_name,
                          COUNT(*)             AS calls,
                          SUM(total_cost)      AS total_cost
                   FROM api_calls
                   WHERE service_name IS NOT NULL AND job_id = ?
                   GROUP BY service_name
                   ORDER BY total_cost DESC""",
                (job_id,),
            )
        return cur.fetchall()

    def cost_by_model(self, job_id: Optional[str] = None) -> List[sqlite3.Row]:
        if job_id is None:
            cur = self._conn.execute(
                """SELECT model,
                          COUNT(*)             AS calls,
                          SUM(input_tokens)    AS input_tokens,
                          SUM(output_tokens)   AS output_tokens,
                          SUM(total_cost)      AS total_cost
                   FROM api_calls
                   GROUP BY model
                   ORDER BY total_cost DESC"""
            )
        else:
            cur = self._conn.execute(
                """SELECT model,
                          COUNT(*)             AS calls,
                          SUM(input_tokens)    AS input_tokens,
                          SUM(output_tokens)   AS output_tokens,
                          SUM(total_cost)      AS total_cost
                   FROM api_calls
                   WHERE job_id = ?
                   GROUP BY model
                   ORDER BY total_cost DESC""",
                (job_id,),
            )
        return cur.fetchall()

    # ── Lifecycle ───────────────────────────────────────────────────────────────

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


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
