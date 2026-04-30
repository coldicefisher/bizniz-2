"""Regression test for the cost-tracker agent-attribution fix.

When the orchestrator escalates models, it builds fresh clients via
``client_factory(model)`` and assigns them to the coder/tester/
quickdebugger. BaseAIAgent.__init__ tags the original client at
construction with ``_caller_agent``, but the fresh clients never go
through that path, so without the retag step their AI calls show up
as ``agent=unknown`` in the cost-tracker report.

Validates the fix: after a model escalation, every reassigned client
carries the correct ``_caller_agent``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.orchestrator.coding_orchestrator import _retag_client_for_agent


class _FakeAgent:
    """Anything with a class name. The helper reads type(agent).__name__."""


class Coder(_FakeAgent):
    pass


class Tester(_FakeAgent):
    pass


class QuickDebugger(_FakeAgent):
    pass


def test_retag_uses_lowercased_class_name():
    client = MagicMock()
    _retag_client_for_agent(client, Coder())
    assert client._caller_agent == "coder"

    _retag_client_for_agent(client, Tester())
    assert client._caller_agent == "tester"

    _retag_client_for_agent(client, QuickDebugger())
    assert client._caller_agent == "quickdebugger"


def test_retag_silently_ignores_clients_that_reject_attribute():
    """Some client subclasses use __slots__ — set may raise. Best-effort only."""
    class FrozenClient:
        __slots__ = ()
    _retag_client_for_agent(FrozenClient(), Coder())  # no exception
