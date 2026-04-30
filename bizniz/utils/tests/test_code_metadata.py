import pytest
from bizniz.utils.code_metadata import (
    build_metadata_block,
    read_code_metadata,
    METADATA_START,
    METADATA_END,
)

# ── build_metadata_block ─────────────────────────────────────────────────────────

def test_build_metadata_block_contains_sentinels():
    block = build_metadata_block({"problem_statement": "Do something."})
    assert METADATA_START in block
    assert METADATA_END in block


def test_build_metadata_block_all_lines_commented():
    block = build_metadata_block({"key": "value"})
    inner_lines = block.split(METADATA_START)[1].split(METADATA_END)[0].strip().splitlines()
    for line in inner_lines:
        assert line.startswith("# "), f"Line not commented: {line!r}"


def test_build_metadata_block_roundtrip():
    meta = {"problem_statement": "Build X", "saved_at": "2026-03-07T00:00:00+00:00"}
    block = build_metadata_block(meta)
    result = read_code_metadata(block)
    assert result["problem_statement"] == "Build X"
    assert result["saved_at"] == "2026-03-07T00:00:00+00:00"


# ── read_code_metadata — new format ─────────────────────────────────────────────

def test_read_new_format_extracts_problem_statement():
    code = (
        build_metadata_block({"problem_statement": "Parse CSV files."})
        + "\n\ndef parse(row): pass\n"
    )
    meta = read_code_metadata(code)
    assert meta["problem_statement"] == "Parse CSV files."


def test_read_new_format_returns_extra_keys():
    code = build_metadata_block({"problem_statement": "X", "agent": "Coder"})
    meta = read_code_metadata(code)
    assert meta["agent"] == "Coder"


def test_read_returns_none_when_no_metadata():
    code = "def add(a, b):\n    return a + b\n"
    meta = read_code_metadata(code)
    assert meta["problem_statement"] is None


# ── read_code_metadata — legacy format ──────────────────────────────────────────

LEGACY_CODE = '''\
"""
Problem Statement:
===========================
# Build a calculator module.
# It should support add and subtract.
============================
"""

def add(a, b):
    return a + b
'''


def test_read_legacy_format_extracts_problem_statement():
    meta = read_code_metadata(LEGACY_CODE)
    assert meta["problem_statement"] is not None
    assert "calculator" in meta["problem_statement"].lower()


def test_read_legacy_format_joins_lines():
    meta = read_code_metadata(LEGACY_CODE)
    # Multi-line problem statement is joined into one string
    assert "add and subtract" in meta["problem_statement"]


# ── Edge cases ───────────────────────────────────────────────────────────────────

def test_read_empty_string():
    meta = read_code_metadata("")
    assert meta["problem_statement"] is None


def test_build_with_unicode():
    meta = {"problem_statement": "Café résumé 日本語"}
    block = build_metadata_block(meta)
    result = read_code_metadata(block)
    assert result["problem_statement"] == "Café résumé 日本語"


def test_new_format_preferred_over_legacy():
    """If both formats are present, the new format wins."""
    new_block = build_metadata_block({"problem_statement": "NEW"})
    legacy = '"""\nProblem Statement:\n===\n# LEGACY\n===\n"""'
    code = new_block + "\n\n" + legacy
    meta = read_code_metadata(code)
    assert meta["problem_statement"] == "NEW"
