"""
Example: Tester standalone usage

Generates pytest test suites from prompts or existing code.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
"""
from dotenv import load_dotenv

load_dotenv()

from bizniz.tester.tester import Tester
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    config = BiznizConfig.find_and_load()
    client = config.make_client()
    environment = DockerExecutionEnvironment()
    workspace = TempWorkspace()

    tester = Tester(
        client=client,
        environment=environment,
        workspace=workspace,
        on_status_message=lambda msg: print(f"  [status] {msg}"),
    )

    # ── Generate tests from a prompt ──────────────────────────────────
    print("=== Tests from Prompt ===")
    result = tester.process_from_prompt(
        prompt="Write a calculator module with add and subtract functions.",
        output_path="tests/test_calculator.py",
        code_filename="calculator.py",
    )
    print(result.test_files[0].tests)

    # ── Generate multi-file tests ─────────────────────────────────────
    print("\n=== Multi-File Tests ===")

    # First write some source code to test against
    workspace.write_file(
        "calculator.py",
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n",
    )

    source_code = {"calculator.py": workspace.read_file("calculator.py")}

    result = tester.generate_multi(
        problem_statement="A calculator module with add and subtract.",
        test_files=["tests/test_calculator.py"],
        source_code=source_code,
        architecture_context="Simple calculator.",
    )

    print(f"Generated {len(result.test_files)} test file(s):")
    for tf in result.test_files:
        print(f"  {tf.filepath}")
        print(tf.tests)

    print(f"\nWorkspace files: {workspace.tree()}")
