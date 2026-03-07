# verix/workspace/tests/test_temp_workspace.py

from verix.workspace.temp_workspace import TempWorkspace


def test_temp_workspace_creates_directory():
    ws = TempWorkspace()

    assert ws.root.exists()

    ws.cleanup()


def test_temp_workspace_write_file():
    with TempWorkspace() as ws:
        ws.write_file("file.txt", "hello")

        assert ws.exists("file.txt")


def test_temp_workspace_cleanup():
    ws = TempWorkspace()

    root = ws.root

    assert root.exists()

    ws.cleanup()

    assert not root.exists()