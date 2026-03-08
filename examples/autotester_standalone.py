"""
Example: Autotester standalone usage

Three modes for generating pytest test suites:
  1. From existing code (reads metadata for problem statement)
  2. From a prompt only (contract tests before code exists)
  3. Review & strengthen existing tests

Requirements:
    - OPENAI_API_KEY environment variable set
"""

from bizniz.autotester.autotester import Autotester
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient, ChatGPTClientConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace
from bizniz.utils.code_metadata import build_metadata_block


if __name__ == "__main__":

    client = ChatGPTClient(
        config=ChatGPTClientConfig(),
        api_key=None,
    )
    environment = PythonSandboxExecutionEnvironment()
    workspace = TempWorkspace()

    autotester = Autotester(
        client=client,
        environment=environment,
        workspace=workspace,
        on_status_message=lambda msg: print(f"  [status] {msg}"),
    )

    # ── Mode 2: Generate tests from a prompt (no code yet) ──────────────
    print("=== Mode 2: Tests from prompt ===")
    result = autotester.process_from_prompt(
        prompt="A function called is_palindrome(s) that returns True if the string is a palindrome.",
        output_path="test_palindrome.py",
    )
    print(result.tests)

    # ── Mode 1: Generate tests from existing code ───────────────────────
    print("\n=== Mode 1: Tests from code ===")

    # First, write a code file with embedded metadata
    code_with_meta = (
        build_metadata_block({"problem_statement": "Check if a string is a palindrome."})
        + "\n\n"
        + "def is_palindrome(s: str) -> bool:\n"
        + "    return s == s[::-1]\n"
    )
    workspace.write_file("palindrome.py", code_with_meta)

    result = autotester.process_from_code(
        code_path="palindrome.py",
        output_path="test_palindrome_from_code.py",
    )
    print(result.tests)

    # ── Mode 3: Review and strengthen existing tests ────────────────────
    print("\n=== Mode 3: Review tests ===")

    workspace.write_file("test_basic.py", "def test_basic():\n    assert is_palindrome('aba') is True\n")

    result = autotester.review_tests(
        code_path="palindrome.py",
        test_path="test_basic.py",
        output_path="test_palindrome_strengthened.py",
    )
    print(result.tests)

    print(f"\nWorkspace files: {workspace.tree()}")
