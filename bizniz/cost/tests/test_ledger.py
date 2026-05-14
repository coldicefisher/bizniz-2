"""Tests for cost.ledger."""
import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from bizniz.cost.ledger import (
    CostLedger,
    LedgerEntry,
    backfill_from_costs_md,
    backfill_from_sqlite,
    get_default_ledger_path,
)


@dataclass
class _FakeCost:
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    priced: bool = True


@dataclass
class _FakeRecord:
    timestamp: str = "2026-05-06T12:00:00+00:00"
    agent: str = "engineer"
    model: str = "gemini-3-flash-preview"
    service_name: str = None
    phase: str = "implement"
    milestone_id: int = None
    job_id: str = "job-123"
    input_tokens: int = 1000
    output_tokens: int = 100
    cached_input_tokens: int = 500
    cost: _FakeCost = None

    def __post_init__(self):
        if self.cost is None:
            self.cost = _FakeCost(total_cost=0.0123, priced=True)


# ── Path resolution ────────────────────────────────────────────────────


class TestPath:
    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("BIZNIZ_COST_LEDGER", raising=False)
        p = get_default_ledger_path()
        assert p.name == "cost_ledger.jsonl"
        assert p.parent.name == ".bizniz"

    def test_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.jsonl"
        monkeypatch.setenv("BIZNIZ_COST_LEDGER", str(custom))
        assert get_default_ledger_path() == custom


# ── Append + read ──────────────────────────────────────────────────────


class TestAppendAndRead:
    def test_append_creates_file(self, tmp_path):
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        ledger.append(record=_FakeRecord(), project_slug="proj")
        assert (tmp_path / "l.jsonl").exists()

    def test_round_trip(self, tmp_path):
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        ledger.append(record=_FakeRecord(), project_slug="proj")
        entries = ledger.read_all()
        assert len(entries) == 1
        e = entries[0]
        assert e.project_slug == "proj"
        assert e.agent == "engineer"
        assert e.model == "gemini-3-flash-preview"
        assert e.input_tokens == 1000
        assert e.cached_input_tokens == 500
        assert e.cost == 0.0123
        assert e.phase == "implement"

    def test_multiple_appends(self, tmp_path):
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        for i in range(5):
            ledger.append(
                record=_FakeRecord(job_id=f"job-{i}"),
                project_slug=f"proj_{i % 2}",
            )
        entries = ledger.read_all()
        assert len(entries) == 5

    def test_append_never_raises(self, tmp_path):
        # Pass a record missing fields; should swallow.
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        ledger.append(record=object(), project_slug="x")
        # File may or may not exist; key thing is no exception.

    def test_read_missing_file_returns_empty(self, tmp_path):
        ledger = CostLedger(path=tmp_path / "absent.jsonl")
        assert ledger.read_all() == []

    def test_read_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "l.jsonl"
        path.write_text(
            "garbage line\n"
            + json.dumps({"ts": "2026-05-06T12:00:00", "agent": "x", "model": "y", "cost": 1.0}) + "\n"
            + "{not valid json\n"
        )
        ledger = CostLedger(path=path)
        entries = ledger.read_all()
        assert len(entries) == 1
        assert entries[0].agent == "x"


# ── Filters ────────────────────────────────────────────────────────────


class TestFilters:
    def _seed(self, path):
        ledger = CostLedger(path=path)
        for slug, model, day in [
            ("a", "gemini-3-flash-preview", "2026-05-04"),
            ("a", "gemini-2.5-flash-lite",  "2026-05-04"),
            ("b", "gemini-3-flash-preview", "2026-05-05"),
            ("b", "gemini-3-flash-preview", "2026-05-06"),
        ]:
            ledger.append(
                record=_FakeRecord(
                    timestamp=f"{day}T12:00:00+00:00",
                    model=model,
                    cost=_FakeCost(total_cost=0.10),
                ),
                project_slug=slug,
            )
        return ledger

    def test_filter_by_project(self, tmp_path):
        ledger = self._seed(tmp_path / "l.jsonl")
        a = ledger.read_all(project_slug="a")
        b = ledger.read_all(project_slug="b")
        assert len(a) == 2
        assert len(b) == 2
        assert all(e.project_slug == "a" for e in a)

    def test_filter_by_date_range(self, tmp_path):
        ledger = self._seed(tmp_path / "l.jsonl")
        out = ledger.read_all(
            since=dt.date(2026, 5, 5), until=dt.date(2026, 5, 5),
        )
        assert len(out) == 1
        assert out[0].project_slug == "b"

    def test_filter_by_model(self, tmp_path):
        ledger = self._seed(tmp_path / "l.jsonl")
        lite = ledger.read_all(model="gemini-2.5-flash-lite")
        assert len(lite) == 1

    def test_total(self, tmp_path):
        ledger = self._seed(tmp_path / "l.jsonl")
        assert ledger.total() == pytest.approx(0.40)
        assert ledger.total(project_slug="a") == pytest.approx(0.20)


