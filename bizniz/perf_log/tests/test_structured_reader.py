"""Tests for the structured-events reader (Phase 2C)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from bizniz.perf_log.events import (
    AgentCall, DecomposerResult, GateEvent, MilestoneDone,
    RateLimitEvent, ReadonlyRetryEvent, SmokeRecoveryEvent,
)
from bizniz.perf_log.structured_reader import (
    parse_emitter_jsonl,
    parse_run_artifacts,
    translate_emitter_event,
)


def _write_jsonl(path: Path, records: List[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


# ── translate_emitter_event ──────────────────────────────────────


class TestTranslateEmitterEvent:
    def test_agent_call(self):
        ev = translate_emitter_event({
            "event_type": "agent_call",
            "elapsed_s": 12.3,
            "agent": "Planner",
            "target": "m1",
            "duration_s": 4.5,
            "response_chars": 1000,
            "succeeded": True,
        })
        assert isinstance(ev, AgentCall)
        assert ev.agent == "Planner"
        assert ev.target == "m1"
        assert ev.elapsed_s == 12.3
        assert ev.duration_s == 4.5

    def test_decomposer_result(self):
        ev = translate_emitter_event({
            "event_type": "decomposer_result",
            "issue_id": "BE-001",
            "unit_count": 3,
            "confidence": 0.85,
        })
        assert isinstance(ev, DecomposerResult)
        assert ev.unit_count == 3

    def test_milestone_done(self):
        ev = translate_emitter_event({
            "event_type": "milestone_done",
            "milestone_index": 2,
            "milestone_name": "CRUD",
            "repair_iterations": 1,
        })
        assert isinstance(ev, MilestoneDone)
        assert ev.milestone_index == 2
        assert ev.repair_iterations == 1

    def test_gate_failure(self):
        ev = translate_emitter_event({
            "event_type": "gate_failure",
            "gate_name": "smoke_failed",
            "severity": "halt",
            "reason": "route 500",
        })
        assert isinstance(ev, GateEvent)
        assert ev.gate_name == "smoke_failed"
        assert ev.severity == "halt"

    def test_smoke_recovery(self):
        ev = translate_emitter_event({
            "event_type": "smoke_recovery",
            "duration_s": 91.1,
            "actions_count": 1,
            "self_reported_ok": True,
        })
        assert isinstance(ev, SmokeRecoveryEvent)
        assert ev.self_reported_ok is True

    def test_rate_limit(self):
        ev = translate_emitter_event({
            "event_type": "rate_limit",
            "detail": "usage_cap",
            "wait_s": 1234.0,
        })
        assert isinstance(ev, RateLimitEvent)
        assert ev.detail == "usage_cap"
        assert ev.wait_s == 1234.0

    def test_readonly_retry(self):
        ev = translate_emitter_event({
            "event_type": "readonly_retry",
        })
        assert isinstance(ev, ReadonlyRetryEvent)

    def test_unknown_event_type_returns_none(self):
        assert translate_emitter_event({
            "event_type": "cache_hit",  # no Phase 1 equivalent yet
        }) is None
        assert translate_emitter_event({"event_type": "tool_use"}) is None
        assert translate_emitter_event({"event_type": "weird"}) is None

    def test_gate_severity_invalid_falls_back_to_fail(self):
        ev = translate_emitter_event({
            "event_type": "gate_failure",
            "gate_name": "x",
            "severity": "bogus",
        })
        assert isinstance(ev, GateEvent)
        assert ev.severity == "fail"


# ── parse_emitter_jsonl ──────────────────────────────────────────


class TestParseEmitterJsonl:
    def test_reads_one_per_line(self, tmp_path):
        records = [
            {"event_type": "agent_call", "agent": "A", "duration_s": 1.0,
             "elapsed_s": 0.0, "response_chars": 100, "succeeded": True},
            {"event_type": "milestone_done",
             "milestone_index": 1, "milestone_name": "M1",
             "repair_iterations": 0, "elapsed_s": 60.0},
        ]
        path = _write_jsonl(tmp_path / "events.jsonl", records)
        events = parse_emitter_jsonl(path)
        assert len(events) == 2
        assert isinstance(events[0], AgentCall)
        assert isinstance(events[1], MilestoneDone)

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_emitter_jsonl(tmp_path / "missing.jsonl") == []

    def test_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            "not json\n"
            '{"event_type": "agent_call", "agent": "A", "duration_s": 1.0, "elapsed_s": 0.0}\n'
            "another bad line\n"
            '{}\n'   # missing event_type — silently dropped
            "\n",
            encoding="utf-8",
        )
        events = parse_emitter_jsonl(path)
        assert len(events) == 1
        assert isinstance(events[0], AgentCall)

    def test_unknown_event_types_filtered(self, tmp_path):
        path = _write_jsonl(tmp_path / "events.jsonl", [
            {"event_type": "agent_call", "agent": "A", "elapsed_s": 0.0,
             "duration_s": 1.0},
            {"event_type": "cache_hit", "cache_name": "X"},
            {"event_type": "phase_start", "phase": "enrich"},
        ])
        events = parse_emitter_jsonl(path)
        # Only agent_call survives.
        assert len(events) == 1


# ── parse_run_artifacts ──────────────────────────────────────────


class TestParseRunArtifacts:
    def test_prefers_jsonl_when_present(self, tmp_path):
        # Both files present — JSONL wins.
        log = tmp_path / "build.log"
        log.write_text(
            "[10:00:00] ServicePlanner(backend): AI responded in 1.0s (50 chars)\n",
            encoding="utf-8",
        )
        jsonl = _write_jsonl(tmp_path / "perf_events.jsonl", [
            {"event_type": "agent_call", "agent": "Planner",
             "duration_s": 42.0, "elapsed_s": 0.0, "response_chars": 1},
        ])
        events = parse_run_artifacts(tmp_path, log_path=log)
        # JSONL says Planner; log says ServicePlanner. We get Planner.
        assert len(events) == 1
        assert events[0].agent == "Planner"

    def test_falls_back_to_log_when_no_jsonl(self, tmp_path):
        log = tmp_path / "build.log"
        log.write_text(
            "[10:00:00] ServicePlanner(backend): AI responded in 1.0s (50 chars)\n",
            encoding="utf-8",
        )
        events = parse_run_artifacts(tmp_path, log_path=log)
        # Phase 1 regex parser recognized the line.
        assert len(events) == 1
        assert events[0].agent == "ServicePlanner"

    def test_no_jsonl_no_log_returns_empty(self, tmp_path):
        assert parse_run_artifacts(tmp_path, log_path=None) == []

    def test_empty_jsonl_falls_back_to_log(self, tmp_path):
        # JSONL exists but has no usable events → fall through to log.
        (tmp_path / "perf_events.jsonl").write_text("\n", "utf-8")
        log = tmp_path / "build.log"
        log.write_text(
            "[10:00:00] MilestoneLoop: M1 'auth' DONE (0 repair iterations)\n",
            encoding="utf-8",
        )
        events = parse_run_artifacts(tmp_path, log_path=log)
        assert len(events) == 1
        assert isinstance(events[0], MilestoneDone)
