from verix.environment.python_environment import PythonSandboxExecutionEnvironment
from verix.environment.docker_environment import DockerExecutionEnvironment



if __name__ == "__main__":
    
    # Example usage of the PythonSandboxExecutionEnvironment //////////////////////////////////////
    env: PythonSandboxExecutionEnvironment = PythonSandboxExecutionEnvironment()

    code = """
def add(a, b):
    return a + b
"""
    
    call_spec = {
        "symbol": "add",
        "args": [2, 3],
        "kwargs": {}
    }

    result = env.execute(code, call_spec)

    print("Result:", result.result)
    print("Error:", result.error)
    
    
    # Example usage of the DockerExecutionEnvironment //////////////////////////////////////
    docker_env = DockerExecutionEnvironment(
        image="python:3.9-slim",
        allowed_modules={"math": __import__("math")},
        timeout=10
    )
    
    call_spec = {
        "symbol": "sqrt",
        "args": [16,],
        "kwargs": {}
    }
    
    docker_code = """   
    
import math
def sqrt(x):
    return math.sqrt(x)
"""

    result = docker_env.execute(docker_code, call_spec)
    
    print("Docker Result:", result.result)
    print("Docker Error:", result.error)
    