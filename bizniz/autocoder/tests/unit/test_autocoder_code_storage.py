import pytest
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.autocoder.autocoder import Autocoder


@pytest.fixture
def real_workspace_autocoder(mock_client, mock_environment, tmp_path):
    ws = BaseWorkspace(root=tmp_path)
    return Autocoder(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
    ), ws, tmp_path


# ---------------------------------------------------------------------------
# _strip_code_block
# ---------------------------------------------------------------------------

def test_strip_code_block_plain_text(autocoder):
    assert autocoder._strip_code_block("print('hello')") == "print('hello')"


def test_strip_code_block_python_fence(autocoder):
    text = "```python\nprint('hello')\n```"
    assert autocoder._strip_code_block(text) == "print('hello')"


def test_strip_code_block_generic_fence(autocoder):
    text = "```\nprint('hello')\n```"
    assert autocoder._strip_code_block(text) == "print('hello')"


# ---------------------------------------------------------------------------
# _save_code_to_file
# ---------------------------------------------------------------------------

def test_save_code_creates_file(real_workspace_autocoder):
    autocoder, ws, tmp_path = real_workspace_autocoder

    autocoder._save_code_to_file(code="print('hello')", filename="output.py")

    saved = (tmp_path / "output.py").read_text()
    assert "print('hello')" in saved


def test_save_code_includes_problem_statement_comment(real_workspace_autocoder):
    autocoder, ws, tmp_path = real_workspace_autocoder

    autocoder._save_code_to_file(
        code="x = 1",
        filename="out.py",
        prompt="Add two numbers together",
    )

    content = (tmp_path / "out.py").read_text()
    assert "Add two numbers together" in content


def test_save_code_backs_up_existing_file(real_workspace_autocoder):
    autocoder, ws, tmp_path = real_workspace_autocoder

    # Pre-populate a cached file so backup logic triggers
    import os
    cache_dir = tmp_path / "cached"
    cache_dir.mkdir()
    cached_file = cache_dir / "out.py"
    cached_file.write_text("old code")

    autocoder._save_code_to_file(code="new code", filename="out.py")

    # Original saved at workspace root
    assert (tmp_path / "out.py").exists()

    # Old cached file should have been renamed with a timestamp prefix
    backups = list(cache_dir.glob("*out.py"))
    assert len(backups) == 1
    assert "old code" in backups[0].read_text()


def test_save_code_sanitizes_cached_filename(real_workspace_autocoder):
    """
    _save_code_to_file sanitizes the filename used for the *cached* backup copy.
    The main file is written to workspace.path(filename) as-is.
    On a second save, the old cached copy gets a sanitized timestamped name.
    """
    autocoder, ws, tmp_path = real_workspace_autocoder

    # Create a pre-existing cached file so the backup rename logic runs
    import os
    cache_dir = tmp_path / "cached"
    cache_dir.mkdir()
    # The cached file is stored using the sanitized name
    cached = cache_dir / "bad_name_.py"
    cached.write_text("original")

    autocoder._save_code_to_file(code="x = 1", filename="bad:name?.py")

    # Main file is written (filesystem allows these chars on Linux)
    saved_files = list(tmp_path.iterdir())
    assert any("bad" in f.name for f in saved_files)

    # The cached backup was renamed with a timestamp prefix (sanitized name)
    backups = list(cache_dir.glob("*bad_name_.py"))
    assert len(backups) == 1
    assert "original" in backups[0].read_text()
