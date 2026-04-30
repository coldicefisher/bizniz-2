"""
Functional tests: verify Tester can generate tests via real API calls.

Run with:
    pytest bizniz/tester/tests/functional/ -m functional -v
"""
import pytest

from bizniz.tester.tester import Tester
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.workspace.local_workspace import LocalWorkspace


@pytest.fixture
def workspace_with_code(workspace_path):
    """Create a workspace with a simple source file to test against."""
    ws = LocalWorkspace(root=str(workspace_path))
    ws.write_file(
        "calculator.py",
        'def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n',
    )
    return ws


@pytest.mark.functional
def test_generate_tests_from_prompt(api_key, workspace_path):
    """Tester generates tests from a problem statement."""
    config = BiznizConfig(api_key=api_key)
    client = config.make_client()
    workspace = LocalWorkspace(root=str(workspace_path))
    environment = DockerExecutionEnvironment()

    tester = Tester(client=client, environment=environment, workspace=workspace)

    result = tester.process_from_prompt(
        prompt="Write a calculator module with add and subtract functions.",
        output_path="tests/test_calculator.py",
        code_filename="calculator.py",
    )

    assert result.success
    assert len(result.test_files) > 0

    test_code = result.test_files[0].tests
    assert "def test_" in test_code
    assert workspace.read_file("tests/test_calculator.py") is not None


@pytest.mark.functional
def test_generate_multi_tests(api_key, workspace_with_code):
    """Tester generates tests for multiple source files."""
    config = BiznizConfig(api_key=api_key)
    client = config.make_client()
    workspace = workspace_with_code
    environment = DockerExecutionEnvironment()

    tester = Tester(client=client, environment=environment, workspace=workspace)

    source_code = {"calculator.py": workspace.read_file("calculator.py")}

    result = tester.generate_multi(
        problem_statement="A calculator module with add and subtract.",
        test_files=["tests/test_calculator.py"],
        source_code=source_code,
        architecture_context="Simple calculator.",
    )

    assert result.success
    assert len(result.test_files) > 0

    test_code = result.test_files[0].tests
    assert "def test_" in test_code
