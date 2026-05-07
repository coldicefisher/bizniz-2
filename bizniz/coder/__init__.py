"""Coder package — v2.5 narrow-context per-issue agent.

Combines v1's Coder + Tester + QuickDebugger into one tool-loop
agent scoped to a single issue. Adds a deterministic
``validate_symbols`` step (AST-walk over imports) between code-write
and test-write, the new-in-2.5 hallucination firewall.

Workflow per issue:
  1. Discover (1-3 view_file / list_directory calls)
  2. Write target_files (write_file)
  3. validate_symbols — REQUIRED. Fix anything flagged.
  4. Write test_files (write_file)
  5. run_tests; on fail, debug quick-pass + 1 retry, then grind
  6. submit_code

Constructed once per Orchestrator. Each ``code_issue()`` call runs
the full loop fresh.
"""
from bizniz.coder.agent import Coder
from bizniz.coder.symbol_validator import (
    SymbolValidationReport,
    UnresolvedSymbol,
    validate_files,
    validate_python_file,
)
from bizniz.coder.types import CoderError, CoderResult, Issue

__all__ = [
    "Coder",
    "CoderResult",
    "CoderError",
    "Issue",
    "SymbolValidationReport",
    "UnresolvedSymbol",
    "validate_files",
    "validate_python_file",
]
