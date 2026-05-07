"""Coder package — being restored from v1 with v2 conventions.

Currently only the deterministic ``symbol_validator`` is wired up
(the new-in-2.5 piece). The Coder agent itself + types land when
``_deprecated/coder/coder.py`` is moved here and refactored to
absorb the Tester role + the symbol_validator hook.

Workflow (per issue, once Coder is restored):
  1. Plan-and-write code → target_files
  2. Validate symbols + imports deterministically (AST-walk)
  3. Fix unresolved symbols (forced iteration)
  4. Write tests → test_files
  5. Run tests; debug quick-pass + grind with model escalation
  6. Submit
"""
from bizniz.coder.symbol_validator import (
    SymbolValidationReport,
    UnresolvedSymbol,
    validate_files,
    validate_python_file,
)

__all__ = [
    "SymbolValidationReport",
    "UnresolvedSymbol",
    "validate_files",
    "validate_python_file",
]
