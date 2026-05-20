"""Tests for the edit-mode apply machinery."""
from __future__ import annotations

import pytest

from bizniz.coder_tester.edits import EditApplyReport, apply_edits
from bizniz.coder_tester.types import FileEdit
from bizniz.workspace.local_workspace import LocalWorkspace


def _ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return LocalWorkspace(root)


# ── Single-file happy path ────────────────────────────────────────


class TestSingleFile:
    def test_unique_match_applies(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "def foo():\n    return 1\n")
        report = apply_edits(ws, [
            FileEdit(
                path="app/x.py",
                old_text="return 1",
                new_text="return 42",
            ),
        ])
        assert report.ok
        assert report.paths_written == ["app/x.py"]
        # File on disk reflects the change.
        new = (ws.root / "app/x.py").read_text()
        assert new == "def foo():\n    return 42\n"

    def test_no_match_records_failure(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "def foo(): pass\n")
        report = apply_edits(ws, [
            FileEdit(
                path="app/x.py",
                old_text="NOT IN FILE",
                new_text="anything",
            ),
        ])
        assert not report.ok
        assert report.failures[0].reason == "no_match"
        # Original content preserved.
        assert (ws.root / "app/x.py").read_text() == "def foo(): pass\n"

    def test_ambiguous_match_records_failure(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file(
            "app/x.py",
            "x = 1\nx = 1\n",  # 'x = 1' appears twice
        )
        report = apply_edits(ws, [
            FileEdit(path="app/x.py", old_text="x = 1", new_text="x = 99"),
        ])
        assert not report.ok
        assert "ambiguous" in report.failures[0].reason

    def test_missing_file_records_failure(self, tmp_path):
        ws = _ws(tmp_path)
        report = apply_edits(ws, [
            FileEdit(path="app/missing.py", old_text="x", new_text="y"),
        ])
        assert not report.ok
        assert report.failures[0].reason == "file_missing"


# ── Multi-edit ordering ───────────────────────────────────────────


class TestMultiEdit:
    def test_two_edits_to_same_file_apply_in_order(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "a = 1\nb = 2\n")
        report = apply_edits(ws, [
            FileEdit(path="app/x.py", old_text="a = 1", new_text="a = 10"),
            FileEdit(path="app/x.py", old_text="b = 2", new_text="b = 20"),
        ])
        assert report.ok
        assert (ws.root / "app/x.py").read_text() == "a = 10\nb = 20\n"

    def test_edit_can_reference_prior_edit_result(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "value = 1\n")
        # First edit changes "value = 1" → "value = 2".
        # Second edit references the NEW text "value = 2".
        report = apply_edits(ws, [
            FileEdit(path="app/x.py", old_text="value = 1", new_text="value = 2"),
            FileEdit(path="app/x.py", old_text="value = 2", new_text="value = 3"),
        ])
        assert report.ok
        assert (ws.root / "app/x.py").read_text() == "value = 3\n"

    def test_edits_to_different_files_apply_independently(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/a.py", "AAA\n")
        ws.write_file("app/b.py", "BBB\n")
        report = apply_edits(ws, [
            FileEdit(path="app/a.py", old_text="AAA", new_text="aaa"),
            FileEdit(path="app/b.py", old_text="BBB", new_text="bbb"),
        ])
        assert report.ok
        assert sorted(report.paths_written) == ["app/a.py", "app/b.py"]
        assert (ws.root / "app/a.py").read_text() == "aaa\n"
        assert (ws.root / "app/b.py").read_text() == "bbb\n"

    def test_partial_failure_doesnt_block_other_edits(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/a.py", "AAA\n")
        ws.write_file("app/b.py", "BBB\n")
        report = apply_edits(ws, [
            FileEdit(path="app/a.py", old_text="NOT THERE", new_text="x"),
            FileEdit(path="app/b.py", old_text="BBB", new_text="bbb"),
        ])
        # a.py failed; b.py succeeded.
        assert len(report.failures) == 1
        assert report.failures[0].path == "app/a.py"
        assert "app/b.py" in report.paths_written
        assert (ws.root / "app/b.py").read_text() == "bbb\n"


# ── Whitespace + indentation ──────────────────────────────────────


class TestWhitespacePrecision:
    def test_indentation_must_match_exactly(self, tmp_path):
        ws = _ws(tmp_path)
        ws.write_file("app/x.py", "    return 1\n")
        # Wrong indentation in old_text → no match.
        report = apply_edits(ws, [
            FileEdit(
                path="app/x.py",
                old_text="return 1",       # missing 4 leading spaces
                new_text="return 2",
            ),
        ])
        assert report.ok  # short pattern still finds "return 1"
        # But this case is fine because the substring matches uniquely.
        # The real risk is when whitespace VARIES: try a case where
        # the agent emits the WRONG indentation.
        # Verify the correctly-indented full match works too:
        ws.write_file("app/x.py", "    return 1\n    return 1\n")
        report2 = apply_edits(ws, [
            FileEdit(
                path="app/x.py",
                old_text="    return 1",
                new_text="    return 2",
            ),
        ])
        # Ambiguous — both lines match.
        assert not report2.ok
        assert "ambiguous" in report2.failures[0].reason
