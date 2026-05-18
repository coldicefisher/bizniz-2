"""Tests for the v3 refactorer's per-candidate decision gate.

The gate is a single-shot LLM classifier — tests use a fake
``llm_invoker`` so we can exercise the parsing, defensive paths,
and prompt-shape contracts without an actual API call.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.refactorer.decision_gate import (
    CandidateContext,
    DecisionGate,
    GateDecision,
    _parse_decision,
)


# ── Parsing ──────────────────────────────────────────────────────


class TestParseDecision:
    def test_clean_yes_decision(self):
        d = _parse_decision(
            '{"refactor": true, "rationale": "real duplication",'
            ' "confidence": 0.9}'
        )
        assert d.refactor is True
        assert "duplication" in d.rationale
        assert d.confidence == 0.9

    def test_clean_no_decision(self):
        d = _parse_decision(
            '{"refactor": false, "rationale": "boilerplate"}'
        )
        assert d.refactor is False
        # Confidence defaults to 0.5 when omitted.
        assert d.confidence == 0.5

    def test_strips_code_fences(self):
        # LLMs often wrap JSON in ```json ... ```
        raw = '```json\n{"refactor": true, "rationale": "ok"}\n```'
        d = _parse_decision(raw)
        assert d.refactor is True
        assert d.rationale == "ok"

    def test_strips_plain_code_fences(self):
        raw = '```\n{"refactor": false, "rationale": "skip"}\n```'
        d = _parse_decision(raw)
        assert d.refactor is False

    def test_extracts_object_from_surrounding_prose(self):
        raw = (
            "Sure, here's my analysis:\n"
            '{"refactor": true, "rationale": "real"}\n'
            "Hope that helps!"
        )
        d = _parse_decision(raw)
        assert d.refactor is True

    def test_empty_response_skips(self):
        d = _parse_decision("")
        assert d.refactor is False
        assert "empty" in d.rationale.lower()

    def test_non_json_response_skips(self):
        d = _parse_decision("I think we should refactor!")
        assert d.refactor is False
        assert d.confidence == 0.0

    def test_missing_refactor_field_skips(self):
        d = _parse_decision('{"rationale": "thought about it"}')
        assert d.refactor is False
        assert "bool" in d.rationale.lower()

    def test_non_bool_refactor_field_skips(self):
        d = _parse_decision('{"refactor": "yes", "rationale": "x"}')
        assert d.refactor is False

    def test_confidence_clamped_to_valid_range(self):
        d = _parse_decision(
            '{"refactor": true, "rationale": "x", "confidence": 2.5}'
        )
        assert d.confidence == 1.0
        d = _parse_decision(
            '{"refactor": true, "rationale": "x", "confidence": -0.3}'
        )
        assert d.confidence == 0.0

    def test_invalid_confidence_falls_back_to_default(self):
        d = _parse_decision(
            '{"refactor": true, "rationale": "x", "confidence": "high"}'
        )
        # Falls back to 0.5 default.
        assert d.confidence == 0.5


# ── Gate dispatch ────────────────────────────────────────────────


def _candidate(
    kind: str = "cpd_duplicate",
    summary: str = "duplicated email-validation logic",
) -> CandidateContext:
    return CandidateContext(
        kind=kind,
        summary=summary,
        file_path="app/api/routes/auth.py",
        line_range=(42, 67),
        snippet="def validate_email(addr: str) -> bool:\n    ...",
        extra={"occurrence_count": 3},
    )


class TestDecisionGate:
    def test_passes_through_clean_decision(self):
        invoker = MagicMock(
            return_value='{"refactor": true, "rationale": "3 places", "confidence": 0.8}'
        )
        gate = DecisionGate(llm_invoker=invoker)
        d = gate.decide(_candidate())
        assert d.refactor is True
        assert d.confidence == 0.8

    def test_invoker_exception_yields_conservative_skip(self):
        invoker = MagicMock(side_effect=RuntimeError("api down"))
        gate = DecisionGate(llm_invoker=invoker)
        d = gate.decide(_candidate())
        assert d.refactor is False
        assert "api down" in d.rationale.lower()
        assert d.confidence == 0.0

    def test_prompt_includes_candidate_summary(self):
        seen = {}

        def fake_invoker(system: str, user: str) -> str:
            seen["system"] = system
            seen["user"] = user
            return '{"refactor": false, "rationale": "ok"}'

        gate = DecisionGate(llm_invoker=fake_invoker)
        gate.decide(_candidate(summary="my-unique-marker"))
        assert "my-unique-marker" in seen["user"]
        # Snippet appears.
        assert "validate_email" in seen["user"]
        # Kind is in the prompt frame.
        assert "cpd_duplicate" in seen["user"]
        # Extras list appears.
        assert "occurrence_count: 3" in seen["user"]

    def test_prompt_frames_each_kind(self):
        invoker = MagicMock(return_value='{"refactor": false, "rationale": "x"}')
        gate = DecisionGate(llm_invoker=invoker)
        for kind in ("anti_pattern", "cpd_duplicate", "misplaced_logic"):
            gate.decide(_candidate(kind=kind))
        # System prompt is the same across all (frames all three kinds);
        # user prompt parameterizes by kind. Each call labeled accordingly.
        for call in invoker.call_args_list:
            sys, user = call.args
            assert "anti_pattern" in sys
            assert "cpd_duplicate" in sys
            assert "misplaced_logic" in sys

    def test_decide_all_handles_per_candidate_errors(self):
        # First call raises, second succeeds — both decisions returned.
        invoker = MagicMock(side_effect=[
            RuntimeError("boom"),
            '{"refactor": true, "rationale": "second one ok"}',
        ])
        gate = DecisionGate(llm_invoker=invoker)
        out = gate.decide_all([_candidate(), _candidate()])
        assert len(out) == 2
        assert out[0].refactor is False
        assert "boom" in out[0].rationale.lower()
        assert out[1].refactor is True

    def test_long_snippets_get_truncated_to_safe_size(self):
        seen = {}
        def fake(_sys, user):
            seen["user"] = user
            return '{"refactor": false, "rationale": "x"}'

        long_snippet = "x" * 10000
        c = CandidateContext(
            kind="cpd_duplicate",
            summary="x", file_path="x.py",
            snippet=long_snippet,
        )
        DecisionGate(llm_invoker=fake).decide(c)
        # Truncated to 2000 chars + the surrounding template — way
        # under any model's context limit.
        assert len(seen["user"]) < 4000
