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
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        try:
            import os
            os.chmod(str(self._db_path), 0o666)
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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
