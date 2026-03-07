# verix/environment/tests/conftest.py

import pytest

from verix.environment.python_environment import (
    PythonSandboxExecutionEnvironment
)

from verix.environment.types import ExecutionCallSpec


@pytest.fixture
def env():
    return PythonSandboxExecutionEnvironment()