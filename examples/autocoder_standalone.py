"""
Example: Coder standalone usage

Generates Python code from a prompt using the AI code generation pipeline.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
"""
from dotenv import load_dotenv

load_dotenv()

from bizniz.agents.coder.coder import Coder
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.workspace.temp_workspace import TempWorkspace


if __name__ == "__main__":

    # 1. Set up dependencies via config
    config = BiznizConfig.find_and_load()
    client = config.make_client()
    environment = DockerExecutionEnvironment()
    workspace = TempWorkspace()

    # 2. Create the Coder
    coder = Coder(
        client=client,
        environment=environment,
        workspace=workspace,
        on_status_message=lambda msg: print(f"  [status] {msg}"),
    )

    # ── Single file generation ────────────────────────────────────────
    print("=== Single File Generation ===")
    result = coder.generate_only(
        prompt="Write a Python function called 'add' that takes two numbers and returns their sum.",
        filename="math_utils.py",
    )

    print(f"\nGenerated {len(result.changes)} file(s):")
    for change in result.changes:
        print(f"  {change.filepath} ({change.action})")
        print(change.code)

    # ── Multi-file generation ─────────────────────────────────────────
    print("\n=== Multi-File Generation ===")
    target_files = [
        {"filepath": "calculator/ops.py", "description": "Basic arithmetic operations"},
    ]

    result = coder.generate_multi(
        issue_description="Create a calculator module with add, subtract, multiply, divide functions.",
        target_files=target_files,
        architecture_context="Simple calculator package.",
    )

    print(f"\nGenerated {len(result.changes)} file(s):")
    for change in result.changes:
        print(f"  {change.filepath} ({change.action})")
        print(change.code)

    print(f"\nWorkspace files: {workspace.tree()}")
