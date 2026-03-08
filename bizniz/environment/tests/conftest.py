# bizniz/environment/tests/conftest.py

import pytest

from bizniz.environment.python_environment import (
    PythonSandboxExecutionEnvironment
)

from bizniz.environment.types import ExecutionCallSpec


@pytest.fixture
def env():
    return PythonSandboxExecutionEnvironment()