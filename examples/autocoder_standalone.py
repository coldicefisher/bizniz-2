"""
Example: Autocoder standalone usage

Generates Python code from a prompt, executes it in a sandboxed environment,
and iteratively repairs it until it runs error-free.

Requirements:
    - OPENAI_API_KEY environment variable set
"""

import os
import shutil

from dotenv import load_dotenv

load_dotenv()  # automatically finds .env in current directory or parents



from bizniz.autocoder.autocoder import Autocoder
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    # 1. Set up dependencies
    client = ChatGPTClient(
        config=ChatGPTClientConfig(),
        api_key=None,  # reads from OPENAI_API_KEY env var
    )

    environment = PythonSandboxExecutionEnvironment()
    workspace = TempWorkspace()

    # 2. Create the Autocoder
    autocoder = Autocoder(
        client=client,
        environment=environment,
        workspace=workspace,
        max_retries=5,
        on_status_message=lambda msg: print(f"  [status] {msg}"),
    )

    # 3. Generate code from a prompt
    result = autocoder.generate(
        prompt="Write a function called fibonacci(n) that returns the nth Fibonacci number.",
        filename="fibonacci.py",
    )

    print("\n=== Generated Code ===")
    print(result.code)
    print(f"\n=== Output: {result.output} ===")

    # 4. You can also repair existing code
    repair_result = autocoder.repair(
        previous_code="def fibonacci(n):\n    return n * 2  # wrong!",
        error_message="AssertionError: fibonacci(10) should be 55, got 20",
        filename="fibonacci.py",
    )

    print("\n=== Repaired Code ===")
    print(repair_result.code)

    print(f"\nWorkspace files: {workspace.tree()}")
