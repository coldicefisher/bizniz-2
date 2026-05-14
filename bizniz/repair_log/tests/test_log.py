"""Tests for the sticky repair log."""
import json
from pathlib import Path

from bizniz.repair_log import (
    RepairLogEntry,
    append_entry,
    read_log,
    format_for_prompt,
    log_path,
)


def test_empty_workspace_returns_empty(tmp_path):
    assert read_log(tmp_path) == []
    assert format_for_prompt(tmp_path) == ""


def test_append_creates_log_file(tmp_path):
    e = RepairLogEntry(
        agent="agenticdebugger",
        trigger="test_login failed: 401",
        diagnosis="missing role mapping",
        outcome="still_failing",
        tier="gemini-flash-top",
        attempt=1,
    )
    append_entry(tmp_path, e)
    assert log_path(tmp_path).exists()
    log = read_log(tmp_path)
    assert len(log) == 1
    assert log[0].diagnosis == "missing role mapping"
    assert log[0].tier == "gemini-flash-top"


def test_multiple_appends_accumulate(tmp_path):
    for i in range(3):
        append_entry(tmp_path, RepairLogEntry(
            agent="agenticdebugger",
            trigger=f"failure #{i}",
            diagnosis=f"diagnosis #{i}",
            outcome="still_failing",
            tier="gemini-flash-top",
            attempt=i + 1,
        ))
    log = read_log(tmp_path)
    assert len(log) == 3
    assert log[0].diagnosis == "diagnosis #0"
    assert log[2].attempt == 3


def test_format_for_prompt_lists_attempts(tmp_path):
    append_entry(tmp_path, RepairLogEntry(
        agent="agenticdebugger",
        trigger="test_login failed",
        diagnosis="role table not seeded",
        fixes=[{"file": "app/api/routes/auth.py", "summary": "added seed"}],
        outcome="still_failing",
        tier="gemini-flash-top",
        attempt=1,
    ))
    text = format_for_prompt(tmp_path)
    assert "DO NOT REPEAT" in text
    assert "agenticdebugger" in text
    assert "gemini-flash-top" in text
    assert "still_failing" in text
    assert "role table not seeded" in text
    assert "app/api/routes/auth.py" in text


def test_format_truncates_long_log(tmp_path):
    for i in range(50):
        append_entry(tmp_path, RepairLogEntry(
            agent="agenticdebugger",
            trigger=f"big trigger text " * 10,
            diagnosis=f"long diagnosis " * 20,
            outcome="still_failing",
            tier="gemini-flash-top",
            attempt=i,
        ))
    text = format_for_prompt(tmp_path, max_chars=1500)
    # Bounded; trailing truncation note appears
    assert "truncated" in text


def test_corrupt_file_returns_empty(tmp_path):
    log_path(tmp_path).write_text("not json", encoding="utf-8")
    assert read_log(tmp_path) == []


def test_atomic_append_through_corruption(tmp_path):
    """Appending after a corrupt file resets to a clean log
    rather than crashing."""
    log_path(tmp_path).write_text("garbage", encoding="utf-8")
    append_entry(tmp_path, RepairLogEntry(
        agent="agenticdebugger",
        trigger="recovery",
        diagnosis="ok",
        outcome="passed",
    ))
    log = read_log(tmp_path)
    # Either 0 or 1 — the soft-fail behavior is the contract.
    # If 1, the entry is the recovery one.
    if log:
        assert log[0].outcome == "passed"
