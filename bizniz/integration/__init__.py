"""Integration testing for generated stacks.

Replaces ``bizniz.architect.smoke_verification`` with a richer
contract: AI agents (HTTPApiTester, WebUITester later) author actual
integration test files against the live running stack and execute
them. Files persist in the project workspace so the customer keeps a
working test suite for ongoing iteration after the bizniz handoff.

Public surface:
- ``capture_backend_contracts(architecture, project_root, compose_path)``
  spins up backends just long enough to grab ``/openapi.json``, writes
  each to ``contracts/<svc>.openapi.json``, returns the dict.
- ``run_integration_phase(...)`` — the architect's verify-phase entry
  point: capture contracts, dispatch HTTPApiTester per backend, run
  tests, return updated service_results.
"""
from bizniz.integration.contracts import capture_backend_contracts
from bizniz.integration.runner import run_integration_phase

__all__ = ["capture_backend_contracts", "run_integration_phase"]
