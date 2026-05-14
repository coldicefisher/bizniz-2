"""Tests for review_for_hallucinations — the AI-driven reviewer that
replaced the hardcoded path-token guard.

The AI call itself is mocked; we test the plumbing:
- Empty input is a clean pass (no API call burned).
- Response parsing tolerates fenced markdown, surrounding prose, etc.
- Soft-fail behavior on AI errors (skipped_reason set, clean=True).
- has_blockers / blockers helpers.
- collect_changed_files honors extension + skip-dir filters.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.reviewers.hallucination_review import (
    HallucinationReport,
    SuspiciousFile,
    _parse_review_response,
    collect_changed_files,
    review_for_hallucinations,
)


def _ai_returning(text: str):
    """Build a mock AI client whose get_text() returns the given response."""
    client = MagicMock()
    client.get_text.return_value = (text, "job-1", [])
    return client


def test_empty_changed_files_is_clean_pass_no_api_call():
    client = MagicMock()
    report = review_for_hallucinations(
        problem_statement="any",
        changed_files={},
        ai_client=client,
    )
    assert report.clean is True
    assert report.skipped_reason == "empty_changed_files"
    client.get_text.assert_not_called()


def test_clean_response_parses_to_clean_report():
    client = _ai_returning(
        '{"clean": true, "summary": "looks fine", "suspicious_files": []}'
    )
    report = review_for_hallucinations(
        problem_statement="property manager",
        changed_files={"app/models/property.py": "class Property: pass"},
        ai_client=client,
    )
    assert report.clean is True
    assert report.suspicious_files == []
    assert report.has_blockers is False


def test_blocker_response_surfaces_files_with_severity():
    client = _ai_returning(
        '{"clean": false, "summary": "grooming bleed", "suspicious_files": ['
        '  {"filepath": "app/api/routes/grooming.py", "reason": "no grounding", "severity": "blocker"},'
        '  {"filepath": "app/utils/helper.py", "reason": "tangential", "severity": "warning"}'
        ']}'
    )
    report = review_for_hallucinations(
        problem_statement="property manager",
        changed_files={"app/api/routes/grooming.py": "x"},
        ai_client=client,
    )
    assert report.clean is False
    assert len(report.suspicious_files) == 2
    assert report.has_blockers is True
    assert len(report.blockers) == 1
    assert report.blockers[0].filepath == "app/api/routes/grooming.py"
    assert report.blockers[0].severity == "blocker"


def test_response_with_markdown_fences_parses_cleanly():
    client = _ai_returning(
        '```json\n{"clean": true, "summary": "ok", "suspicious_files": []}\n```'
    )
    report = review_for_hallucinations(
        problem_statement="any",
        changed_files={"a.py": "x"},
        ai_client=client,
    )
    assert report.clean is True


def test_response_with_surrounding_prose_parses_cleanly():
    client = _ai_returning(
        'Here is my review:\n'
        '{"clean": true, "summary": "fine", "suspicious_files": []}\n'
        'Let me know if you have questions.'
    )
    report = review_for_hallucinations(
        problem_statement="any",
        changed_files={"a.py": "x"},
        ai_client=client,
    )
    assert report.clean is True


def test_unparseable_response_soft_fails_to_clean():
    """We don't want a flaky AI call to fail an otherwise-good
    engineering pass. Reviewer is a defense in depth, not the only
    line of defense."""
    client = _ai_returning("blah blah no JSON here")
    report = review_for_hallucinations(
        problem_statement="any",
        changed_files={"a.py": "x"},
        ai_client=client,
    )
    assert report.clean is True
    assert report.skipped_reason == "no_json_in_response"


def test_ai_call_exception_soft_fails_to_clean():
    client = MagicMock()
    client.get_text.side_effect = RuntimeError("network down")
    report = review_for_hallucinations(
        problem_statement="any",
        changed_files={"a.py": "x"},
        ai_client=client,
    )
    assert report.clean is True
    assert "ai_call_failed" in report.skipped_reason


def test_unknown_severity_normalizes_to_warning():
    """Defensive: if AI emits a severity outside the schema, treat
    it as a warning rather than fail-open or fail-closed."""
    client = _ai_returning(
        '{"clean": false, "suspicious_files": ['
        '  {"filepath": "x.py", "reason": "y", "severity": "critical"}'
        ']}'
    )
    report = review_for_hallucinations(
        problem_statement="any",
        changed_files={"a.py": "x"},
        ai_client=client,
    )
    assert report.suspicious_files[0].severity == "warning"
    assert report.has_blockers is False


def test_collect_changed_files_filters_extensions(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("# python")
    (tmp_path / "app" / "config.toml").write_text("not python")  # filtered out
    (tmp_path / "app" / "page.tsx").write_text("// react")
    (tmp_path / "app" / "ignored.txt").write_text("text")  # filtered out

    files = collect_changed_files(tmp_path)
    assert "app/main.py" in files
    assert "app/page.tsx" in files
    assert "app/config.toml" not in files
    assert "app/ignored.txt" not in files


def test_collect_changed_files_skips_node_modules_and_pycache(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("# real")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("# vendor")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "stale.py").write_text("# stale")

    files = collect_changed_files(tmp_path)
    assert "src/real.py" in files
    assert not any("node_modules" in p for p in files)
    assert not any("__pycache__" in p for p in files)


def test_parse_response_handles_extra_filepath_keys():
    """The AI may emit extra keys we don't model. Drop them, keep
    parsing the ones we do."""
    text = '{"clean": false, "suspicious_files": [{"filepath": "x.py", "reason": "y", "severity": "blocker", "confidence": 0.95}]}'
    report = _parse_review_response(text)
    assert report.suspicious_files[0].filepath == "x.py"
    assert report.suspicious_files[0].severity == "blocker"
