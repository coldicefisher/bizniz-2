"""Tests for file_io tool factories."""
import pytest

from bizniz.lib.tools.file_io import (
    build_file_io_handlers,
    make_list_directory,
    make_search_files,
    make_view_file,
    make_write_file,
)
from bizniz.workspace.local_workspace import LocalWorkspace


@pytest.fixture
def ws(tmp_path):
    return LocalWorkspace(root=tmp_path)


class TestViewFile:
    def test_reads_with_line_numbers(self, ws, tmp_path):
        (tmp_path / "x.py").write_text("a = 1\nb = 2\n")
        handler = make_view_file(ws)
        out = handler({"path": "x.py"})
        assert "x.py" in out
        assert "    1  a = 1" in out
        assert "    2  b = 2" in out

    def test_missing_path(self, ws):
        handler = make_view_file(ws)
        out = handler({"path": ""})
        assert "ERROR" in out

    def test_missing_file(self, ws):
        handler = make_view_file(ws)
        out = handler({"path": "nope.py"})
        assert "ERROR" in out

    def test_too_large_file_rejected(self, ws, tmp_path):
        big = "x" * (300 * 1024)
        (tmp_path / "big.py").write_text(big)
        handler = make_view_file(ws)
        out = handler({"path": "big.py"})
        assert "ERROR" in out
        assert "outline" in out.lower()


class TestWriteFile:
    def test_writes_creates_parents(self, ws, tmp_path):
        handler = make_write_file(ws)
        out = handler({"path": "deep/nested/file.py", "new_content": "print('hi')"})
        assert "wrote" in out
        assert (tmp_path / "deep" / "nested" / "file.py").read_text() == "print('hi')"

    def test_overwrites(self, ws, tmp_path):
        (tmp_path / "x.py").write_text("old")
        handler = make_write_file(ws)
        handler({"path": "x.py", "new_content": "new"})
        assert (tmp_path / "x.py").read_text() == "new"

    def test_missing_path(self, ws):
        handler = make_write_file(ws)
        out = handler({"path": "", "new_content": "x"})
        assert "ERROR" in out

    def test_missing_content(self, ws):
        handler = make_write_file(ws)
        out = handler({"path": "x.py"})
        assert "ERROR" in out


class TestListDirectory:
    def test_lists_with_dir_suffix(self, ws, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "subdir").mkdir()
        handler = make_list_directory(ws)
        out = handler({"path": "."})
        assert "subdir/" in out
        assert "a.py" in out

    def test_excludes_noise_dirs(self, ws, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "real.py").write_text("")
        handler = make_list_directory(ws)
        out = handler({"path": "."})
        assert "__pycache__" not in out
        assert "node_modules" not in out
        assert "real.py" in out


class TestSearchFiles:
    def test_finds_pattern(self, ws, tmp_path):
        (tmp_path / "a.py").write_text("def foo(): pass\n")
        (tmp_path / "b.py").write_text("def bar(): pass\n")
        handler = make_search_files(ws)
        out = handler({"path": r"def \w+"})
        assert "a.py" in out
        assert "b.py" in out

    def test_excludes_binary(self, ws, tmp_path):
        (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "code.py").write_text("PNG\n")
        handler = make_search_files(ws)
        out = handler({"path": "PNG"})
        assert "img.png" not in out
        assert "code.py" in out

    def test_no_matches(self, ws):
        handler = make_search_files(ws)
        out = handler({"path": "nothing_matches"})
        assert "no matches" in out.lower()

    def test_invalid_regex(self, ws):
        handler = make_search_files(ws)
        out = handler({"path": "[unclosed"})
        assert "ERROR" in out


class TestBuilder:
    def test_build_returns_full_dict(self, ws):
        handlers = build_file_io_handlers(ws)
        assert set(handlers.keys()) == {
            "view_file", "write_file", "list_directory", "search_files",
        }
