import shutil
from unittest import result
import pytest

from verix.environment.docker_environment import DockerExecutionEnvironment
from verix.environment.types import ExecutionCallSpec
from verix.workspace.temp_workspace import TempWorkspace


def docker_available():
    return shutil.which("docker") is not None


pytestmark = pytest.mark.skipif(
    not docker_available(),
    reason="Docker is not installed"
)


# --------------------------------------------------
# Basic execution
# --------------------------------------------------

def test_execute_simple_function():

    env = DockerExecutionEnvironment()

    code = """
def process(x):
    return x * 2
"""

    call_spec = ExecutionCallSpec(
        symbol="process",
        args=[5],
        kwargs={}
    )

    result = env.execute(code, call_spec)

    assert result.error is None
    assert result.result == 10


# --------------------------------------------------
# Argument passing
# --------------------------------------------------

def test_execute_with_kwargs():

    env = DockerExecutionEnvironment()

    code = """
def process(a, b=1):
    return a + b
"""

    call_spec = ExecutionCallSpec(
        symbol="process",
        args=[3],
        kwargs={"b": 4}
    )

    result = env.execute(code, call_spec)

    assert result.error is None
    assert result.result == 7


# --------------------------------------------------
# Runtime error handling
# --------------------------------------------------

def test_runtime_error():

    env = DockerExecutionEnvironment()

    code = """
def process():
    raise ValueError("boom")
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.error is not None
    assert "boom" in result.error.message
    assert result.error.type == "ValueError"


# --------------------------------------------------
# Workspace mounting
# --------------------------------------------------

def test_workspace_mount():

    env = DockerExecutionEnvironment()

    with TempWorkspace() as ws:

        ws.write_file("data.txt", "hello")

        code = """
def process():
    with open('/workspace/data.txt') as f:
        return f.read()
"""

        call_spec = ExecutionCallSpec(symbol="process")

        result = env.execute(code, call_spec, workspace=ws)

        assert result.error is None
        assert result.result == "hello"


# --------------------------------------------------
# Timeout behavior
# --------------------------------------------------

def test_timeout():

    env = DockerExecutionEnvironment(timeout=1)

    code = """
import time

def process():
    time.sleep(5)
    return 1
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.error is not None
    assert result.error.type == "TimeoutError"


# --------------------------------------------------
# Missing symbol
# --------------------------------------------------

def test_missing_symbol():

    env = DockerExecutionEnvironment()

    code = """
def something_else():
    return 1
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.error is not None