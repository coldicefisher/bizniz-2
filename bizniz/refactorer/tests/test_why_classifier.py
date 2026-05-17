"""Tests for the LLM-driven why-classifier (Phase D)."""
from __future__ import annotations

from typing import Dict, List

import pytest

from bizniz.refactorer.anti_patterns import AntiPatternFinding
from bizniz.refactorer.why_classifier import (
    WhyClassifier,
    WhyReport,
    WhyVerdict,
    _gather_context,
    _parse_verdict,
)


def _finding(pattern: str = "drop_all_in_test",
             line: int = 5, path: str = "/x/tests/conftest.py") -> AntiPatternFinding:
    return AntiPatternFinding(
        pattern=pattern, severity="critical",
        path=path, line=line,
        snippet="Base.metadata.drop_all(conn)",
        description="drop_all in test fixture",
        suggested_fix="use transactional rollback",
    )


def _file_reader(contents: Dict[str, str]):
    return lambda p: contents[p]


# ── Context gathering ────────────────────────────────────────────


class TestGatherContext:
    def test_window_around_finding(self):
        text = "\n".join(f"line {i}" for i in range(1, 21))
        ctx, start, end = _gather_context(
            _finding(line=10), text, radius=3,
        )
        assert start == 7
        assert end == 13
        assert "line 7" in ctx
        assert "line 13" in ctx
        assert "line 6" not in ctx
        assert "line 14" not in ctx

    def test_clamps_at_file_start(self):
        text = "\n".join(f"line {i}" for i in range(1, 6))
        ctx, start, end = _gather_context(
            _finding(line=2), text, radius=5,
        )
        assert start == 1
        assert end == 5

    def test_clamps_at_file_end(self):
        text = "\n".join(f"line {i}" for i in range(1, 11))
        ctx, start, end = _gather_context(
            _finding(line=9), text, radius=5,
        )
        assert start == 4
        assert end == 10


# ── Verdict parsing ──────────────────────────────────────────────


class TestParseVerdict:
    def test_full_valid_json(self):
        raw = {
            "hypothesis": "test author wanted isolation",
            "confidence": 0.85,
            "recommended_action": "rewrite",
            "rationale": "transactional rollback covers same intent",
        }
        v = _parse_verdict(raw, _finding())
        assert v.hypothesis == "test author wanted isolation"
        assert v.confidence == 0.85
        assert v.recommended_action == "rewrite"

    def test_confidence_clamped(self):
        assert _parse_verdict({"confidence": 1.5}, _finding()).confidence == 1.0
        assert _parse_verdict({"confidence": -0.5}, _finding()).confidence == 0.0

    def test_non_numeric_confidence(self):
        assert _parse_verdict({"confidence": "high"}, _finding()).confidence == 0.0

    def test_unknown_action_falls_back(self):
        v = _parse_verdict({"recommended_action": "delete"}, _finding())
        assert v.recommended_action == "surface"

    def test_missing_fields_default(self):
        v = _parse_verdict({}, _finding())
        assert v.confidence == 0.0
        assert v.recommended_action == "surface"
        assert v.hypothesis == ""


# ── Classifier ───────────────────────────────────────────────────


class TestClassify:
    def test_happy_path(self, tmp_path):
        path = str(tmp_path / "conftest.py")
        contents = {path: "line 1\nline 2\nBase.metadata.drop_all(conn)\nline 4"}
        def fake_llm(finding, prompt):
            return {
                "hypothesis": "per-test isolation",
                "confidence": 0.9,
                "recommended_action": "rewrite",
                "rationale": "transactional rollback is standard",
            }
        classifier = WhyClassifier(
            llm_invoker=fake_llm,
            file_reader=_file_reader(contents),
        )
        v = classifier.classify(_finding(line=3, path=path))
        assert v.confidence == 0.9
        assert v.recommended_action == "rewrite"

    def test_llm_returns_none_defaults_to_surface(self, tmp_path):
        path = str(tmp_path / "x.py")
        contents = {path: "line"}
        classifier = WhyClassifier(
            llm_invoker=lambda f, p: None,
            file_reader=_file_reader(contents),
        )
        v = classifier.classify(_finding(line=1, path=path))
        assert v.recommended_action == "surface"
        assert v.confidence == 0.0

    def test_unreadable_file_defaults_to_surface(self):
        classifier = WhyClassifier(
            llm_invoker=lambda f, p: {"confidence": 1.0, "recommended_action": "rewrite"},
            file_reader=lambda _: "",  # empty → unreadable
        )
        v = classifier.classify(_finding())
        assert v.recommended_action == "surface"
        assert v.confidence == 0.0
        # LLM should NOT have been called with empty context.
        assert "(file unreadable)" in v.hypothesis

    def test_prompt_includes_finding_metadata(self):
        prompts: List[str] = []
        def fake(finding, prompt):
            prompts.append(prompt)
            return {"confidence": 0.5, "recommended_action": "surface"}
        contents = {"/x/y.py": "x = 1\n" * 20}
        classifier = WhyClassifier(
            llm_invoker=fake,
            file_reader=_file_reader(contents),
        )
        classifier.classify(_finding(line=10, path="/x/y.py"))
        assert len(prompts) == 1
        assert "drop_all_in_test" in prompts[0]
        assert "/x/y.py" in prompts[0]
        assert "critical" in prompts[0].lower()


class TestClassifyAll:
    def test_aggregates_into_report(self, tmp_path):
        path = str(tmp_path / "conftest.py")
        contents = {path: "line\n" * 20}
        findings = [
            _finding(pattern="drop_all_in_test", line=5, path=path),
            _finding(pattern="bare_except", line=10, path=path),
        ]
        verdicts_iter = iter([
            {"confidence": 0.9, "recommended_action": "rewrite"},
            {"confidence": 0.3, "recommended_action": "surface"},
        ])
        classifier = WhyClassifier(
            llm_invoker=lambda f, p: next(verdicts_iter),
            file_reader=_file_reader(contents),
        )
        report = classifier.classify_all(findings)
        assert len(report.verdicts) == 2

    def test_auto_fix_candidates_filter(self, tmp_path):
        # Mix: 1 high-conf rewrite, 1 low-conf rewrite, 1 surface.
        path = str(tmp_path / "x.py")
        contents = {path: "x\n" * 20}
        findings = [_finding(line=i, path=path) for i in (1, 5, 10)]
        verdicts_iter = iter([
            {"confidence": 0.85, "recommended_action": "rewrite"},
            {"confidence": 0.4, "recommended_action": "rewrite"},
            {"confidence": 0.9, "recommended_action": "surface"},
        ])
        classifier = WhyClassifier(
            llm_invoker=lambda f, p: next(verdicts_iter),
            file_reader=_file_reader(contents),
        )
        report = classifier.classify_all(findings)
        auto = report.auto_fix_candidates(min_confidence=0.7)
        assert len(auto) == 1
        assert auto[0].confidence == 0.85
        surface = report.surface_candidates()
        # Low-conf rewrite + the explicit surface = 2
        assert len(surface) == 2
