"""
ProjectScope — Scoped accessor for project-level data.

Drop-in replacement for ProjectDB, backed by the unified BiznizDB.
All queries are automatically scoped to project_id.
"""

import json
from typing import Optional, List

from bizniz.db.bizniz_db import _now

if False:  # TYPE_CHECKING
    from bizniz.db.bizniz_db import BiznizDB


class ProjectScope:

    def __init__(self, db: "BiznizDB", project_id: str):
        self._db = db
        self._project_id = project_id

    def _execute(self, sql, params=()):
        return self._db._execute(sql, params)

    def _commit(self):
        self._db._commit()

    # ── Services ──────────────────────────────────────────────────────────────────

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
        sql = self._db._upsert_sql(
            "services",
            ["project_id", "name", "service_type", "framework", "language",
             "workspace_path", "image_name", "status", "created_at", "updated_at"],
            ["project_id", "name"],
            ["service_type", "framework", "language", "workspace_path",
             "image_name", "status", "updated_at"],
        )
        cur = self._execute(
            sql,
            (self._project_id, name, service_type, framework, language,
             workspace_path, image_name, "open", now, now),
        )
        self._commit()
        return cur.lastrowid

    def update_service_status(self, name: str, status: str):
        self._execute(
            "UPDATE services SET status = ?, updated_at = ? WHERE name = ? AND project_id = ?",
            (status, _now(), name, self._project_id),
        )
        self._commit()

    def update_service_image(self, name: str, image_name: str):
        self._execute(
            "UPDATE services SET image_name = ?, updated_at = ? WHERE name = ? AND project_id = ?",
            (image_name, _now(), name, self._project_id),
        )
        self._commit()

    def get_services(self):
        cur = self._execute(
            "SELECT * FROM services WHERE project_id = ? ORDER BY name",
            (self._project_id,),
        )
        return cur.fetchall()

    def get_service(self, name: str):
        cur = self._execute(
            "SELECT * FROM services WHERE name = ? AND project_id = ?",
            (name, self._project_id),
        )
        return cur.fetchone()

    # ── Architecture Snapshots ────────────────────────────────────────────────────

    def save_architecture_snapshot(
        self, snapshot_json: str, description: str = "",
    ) -> int:
        cur = self._execute(
            "SELECT COALESCE(MAX(version), 0) AS max_ver FROM architecture_snapshots WHERE project_id = ?",
            (self._project_id,),
        )
        next_version = cur.fetchone()["max_ver"] + 1
        cur = self._execute(
            """INSERT INTO architecture_snapshots
               (project_id, snapshot_json, version, description, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (self._project_id, snapshot_json, next_version, description, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_architecture_changes(self):
        cur = self._execute(
            "SELECT * FROM architecture_snapshots WHERE project_id = ? ORDER BY version",
            (self._project_id,),
        )
        return cur.fetchall()

    def get_latest_architecture(self):
        cur = self._execute(
            "SELECT * FROM architecture_snapshots WHERE project_id = ? ORDER BY version DESC LIMIT 1",
            (self._project_id,),
        )
        return cur.fetchone()

    # ── Issue Log ─────────────────────────────────────────────────────────────────

    def log_issue(
        self,
        service_name: str,
        title: str,
        description: str,
        status: str = "open",
    ) -> int:
        cur = self._execute(
            """INSERT INTO issue_log
               (project_id, service_name, issue_title, issue_description, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (self._project_id, service_name, title, description, status, _now()),
        )
        self._commit()
        return cur.lastrowid

    def update_issue(
        self,
        issue_id: int,
        status: str,
        strategy_used: Optional[str] = None,
        iterations: Optional[int] = None,
    ):
        self._execute(
            """UPDATE issue_log
               SET status = ?, strategy_used = COALESCE(?, strategy_used),
                   iterations = COALESCE(?, iterations)
               WHERE id = ? AND project_id = ?""",
            (status, strategy_used, iterations, issue_id, self._project_id),
        )
        self._commit()

    def close_issue(
        self,
        issue_id: int,
        strategy_used: Optional[str] = None,
        iterations: Optional[int] = None,
    ):
        self._execute(
            """UPDATE issue_log
               SET status = 'closed', closed_at = ?,
                   strategy_used = COALESCE(?, strategy_used),
                   iterations = COALESCE(?, iterations)
               WHERE id = ? AND project_id = ?""",
            (_now(), strategy_used, iterations, issue_id, self._project_id),
        )
        self._commit()

    def get_all_issues(self, service_name: Optional[str] = None):
        if service_name is not None:
            cur = self._execute(
                "SELECT * FROM issue_log WHERE service_name = ? AND project_id = ? ORDER BY created_at",
                (service_name, self._project_id),
            )
        else:
            cur = self._execute(
                "SELECT * FROM issue_log WHERE project_id = ? ORDER BY created_at",
                (self._project_id,),
            )
        return cur.fetchall()

    def get_open_issues(self, service_name: Optional[str] = None):
        if service_name is not None:
            cur = self._execute(
                """SELECT * FROM issue_log
                   WHERE service_name = ? AND project_id = ? AND status = 'open'
                   ORDER BY created_at""",
                (service_name, self._project_id),
            )
        else:
            cur = self._execute(
                "SELECT * FROM issue_log WHERE project_id = ? AND status = 'open' ORDER BY created_at",
                (self._project_id,),
            )
        return cur.fetchall()

    # ── Build Log ─────────────────────────────────────────────────────────────────

    def log_build_event(
        self,
        service_name: str,
        event_type: str,
        success: bool,
        detail: str = "",
    ) -> int:
        cur = self._execute(
            """INSERT INTO build_log
               (project_id, service_name, event_type, success, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (self._project_id, service_name, event_type, int(success), detail, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_build_log(self, service_name: Optional[str] = None):
        if service_name is not None:
            cur = self._execute(
                "SELECT * FROM build_log WHERE service_name = ? AND project_id = ? ORDER BY created_at",
                (service_name, self._project_id),
            )
        else:
            cur = self._execute(
                "SELECT * FROM build_log WHERE project_id = ? ORDER BY created_at",
                (self._project_id,),
            )
        return cur.fetchall()

    # ── Drift Events ──────────────────────────────────────────────────────────────

    def log_drift(
        self,
        service_name: str,
        drift_files: list,
        resolution: str = "",
    ) -> int:
        cur = self._execute(
            """INSERT INTO drift_events
               (project_id, service_name, drift_files_json, resolution, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (self._project_id, service_name, json.dumps(drift_files), resolution, _now()),
        )
        self._commit()
        return cur.lastrowid

    def get_drift_events(self, service_name: Optional[str] = None):
        if service_name is not None:
            cur = self._execute(
                "SELECT * FROM drift_events WHERE service_name = ? AND project_id = ? ORDER BY created_at",
                (service_name, self._project_id),
            )
        else:
            cur = self._execute(
                "SELECT * FROM drift_events WHERE project_id = ? ORDER BY created_at",
                (self._project_id,),
            )
        return cur.fetchall()

    # ── Lifecycle ─────────────────────────────────────────────────────────────────

    def close(self):
        pass  # Shared connection; closed by BiznizDB

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
