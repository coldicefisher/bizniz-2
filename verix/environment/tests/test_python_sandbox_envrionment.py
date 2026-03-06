from verix.environment.python_environment import PythonSandboxExecutionEnvironment

from verix.environment.types import (
    ExecutionCallSpec, 
    ExecutionEnvironmentResult, 
    ExecutionEnvironmentErrorDetails
)

from verix.environment.base_environment import BaseExecutionEnvironment

def test_execute_simple_function(env: BaseExecutionEnvironment):

    code = """
def process(x):
    return x + 1
"""

    call_spec = ExecutionCallSpec(
        symbol="process",
        args=(5,)
    )

    result: ExecutionEnvironmentResult = env.execute(code, call_spec)

    assert result.success is True
    assert result.result == 6
    
    
def test_symbol_not_found(env: BaseExecutionEnvironment):

    code = """
def something_else():
    return 1
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result: ExecutionEnvironmentResult = env.execute(code, call_spec)
    assert result.success is False
    assert result.error.type == "EntrypointNotFound"
    
    
def test_symbol_not_callable(env: BaseExecutionEnvironment):

    code = """
process = 123
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.success is False
    assert result.error.type == "SymbolNotCallable"





def test_compile_error(env: BaseExecutionEnvironment):

    code = """
def process(x)
    return x
"""

    call_spec = ExecutionCallSpec(symbol="process", args=(1,))

    result = env.execute(code, call_spec)

    assert result.success is False
    assert result.error.stage == "compile"
    
    
    
def test_invalid_arguments(env: BaseExecutionEnvironment):

    code = """
def process(x, y):
    return x + y
"""

    call_spec = ExecutionCallSpec(
        symbol="process",
        args=(1,)
    )

    result = env.execute(code, call_spec)

    assert result.success is False
    assert result.error.type == "InvalidArguments"
    
    
    
    
def test_runtime_error(env: BaseExecutionEnvironment):

    code = """
def process(x):
    return 1 / 0
"""

    call_spec = ExecutionCallSpec(
        symbol="process",
        args=(5,)
    )

    result = env.execute(code, call_spec)

    assert result.success is False
    assert result.error.stage == "runtime"
    assert result.error.type == "ZeroDivisionError"
    
    
    
    
def test_stdout_capture(env: BaseExecutionEnvironment):

    code = """
def process():
    print("hello world")
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result: ExecutionEnvironmentResult = env.execute(code, call_spec)

    assert result.success is True
    assert "hello world" in result.stdout
    
    
    
def test_security_exec_blocked(env: BaseExecutionEnvironment):

    code = """
def process():
    exec("print(1)")
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.success is False
    assert result.error.stage == "security"
    
    
    
def test_import_blocked(env: BaseExecutionEnvironment):

    code = """
import os

def process():
    return os.getcwd()
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.success is False
    
    
    
def test_trace_events_exist(env: BaseExecutionEnvironment):

    code = """
def process():
    return 42
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.success is True
    assert len(result.traces) > 0
    
    

def test_symbol_resolution_nested(env: BaseExecutionEnvironment):

    code = """
class A:
    class B:
        def run(self):
            return 99

b = A.B()
"""

    call_spec = ExecutionCallSpec(symbol="b.run")

    result = env.execute(code, call_spec)

    assert result.success is True
    assert result.result == 99
    
    
    
    
    
def test_allowed_import():

    env = PythonSandboxExecutionEnvironment(
        allowed_modules={"hashlib": __import__("hashlib")}
    )

    code = """
import hashlib

def process():
    return hashlib.sha256(b"test").hexdigest()
"""

    call_spec = ExecutionCallSpec(symbol="process")

    result = env.execute(code, call_spec)

    assert result.success is True
    
    
    
    
