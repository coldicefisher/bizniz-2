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

            -- One row per AI call. Tagged with job/service/issue/phase/
            -- milestone so any rollup is just a GROUP BY away.
            CREATE TABLE IF NOT EXISTS api_calls (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          TEXT,
                timestamp       TEXT    NOT NULL,
                agent           TEXT,
                model           TEXT    NOT NULL,
                service_name    TEXT,
                issue_id        INTEGER,
                milestone_id    INTEGER,
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
            CREATE INDEX IF NOT EXISTS idx_api_calls_milestone ON api_calls(milestone_id);

            -- The Planner's output: a sequence of milestones that the
            -- Architect later evolves the project through. One active plan
            -- per project at a time; old plans are archived (archived_at
            -- set) when a re-plan supersedes them.
            CREATE TABLE IF NOT EXISTS project_plans (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug        TEXT    NOT NULL,
                problem_statement   TEXT    NOT NULL DEFAULT '',
                description         TEXT    NOT NULL DEFAULT '',
                created_at          TEXT    NOT NULL,
                archived_at         TEXT
            );

            -- Each milestone is a self-contained problem-slice the
            -- Architect can decompose later. Use cases describe user value;
            -- success criteria are testable outcomes; depends_on_json holds
            -- milestone NAMES (resolved to ids on read).
            CREATE TABLE IF NOT EXISTS milestones (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id               INTEGER NOT NULL,
                sequence_index        INTEGER NOT NULL,
                name                  TEXT    NOT NULL,
                problem_slice         TEXT    NOT NULL,
                use_cases_json        TEXT    NOT NULL DEFAULT '[]',
                success_criteria_json TEXT    NOT NULL DEFAULT '[]',
                depends_on_json       TEXT    NOT NULL DEFAULT '[]',
                estimated_effort      TEXT,
                status                TEXT    NOT NULL DEFAULT 'planned'
                                      CHECK(status IN ('planned','in_progress','completed','skipped')),
                started_at            TEXT,
                completed_at          TEXT,
                created_at            TEXT    NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES project_plans(id)
            );

            CREATE INDEX IF NOT EXISTS idx_milestones_plan ON milestones(plan_id);
            CREATE INDEX IF NOT EXISTS idx_milestones_status ON milestones(status);

            -- v2.5 single-source-of-truth for issue-level state. Each row
            -- is one Coder issue inside one milestone of one job. Status
            -- transitions: pending → running → (passed|partial|failed|
            -- stalled|deferred|errored|skipped). Rows persist across
            -- runs so you can query stall history per issue across all
            -- M1 retries. Filter by job_id for per-run views.
            CREATE TABLE IF NOT EXISTS coder_issues (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id               TEXT    NOT NULL,
                milestone_index      INTEGER NOT NULL,
                service              TEXT    NOT NULL,
                issue_id             TEXT    NOT NULL,
                issue_index          INTEGER NOT NULL DEFAULT 0,
                title                TEXT    NOT NULL,
                description          TEXT    NOT NULL DEFAULT '',
                language             TEXT    NOT NULL DEFAULT 'python',
                target_files         TEXT    NOT NULL DEFAULT '[]',
                test_files           TEXT    NOT NULL DEFAULT '[]',
                spec_refs            TEXT    NOT NULL DEFAULT '[]',
                depends_on           TEXT    NOT NULL DEFAULT '[]',
                success_criteria     TEXT    NOT NULL DEFAULT '[]',
                status               TEXT    NOT NULL DEFAULT 'pending'
                                     CHECK(status IN ('pending','running','passed',
                                                      'partial','failed','stalled',
                                                      'deferred','errored','skipped','escalated')),
                tiers_used           TEXT    NOT NULL DEFAULT '[]',
                current_tier         TEXT,
                iterations_used      INTEGER NOT NULL DEFAULT 0,
                target_files_written TEXT    NOT NULL DEFAULT '[]',
                test_files_written   TEXT    NOT NULL DEFAULT '[]',
                last_test_output     TEXT    NOT NULL DEFAULT '',
                summary              TEXT    NOT NULL DEFAULT '',
                error                TEXT    NOT NULL DEFAULT '',
                notes                TEXT    NOT NULL DEFAULT '[]',
                planned_at           TEXT    NOT NULL,
                started_at           TEXT,
                finished_at          TEXT,
                UNIQUE(job_id, milestone_index, service, issue_id)
            );

            CREATE INDEX IF NOT EXISTS idx_coder_issues_job ON coder_issues(job_id);
            CREATE INDEX IF NOT EXISTS idx_coder_issues_milestone
                ON coder_issues(job_id, milestone_index);
            CREATE INDEX IF NOT EXISTS idx_coder_issues_service
                ON coder_issues(service);
            CREATE INDEX IF NOT EXISTS idx_coder_issues_status
                ON coder_issues(status);
        """)
        self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self):
        """Forward-only schema fixes for project DBs created by older
        versions of this code. Idempotent — each ALTER is wrapped to
        swallow the 'duplicate column' error."""
        for ddl in (
            "ALTER TABLE api_calls ADD COLUMN milestone_id INTEGER",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    continue
                raise
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
        service_name / phase / milestone_id tags).
        """
        cost = getattr(record, "cost", None)
        cur = self._conn.execute(
            """INSERT INTO api_calls
               (job_id, timestamp, agent, model, service_name, issue_id,
                milestone_id, phase,
                input_tokens, output_tokens, duration_ms,
                input_cost, output_cost, total_cost, priced)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                getattr(record, "job_id", None),
                getattr(record, "timestamp", _now()),
                getattr(record, "agent", None),
                getattr(record, "model", "unknown"),
                getattr(record, "service_name", None),
                getattr(record, "issue_id", None),
                getattr(record, "milestone_id", None),
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

    # ── Project plans + milestones ──────────────────────────────────────────────

    def save_project_plan(
        self,
        project_slug: str,
        problem_statement: str,
        description: str = "",
    ) -> int:
        """Insert a new project plan and return its id. Doesn't archive
        prior plans — call ``archive_plan`` first if a re-plan should
        supersede the active one."""
        cur = self._conn.execute(
            """INSERT INTO project_plans
               (project_slug, problem_statement, description, created_at)
               VALUES (?, ?, ?, ?)""",
            (project_slug, problem_statement or "", description or "", _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def archive_plan(self, plan_id: int) -> None:
        self._conn.execute(
            "UPDATE project_plans SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (_now(), plan_id),
        )
        self._conn.commit()

    def get_active_plan(self, project_slug: str) -> Optional[sqlite3.Row]:
        """Return the most recent non-archived plan for the project."""
        cur = self._conn.execute(
            """SELECT * FROM project_plans
               WHERE project_slug = ? AND archived_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (project_slug,),
        )
        return cur.fetchone()

    def save_milestone(
        self,
        plan_id: int,
        sequence_index: int,
        name: str,
        problem_slice: str,
        use_cases: Optional[List[str]] = None,
        success_criteria: Optional[List[str]] = None,
        depends_on_names: Optional[List[str]] = None,
        estimated_effort: Optional[str] = None,
        status: str = "planned",
    ) -> int:
        if status not in ("planned", "in_progress", "completed", "skipped"):
            status = "planned"
        cur = self._conn.execute(
            """INSERT INTO milestones
               (plan_id, sequence_index, name, problem_slice,
                use_cases_json, success_criteria_json, depends_on_json,
                estimated_effort, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_id, int(sequence_index), name, problem_slice,
                json.dumps(use_cases or []),
                json.dumps(success_criteria or []),
                json.dumps(depends_on_names or []),
                estimated_effort,
                status,
                _now(),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_milestones(
        self,
        plan_id: int,
        status: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        if status is None:
            cur = self._conn.execute(
                "SELECT * FROM milestones WHERE plan_id = ? ORDER BY sequence_index",
                (plan_id,),
            )
        else:
            cur = self._conn.execute(
                """SELECT * FROM milestones
                   WHERE plan_id = ? AND status = ?
                   ORDER BY sequence_index""",
                (plan_id, status),
            )
        return cur.fetchall()

    def get_milestone(self, milestone_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,),
        )
        return cur.fetchone()

    def update_milestone_status(self, milestone_id: int, status: str) -> None:
        if status not in ("planned", "in_progress", "completed", "skipped"):
            return
        if status == "in_progress":
            self._conn.execute(
                """UPDATE milestones
                   SET status = ?, started_at = COALESCE(started_at, ?)
                   WHERE id = ?""",
                (status, _now(), milestone_id),
            )
        elif status == "completed":
            self._conn.execute(
                """UPDATE milestones
                   SET status = ?, completed_at = ?
                   WHERE id = ?""",
                (status, _now(), milestone_id),
            )
        else:
            self._conn.execute(
                "UPDATE milestones SET status = ? WHERE id = ?",
                (status, milestone_id),
            )
        self._conn.commit()

    def cost_by_milestone(self, plan_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Aggregate api_calls by milestone_id. Pass ``plan_id`` to scope
        to one plan; otherwise rolls up across every milestone the
        project has ever recorded against."""
        if plan_id is None:
            cur = self._conn.execute(
                """SELECT m.id           AS milestone_id,
                          m.name         AS name,
                          m.sequence_index AS sequence_index,
                          COUNT(c.id)    AS calls,
                          COALESCE(SUM(c.input_tokens), 0)  AS input_tokens,
                          COALESCE(SUM(c.output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(c.total_cost), 0.0)  AS total_cost
                   FROM milestones m
                   LEFT JOIN api_calls c ON c.milestone_id = m.id
                   GROUP BY m.id
                   ORDER BY m.sequence_index"""
            )
        else:
            cur = self._conn.execute(
                """SELECT m.id           AS milestone_id,
                          m.name         AS name,
                          m.sequence_index AS sequence_index,
                          COUNT(c.id)    AS calls,
                          COALESCE(SUM(c.input_tokens), 0)  AS input_tokens,
                          COALESCE(SUM(c.output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(c.total_cost), 0.0)  AS total_cost
                   FROM milestones m
                   LEFT JOIN api_calls c ON c.milestone_id = m.id
                   WHERE m.plan_id = ?
                   GROUP BY m.id
                   ORDER BY m.sequence_index""",
                (plan_id,),
            )
        return cur.fetchall()

    # ── Coder issues (v2.5 single-source-of-truth for IMPLEMENT phase) ─────

    def upsert_planned_issue(
        self,
        *,
        job_id: str,
        milestone_index: int,
        service: str,
        issue_id: str,
        issue_index: int,
        title: str,
        description: str,
        language: str,
        target_files: List[str],
        test_files: List[str],
        spec_refs: List[str],
        depends_on: List[str],
        success_criteria: List[str],
    ) -> int:
        """Record an issue planned by ServicePlanner. Idempotent via the
        UNIQUE(job_id, milestone_index, service, issue_id) constraint:
        re-planning the same issue updates its definition without
        clobbering runtime state. Returns the row id.
        """
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO coder_issues
               (job_id, milestone_index, service, issue_id, issue_index,
                title, description, language, target_files, test_files,
                spec_refs, depends_on, success_criteria, planned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id, milestone_index, service, issue_id) DO UPDATE SET
                   issue_index = excluded.issue_index,
                   title = excluded.title,
                   description = excluded.description,
                   language = excluded.language,
                   target_files = excluded.target_files,
                   test_files = excluded.test_files,
                   spec_refs = excluded.spec_refs,
                   depends_on = excluded.depends_on,
                   success_criteria = excluded.success_criteria
               """,
            (
                job_id, milestone_index, service, issue_id, issue_index,
                title, description, language,
                json.dumps(target_files), json.dumps(test_files),
                json.dumps(spec_refs), json.dumps(depends_on),
                json.dumps(success_criteria), now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def mark_issue_started(
        self,
        *,
        job_id: str,
        milestone_index: int,
        service: str,
        issue_id: str,
        tier: str,
    ) -> None:
        """Mark issue as running, append tier to tiers_used."""
        row = self.get_coder_issue(job_id, milestone_index, service, issue_id)
        if row is None:
            return
        tiers = list(json.loads(row["tiers_used"] or "[]"))
        if not tiers or tiers[-1] != tier:
            tiers.append(tier)
        now = _now()
        self._conn.execute(
            """UPDATE coder_issues
               SET status='running', current_tier=?, tiers_used=?,
                   started_at = COALESCE(started_at, ?)
               WHERE job_id=? AND milestone_index=? AND service=? AND issue_id=?""",
            (tier, json.dumps(tiers), now,
             job_id, milestone_index, service, issue_id),
        )
        self._conn.commit()

    def mark_issue_finished(
        self,
        *,
        job_id: str,
        milestone_index: int,
        service: str,
        issue_id: str,
        status: str,
        target_files_written: Optional[List[str]] = None,
        test_files_written: Optional[List[str]] = None,
        last_test_output: str = "",
        summary: str = "",
        error: str = "",
        notes: Optional[List[str]] = None,
        iterations_used: Optional[int] = None,
    ) -> None:
        """Persist the final state of an issue attempt."""
        now = _now()
        sets = ["status=?", "summary=?", "error=?", "finished_at=?"]
        params: list = [status, summary, error, now]
        if target_files_written is not None:
            sets.append("target_files_written=?")
            params.append(json.dumps(target_files_written))
        if test_files_written is not None:
            sets.append("test_files_written=?")
            params.append(json.dumps(test_files_written))
        if last_test_output:
            sets.append("last_test_output=?")
            params.append(last_test_output)
        if notes is not None:
            sets.append("notes=?")
            params.append(json.dumps(notes))
        if iterations_used is not None:
            sets.append("iterations_used=?")
            params.append(iterations_used)
        params.extend([job_id, milestone_index, service, issue_id])
        self._conn.execute(
            f"UPDATE coder_issues SET {', '.join(sets)} "
            f"WHERE job_id=? AND milestone_index=? AND service=? AND issue_id=?",
            params,
        )
        self._conn.commit()

    def get_coder_issue(
        self, job_id: str, milestone_index: int,
        service: str, issue_id: str,
    ) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            """SELECT * FROM coder_issues
               WHERE job_id=? AND milestone_index=? AND service=? AND issue_id=?""",
            (job_id, milestone_index, service, issue_id),
        )
        return cur.fetchone()

    def list_coder_issues(
        self, job_id: str, milestone_index: int,
        service: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        if service is not None:
            cur = self._conn.execute(
                """SELECT * FROM coder_issues
                   WHERE job_id=? AND milestone_index=? AND service=?
                   ORDER BY issue_index, id""",
                (job_id, milestone_index, service),
            )
        else:
            cur = self._conn.execute(
                """SELECT * FROM coder_issues
                   WHERE job_id=? AND milestone_index=?
                   ORDER BY service, issue_index, id""",
                (job_id, milestone_index),
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
