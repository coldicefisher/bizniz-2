"""Tests for the structured perf-event emitter (Phase 2A)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from bizniz.perf_log.emitter import (
    AgentCallEvent,
    AgentRetryEvent,
    CacheHitEvent,
    CacheMissEvent,
    GateFailureEvent,
    MilestoneDoneEvent,
    NullPerfEmitter,
    PerfEmitter,
    PhaseEndEvent,
    PhaseStartEvent,
    RateLimitEvent,
    ReadonlyRetryEvent,
    DecomposerResultEvent,
)


def _read_events(path: Path) -> List[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── Construction guards ──────────────────────────────────────────


class TestConstruction:
    def test_requires_output_or_memory(self):
        with pytest.raises(ValueError, match="output_path or in_memory"):
            PerfEmitter()

    def test_mutually_exclusive(self, tmp_path):
        with pytest.raises(ValueError, match="mutually exclusive"):
            PerfEmitter(output_path=tmp_path / "p.jsonl", in_memory=True)

    def test_in_memory_works_without_path(self):
        em = PerfEmitter(in_memory=True)
        em.agent_call(agent="X", duration_s=1.0)
        assert len(em.collected) == 1


# ── In-memory emission ───────────────────────────────────────────


class TestInMemory:
    def test_collected_events_in_order(self):
        em = PerfEmitter(in_memory=True)
        em.agent_call(agent="Planner", duration_s=12.3)
        em.cache_hit(cache_name="design_lock")
        em.gate_failure(gate_name="smoke_failed", severity="halt")
        assert len(em.collected) == 3
        assert em.collected[0].event_type == "agent_call"
        assert em.collected[1].event_type == "cache_hit"
        assert em.collected[2].event_type == "gate_failure"

    def test_agent_call_fields_threaded(self):
        em = PerfEmitter(in_memory=True)
        em.agent_call(
            agent="QualityEngineer.enrich",
            target="backend",
            model="claude-cli",
            duration_s=307.7,
            response_chars=50650,
            tokens_in=12000, tokens_out=8000,
            permanent_attempts=1, transient_attempts=2,
        )
        ev = em.collected[0]
        assert isinstance(ev, AgentCallEvent)
        assert ev.target == "backend"
        assert ev.tokens_in == 12000
        assert ev.transient_attempts == 2

    def test_invalid_severity_defaults_to_fail(self):
        em = PerfEmitter(in_memory=True)
        em.gate_failure(gate_name="x", severity="bogus")
        assert em.collected[0].severity == "fail"

    def test_invalid_retry_classification_defaults(self):
        em = PerfEmitter(in_memory=True)
        em.agent_retry(agent="X", classification="weird")
        assert em.collected[0].classification == "permanent"


# ── Disk emission ────────────────────────────────────────────────


class TestDiskEmission:
    def test_writes_jsonl_one_event_per_line(self, tmp_path):
        path = tmp_path / "events.jsonl"
        em = PerfEmitter(output_path=path)
        em.agent_call(agent="A", duration_s=1.0)
        em.cache_hit(cache_name="X")
        em.milestone_done(
            milestone_index=2, milestone_name="M2", repair_iterations=1,
        )
        events = _read_events(path)
        assert len(events) == 3
        assert events[0]["event_type"] == "agent_call"
        assert events[2]["event_type"] == "milestone_done"
        assert events[2]["milestone_index"] == 2

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "nested" / "perf_events.jsonl"
        em = PerfEmitter(output_path=path)
        em.agent_call(agent="A")
        assert path.is_file()

    def test_appends_across_constructions(self, tmp_path):
        # Simulating two pipeline runs writing to the same file
        # (resume after crash). Both events should land in order.
        path = tmp_path / "perf.jsonl"
        em1 = PerfEmitter(output_path=path)
        em1.agent_call(agent="run1")
        em2 = PerfEmitter(output_path=path)
        em2.agent_call(agent="run2")
        events = _read_events(path)
        assert [e["agent"] for e in events] == ["run1", "run2"]

    def test_unicode_in_payload(self, tmp_path):
        # Emoji + non-ASCII in messages should round-trip.
        path = tmp_path / "p.jsonl"
        em = PerfEmitter(output_path=path)
        em.gate_failure(gate_name="ñ", reason="🚧 something")
        events = _read_events(path)
        assert events[0]["gate_name"] == "ñ"
        assert "🚧" in events[0]["reason"]

    def test_disk_write_failure_logged_not_raised(self, tmp_path):
        # Make the output dir unwritable post-construction.
        path = tmp_path / "events.jsonl"
        em = PerfEmitter(output_path=path)
        # Now lock the file out — chmod the parent dir.
        path.parent.chmod(0o500)
        try:
            statuses: List[str] = []
            em._on_status = lambda m: statuses.append(m)
            em.agent_call(agent="X")
            # No raise; status logged.
            # (chmod doesn't always reliably block in tests; just
            # verify no exception escapes.)
        finally:
            path.parent.chmod(0o700)


# ── Job-id stamping + elapsed time ───────────────────────────────


class TestStamping:
    def test_job_id_stamped_when_set(self):
        em = PerfEmitter(in_memory=True, job_id="20260516_120000")
        em.agent_call(agent="A")
        assert em.collected[0].job_id == "20260516_120000"

    def test_elapsed_computed_from_time_source(self):
        ticks = [100.0, 105.5]    # start, then first emit
        def fake_clock():
            return ticks.pop(0)
        em = PerfEmitter(in_memory=True, time_source=fake_clock)
        em.agent_call(agent="A")
        assert em.collected[0].elapsed_s == 5.5


# ── Convenience emitters ─────────────────────────────────────────


class TestConvenienceEmitters:
    def test_cache_hit_and_miss(self):
        em = PerfEmitter(in_memory=True)
        em.cache_hit(cache_name="plan_cache", key="abc")
        em.cache_miss(cache_name="plan_cache", reason="input mtime changed")
        assert em.collected[0].event_type == "cache_hit"
        assert em.collected[1].event_type == "cache_miss"
        assert em.collected[1].reason == "input mtime changed"

    def test_phase_start_end(self):
        em = PerfEmitter(in_memory=True)
        em.phase_start(phase="enrich")
        em.phase_end(phase="enrich", duration_s=42.0, passed=True)
        assert em.collected[0].phase == "enrich"
        assert em.collected[1].duration_s == 42.0

    def test_rate_limit(self):
        em = PerfEmitter(in_memory=True)
        em.rate_limit(detail="usage_cap", wait_s=1800.0)
        ev = em.collected[0]
        assert isinstance(ev, RateLimitEvent)
        assert ev.detail == "usage_cap"
        assert ev.wait_s == 1800.0

    def test_decomposer_result(self):
        em = PerfEmitter(in_memory=True)
        em.decomposer_result(issue_id="BE-001", unit_count=3, confidence=0.85)
        ev = em.collected[0]
        assert isinstance(ev, DecomposerResultEvent)
        assert ev.unit_count == 3

    def test_readonly_retry_default_shape(self):
        em = PerfEmitter(in_memory=True)
        em.readonly_retry()
        ev = em.collected[0]
        assert isinstance(ev, ReadonlyRetryEvent)
        assert ev.shape == "readonly-database"


# ── NullPerfEmitter ──────────────────────────────────────────────


class TestNullEmitter:
    def test_no_op_emit(self):
        em = NullPerfEmitter()
        em.agent_call(agent="X")
        em.cache_hit(cache_name="X")
        em.gate_failure(gate_name="x", severity="halt")
        # Nothing collected, no exception.
        assert em.collected == []

    def test_safe_to_call_without_path(self):
        # NullPerfEmitter doesn't need output_path / in_memory args.
        NullPerfEmitter().agent_retry(agent="X")
