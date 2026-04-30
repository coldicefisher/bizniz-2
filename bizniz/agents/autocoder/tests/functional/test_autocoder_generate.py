"""
Functional tests: verify Autocoder can generate code via real API calls.

Run with:
    pytest bizniz/autocoder/tests/functional/ -m functional -v
"""
import pytest

from bizniz.agents.autocoder.autocoder import Autocoder
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.workspace.local_workspace import LocalWorkspace


@pytest.mark.functional
def test_generate_single_file(api_key, workspace_path):
    """Autocoder generates a single Python file from a prompt."""
    config = BiznizConfig(api_key=api_key)
    client = config.make_client()
    workspace = LocalWorkspace(root=str(workspace_path))
    environment = DockerExecutionEnvironment()

    autocoder = Autocoder(client=client, environment=environment, workspace=workspace)

    result = autocoder.generate_only(
        prompt="Write a Python function called 'add' that takes two numbers and returns their sum.",
        filename="math_utils.py",
    )

    assert len(result.changes) > 0
    code = result.changes[0].code
    assert "def add" in code
    assert workspace.read_file("math_utils.py") is not None


@pytest.mark.functional
def test_generate_multi_file(api_key, workspace_path):
    """Autocoder generates multiple files from an issue description."""
    config = BiznizConfig(api_key=api_key)
    client = config.make_client()
    workspace = LocalWorkspace(root=str(workspace_path))
    environment = DockerExecutionEnvironment()

    autocoder = Autocoder(client=client, environment=environment, workspace=workspace)

    target_files = [
        {"filepath": "calculator/ops.py", "description": "Basic arithmetic operations"},
    ]

    result = autocoder.generate_multi(
        issue_description="Create a calculator module with add, subtract, multiply, divide functions.",
        target_files=target_files,
        architecture_context="Simple calculator package.",
    )

    assert len(result.changes) > 0
    found_ops = any("ops.py" in ch.filepath for ch in result.changes)
    assert found_ops, f"Expected ops.py in changes, got: {[ch.filepath for ch in result.changes]}"