# ── Backfill: sqlite ────────────────────────────────────────────────────


def _make_sqlite_db_with_calls(path: Path, calls):
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT, timestamp TEXT, agent TEXT, model TEXT,
            service_name TEXT, issue_id INTEGER, milestone_id INTEGER, phase TEXT,
            input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER,
            input_cost REAL, output_cost REAL, total_cost REAL, priced INTEGER
        )
    """)
    for c in calls:
        conn.execute(
            """INSERT INTO api_calls
               (timestamp, agent, model, service_name, phase, milestone_id, job_id,
                input_tokens, output_tokens, input_cost, output_cost, total_cost, priced)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            c,
        )
    conn.commit()
    conn.close()


class TestBackfillSqlite:
    def test_copies_rows(self, tmp_path):
        db_path = tmp_path / "p.db"
        _make_sqlite_db_with_calls(db_path, [
            ("2026-05-04T10:00:00", "coder", "gemini-2.5-flash", None, "code", None, "j1", 100, 50, 0.001, 0.002, 0.003, 1),
            ("2026-05-04T11:00:00", "tester", "gemini-2.5-flash", "backend", "test", None, "j1", 200, 75, 0.004, 0.005, 0.009, 1),
        ])
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        n = backfill_from_sqlite(db_path, "myproj", ledger=ledger)
        assert n == 2
        entries = ledger.read_all()
        assert len(entries) == 2
        assert all(e.project_slug == "myproj" for e in entries)
        assert {e.agent for e in entries} == {"coder", "tester"}
        assert ledger.total() == pytest.approx(0.012)

    def test_missing_table_returns_zero(self, tmp_path):
        db_path = tmp_path / "p.db"
        sqlite3.connect(str(db_path)).close()  # empty db, no tables
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        n = backfill_from_sqlite(db_path, "x", ledger=ledger)
        assert n == 0

    def test_missing_db_creates_empty_and_returns_zero(self, tmp_path):
        # sqlite3.connect() to a nonexistent path silently creates an
        # empty file. Our backfill detects no api_calls table and
        # returns 0 — never raises.
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        n = backfill_from_sqlite(tmp_path / "nowhere.db", "x", ledger=ledger)
        assert n == 0


# ── Backfill: costs.md ──────────────────────────────────────────────────


COSTS_MD_FIXTURE = """\
# Cost log

## 2026-05-06 11:17:45 — phase=provision

CostSummary(...)

### Per-call breakdown
```
  (no calls)
```

---

## 2026-05-06 11:19:29 — phase=auth

CostSummary(...)

### Per-call breakdown
```
  auth_agent  gemini-3-flash-preview  in= 5,000 out=  300  $0.0034
  auth_agent  gemini-3-flash-preview  in= 8,000 out=  150  $0.0046  cached_in=4,000
```

---
"""


class TestBackfillCostsMd:
    def test_reads_per_call_lines(self, tmp_path):
        md_path = tmp_path / "costs.md"
        md_path.write_text(COSTS_MD_FIXTURE)
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        n = backfill_from_costs_md(md_path, "p_v2", ledger=ledger)
        assert n == 2
        entries = ledger.read_all()
        assert all(e.project_slug == "p_v2" for e in entries)
        # Cached on the second one.
        cached = [e for e in entries if e.cached_input_tokens > 0]
        assert len(cached) == 1
        assert cached[0].cached_input_tokens == 4000


# ── Tracker integration ────────────────────────────────────────────────


class TestTrackerIntegration:
    def test_tracker_appends_to_ledger(self, tmp_path, monkeypatch):
        from bizniz.cost.tracker import CostTracker
        ledger = CostLedger(path=tmp_path / "l.jsonl")
        tr = CostTracker()
        tr.attach_ledger(ledger)
        tr.start_job(project_slug="myproj", problem_statement="x")
        tr.record(
            agent="engineer", model="gemini-3-flash-preview",
            input_tokens=1000, output_tokens=100,
            cached_input_tokens=500,
        )
        entries = ledger.read_all()
        assert len(entries) == 1
        assert entries[0].project_slug == "myproj"
        assert entries[0].agent == "engineer"
        assert entries[0].cached_input_tokens == 500

    def test_tracker_without_ledger_does_not_break(self):
        from bizniz.cost.tracker import CostTracker
        tr = CostTracker()
        tr.start_job(project_slug="x", problem_statement="")
        tr.record(
            agent="x", model="gpt-4o-mini",
            input_tokens=10, output_tokens=5,
        )  # no ledger attached — must not raise
