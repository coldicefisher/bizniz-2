"""``coder_tester`` — v4 unified agent that writes code AND tests for
one issue at a time.

Replaces v2's separate Coder + Tester agents (which drifted on
spec interpretation) and v3's CoderAgentV3 (which works per-milestone,
not per-issue). v4 scope: ONE issue per LLM call, structured output,
no tool loop. Designed for parallel dispatch via PIRunner.
"""
from bizniz.coder_tester.agent import (
    CoderTesterAgent,
    CoderTesterError,
)
from bizniz.coder_tester.types import (
    CoderTesterResult,
    FilledFile,
)

__all__ = [
    "CoderTesterAgent",
    "CoderTesterError",
    "CoderTesterResult",
    "FilledFile",
]
