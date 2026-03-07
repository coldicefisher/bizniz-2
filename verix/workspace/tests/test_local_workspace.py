# verix/workspace/tests/test_local_workspace.py

import pytest
from pathlib import Path
from verix.workspace.local_workspace import LocalWorkspace




def test_local_workspace_creates_directory(tmp_path):
    root: Path = tmp_path / "workspace"

    ws = LocalWorkspace(root)

    assert root.exists()
    assert ws.root == root.resolve()
    
    
def test_local_workspace_requires_existing_when_create_false(tmp_path):
    root = tmp_path / "workspace"

    with pytest.raises(FileNotFoundError):
        LocalWorkspace(root, create=False)