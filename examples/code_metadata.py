"""
Example: Code metadata utilities

Shows how to embed and read structured metadata in Python source files.
Used by the Coder and Tester to track problem statements.
"""
import os
import shutil

from dotenv import load_dotenv

load_dotenv()  # automatically finds .env in current directory or parents



from bizniz.utils.code_metadata import build_metadata_block, read_code_metadata


if __name__ == "__main__":

    # Build a metadata block
    metadata = {
        "problem_statement": "Write a function that checks if a number is prime.",
        "saved_at": "2026-03-07T12:00:00+00:00",
        "agent": "Coder",
    }
    block = build_metadata_block(metadata)

    print("=== Metadata Block ===")
    print(block)

    # Combine with code
    code = block + "\n\n" + "def is_prime(n):\n    if n < 2:\n        return False\n    return all(n % i for i in range(2, int(n**0.5) + 1))\n"

    print("\n=== Full File ===")
    print(code)

    # Read metadata back
    parsed = read_code_metadata(code)
    print("\n=== Parsed Metadata ===")
    for k, v in parsed.items():
        print(f"  {k}: {v}")

    # Legacy format support
    legacy_code = '''"""
Problem Statement:
===========================
# Build a calculator module.
# It should support add and subtract.
============================
"""

def add(a, b):
    return a + b
'''
    legacy_meta = read_code_metadata(legacy_code)
    print(f"\n=== Legacy Format ===")
    print(f"  problem_statement: {legacy_meta['problem_statement']}")

    # No metadata
    plain = "def hello(): return 'world'"
    plain_meta = read_code_metadata(plain)
    print(f"\n=== No Metadata ===")
    print(f"  problem_statement: {plain_meta['problem_statement']}")
