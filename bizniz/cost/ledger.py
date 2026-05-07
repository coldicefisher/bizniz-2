"""Persistent cumulative cost ledger.

Writes one JSONL line per LLM call to ``~/.bizniz/cost_ledger.jsonl``
(override via ``BIZNIZ_COST_LEDGER`` env var). Survives project-dir
wipes because it lives outside ``~/bizniz_projects/``.

Format: append-only JSONL, one record per API call:

  {"ts": "...", "project_slug": "pet_groomer", "job_id": "...",
   "agent": "engineer", "model": "gemini-3-flash-preview",
   "phase": "implement", "milestone_id": null,
   "input_tokens": 65400, "output_tokens": 487,
   "cached_input_tokens": 56950, "cost": 0.0128, "priced": true}

Append-only by design: cumulative spend is the sum across all lines
ever written, regardless of project lifecycle. Read via ``read_all``
or filter by project / date / model.

Backfill helpers exist for migrating existing data from sqlite
project DBs (``backfill_from_sqlite``) and v2 costs.md files
(``backfill_from_costs_md``). Run them once after enabling the
ledger to capture historical spend.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional


DEFAULT_LEDGER_PATH = Path.home() / ".bizniz" / "cost_ledger.jsonl"


def get_default_ledger_path() -> Path:
    """Resolve the ledger path honoring ``BIZNIZ_COST_LEDGER`` env var."""
    env = os.environ.get("BIZNIZ_COST_LEDGER")
    if env:
        return Path(env).expanduser()
    return DEFAULT_LEDGER_PATH


@dataclass
class LedgerEntry:
    """One line of the ledger after parsing back into a dataclass."""
    ts: dt.datetime
    project_slug: str
    job_id: Optional[str]
    agent: str
    model: str
    phase: Optional[str]
    milestone_id: Optional[int]
    service_name: Optional[str]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost: float
    priced: bool


class CostLedger:
    """Append-only JSONL ledger of all LLM-call spend.

    Thread-safe at the OS level (single ``open(..., "a")`` per write
    is atomic for small writes on POSIX). No internal locking — fine
    for our bursty single-process workload.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else get_default_ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        record,
        project_slug: str,
    ) -> None:
        """Append one CallRecord to the ledger.

        ``record`` is a ``bizniz.cost.tracker.CallRecord`` (or anything
        with the same field surface). Best-effort — never raises.
        """
        try:
            cost = getattr(record, "cost", None)
            entry = {
                "ts": getattr(record, "timestamp", _iso_now()),
                "project_slug": project_slug or "",
                "job_id": getattr(record, "job_id", None),
                "agent": getattr(record, "agent", "unknown"),
                "model": getattr(record, "model", "unknown"),
                "phase": getattr(record, "phase", None),
                "milestone_id": getattr(record, "milestone_id", None),
                "service_name": getattr(record, "service_name", None),
                "input_tokens": int(getattr(record, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(record, "output_tokens", 0) or 0),
                "cached_input_tokens": int(
                    getattr(record, "cached_input_tokens", 0) or 0
                ),
                "cost": float(getattr(cost, "total_cost", 0.0) or 0.0),
                "priced": bool(getattr(cost, "priced", True)),
            }
            line = json.dumps(entry, default=str) + "\n"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            # Ledger is best-effort; never break a real call.
            pass

    def read_all(
        self,
        *,
        since: Optional[dt.date] = None,
        until: Optional[dt.date] = None,
        project_slug: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[LedgerEntry]:
        """Read all entries optionally filtered by date/project/model.
        Malformed lines are skipped silently."""
        if not self.path.exists():
            return []
        out: List[LedgerEntry] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
                ts = dt.datetime.fromisoformat(d["ts"])
            except Exception:
                continue
            if since and ts.date() < since:
                continue
            if until and ts.date() > until:
                continue
            if project_slug and d.get("project_slug") != project_slug:
                continue
            if model and d.get("model") != model:
                continue
            out.append(LedgerEntry(
                ts=ts,
                project_slug=d.get("project_slug") or "",
                job_id=d.get("job_id"),
                agent=d.get("agent", "unknown"),
                model=d.get("model", "unknown"),
                phase=d.get("phase"),
                milestone_id=d.get("milestone_id"),
                service_name=d.get("service_name"),
                input_tokens=int(d.get("input_tokens", 0) or 0),
                output_tokens=int(d.get("output_tokens", 0) or 0),
                cached_input_tokens=int(d.get("cached_input_tokens", 0) or 0),
                cost=float(d.get("cost", 0.0) or 0.0),
                priced=bool(d.get("priced", True)),
            ))
        return out

    def total(self, **filters) -> float:
        """Sum of ``cost`` across all matching entries."""
        return sum(e.cost for e in self.read_all(**filters))


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── Backfill helpers ────────────────────────────────────────────────────


def backfill_from_sqlite(
    project_db_path: Path,
    project_slug: str,
    ledger: Optional[CostLedger] = None,
) -> int:
    """Read every row from a project's ``api_calls`` table and append
    to the ledger. Returns count appended.

    Idempotent in the sense that running twice doubles the entries —
    callers should backfill ONCE per project DB. To re-run safely
    after a partial failure, delete the ledger and re-backfill from
    scratch.
    """
    import sqlite3
    if ledger is None:
        ledger = CostLedger()
    conn = sqlite3.connect(str(project_db_path))
    try:
        cur = conn.execute(
            "SELECT timestamp, agent, model, service_name, phase, "
            "milestone_id, job_id, input_tokens, output_tokens, "
            "input_cost, output_cost, total_cost, priced "
            "FROM api_calls"
        )
    except sqlite3.OperationalError:
        # No api_calls table; nothing to migrate.
        return 0
    count = 0
    for row in cur:
        rec_obj = _SQLiteShim(*row)
        ledger.append(record=rec_obj, project_slug=project_slug)
        count += 1
    conn.close()
    return count


def backfill_from_costs_md(
    costs_md_path: Path,
    project_slug: str,
    ledger: Optional[CostLedger] = None,
) -> int:
    """Read a v2 costs.md file (one entry per --phase invocation,
    each with a ``### Per-call breakdown`` section) and append every
    per-call line to the ledger.

    Uses ``bizniz.cost.audit.parse_costs_md`` — keep regex changes
    in sync there.
    """
    from bizniz.cost.audit import parse_costs_md
    if ledger is None:
        ledger = CostLedger()
    entries = parse_costs_md(costs_md_path)
    count = 0
    for e in entries:
        rec_obj = _CostsMdShim(e)
        ledger.append(record=rec_obj, project_slug=project_slug)
        count += 1
    return count


@dataclass
class _SQLiteShim:
    """Minimal duck-type for ``ledger.append(record=...)`` from a
    sqlite ``api_calls`` row (column order matches the SELECT above)."""
    timestamp: str
    agent: str
    model: str
    service_name: Optional[str]
    phase: Optional[str]
    milestone_id: Optional[int]
    job_id: Optional[str]
    input_tokens: int
    output_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float
    priced: int

    @property
    def cost(self):
        # Matches CallCost surface enough for ledger.append.
        return _CostShim(
            input_cost=float(self.input_cost or 0.0),
            output_cost=float(self.output_cost or 0.0),
            total_cost=float(self.total_cost or 0.0),
            priced=bool(self.priced),
        )

    cached_input_tokens: int = 0
    issue_id: Optional[int] = None


@dataclass
class _CostShim:
    input_cost: float
    output_cost: float
    total_cost: float
    priced: bool


class _CostsMdShim:
    """Adapt ``audit.TrackerEntry`` to the ledger.append surface."""

    def __init__(self, e):
        self.timestamp = e.timestamp.isoformat()
        self.agent = e.agent
        self.model = e.model
        self.service_name = None
        self.phase = e.phase
        self.milestone_id = None
        self.job_id = None
        self.input_tokens = e.input_tokens
        self.output_tokens = e.output_tokens
        self.cached_input_tokens = e.cached_input_tokens
        self.cost = _CostShim(
            input_cost=0.0, output_cost=0.0,
            total_cost=e.cost, priced=True,
        )
