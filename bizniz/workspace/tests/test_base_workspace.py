# bizniz/workspace/tests/test_base_workspace.py

import os
from pathlib import Path

import pytest

from bizniz.workspace.base_workspace import BaseWorkspace


def test_workspace_initializes_directory(tmp_path):
    root = tmp_path / "workspace"

    ws = BaseWorkspace(root)

    assert ws.root == root.resolve()
    assert root.exists()
    assert root.is_dir()


def test_write_and_read_file(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("src/main.py", "print('hello')")

    content = ws.read_file("src/main.py")

    assert content == "print('hello')"


def test_write_creates_directories(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("a/b/c/test.txt", "data")

    assert (tmp_path / "a/b/c/test.txt").exists()


def test_delete_file(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("file.txt", "hello")

    ws.delete_file("file.txt")

    assert not (tmp_path / "file.txt").exists()


def test_make_dir(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.make_dir("src/utils")

    assert (tmp_path / "src/utils").exists()
    assert (tmp_path / "src/utils").is_dir()


def test_list_files(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("a.txt", "1")
    ws.write_file("b/c.txt", "2")

    files = ws.list_relative_files()

    assert "a.txt" in files
    assert "b/c.txt" in files
    assert len(files) == 2


def test_exists(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("file.txt", "hello")

    assert ws.exists("file.txt")
    assert not ws.exists("missing.txt")


def test_absolute_path(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("file.txt", "hello")

    path = ws.absolute_path("file.txt")

    assert isinstance(path, Path)
    assert path.exists()
    assert path == (tmp_path / "file.txt").resolve()


def test_tree_lists_files(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.write_file("a.txt", "1")
    ws.write_file("b/c.txt", "2")

    tree = ws.tree()

    assert "a.txt" in tree
    assert "b/c.txt" in tree


def test_git_init(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.init_git()

    assert (tmp_path / ".git").exists()


def test_git_commit(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.init_git()

    ws.write_file("file.txt", "hello")

    ws.git_commit("initial commit")

    result = ws.git_diff()

    # After commit diff should be empty
    assert result == ""


def test_git_diff_detects_changes(tmp_path):
    ws = BaseWorkspace(tmp_path)

    ws.init_git()

    ws.write_file("file.txt", "hello")
    ws.git_commit("initial commit")

    ws.write_file("file.txt", "changed")

    diff = ws.git_diff()

    assert "changed" in diff