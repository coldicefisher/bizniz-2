"""Post-engineer validators.

After an engineer finishes a service, run the language's type-checker
across the whole workspace. This catches cross-file consistency
errors that per-issue unit tests can't see — exactly the
LoginPage / authStore class of bug where each file compiles in
isolation but they don't fit together.

The validator profile (Phase 5) defines:
- ``validator``: command to run (e.g. ``["npx", "tsc", "--noEmit"]``)
- ``validator_runner``: which sidecar/runtime executes it

Validators return a ``ValidationReport`` with a pass/fail flag and
the captured stdout/stderr so callers (the architect) can decide
whether to mark the service as failed and stop the milestone, or
just log and continue.

We deliberately don't auto-regenerate on failure in the first
prototype — surface errors loudly first, then layer regeneration
once we've validated the catch rate.
"""
from __future__ import annotations

from bizniz.validators.runner import (
    ValidationReport,
    ValidatorError,
    run_validator,
)

__all__ = ["ValidationReport", "ValidatorError", "run_validator"]
