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


# ── BaseAIAgent._ai_client property — the primary fix ───────────────────────


def test_ai_client_property_retags_on_every_access():
    """Three agents sharing one client all see correct tags via the property,
    even though the underlying client object is the same. This is the
    shared-client design that broke per-call attribution.

    Without this fix, the second pet-groomer run (job 2df8f718) showed
    29 calls labeled ``quickdebugger`` because the coder/tester were
    going through ``self._client.get_text(...)`` and the shared client's
    tag had been last-written by the QuickDebugger constructor. The
    property re-tags right before each call so the cost-tracker
    record-time read gets the right value.
    """
    from bizniz.base_ai_agent import BaseAIAgent

    shared = MagicMock()

    class _Stub(BaseAIAgent):
        @property
        def _process_system_prompt(self) -> str:
            return ""

    class CoderStub(_Stub):
        pass

    class TesterStub(_Stub):
        pass

    class QuickDebuggerStub(_Stub):
        pass

    # Skip the real BaseAIAgent constructor — it requires environment
    # and workspace machinery that the test doesn't need.
    coder = CoderStub.__new__(CoderStub)
    tester = TesterStub.__new__(TesterStub)
    qd = QuickDebuggerStub.__new__(QuickDebuggerStub)
    for inst in (coder, tester, qd):
        inst._client = shared

    # Alternating-access pattern matching what an orchestrator does.
    _ = coder._ai_client
    assert shared._caller_agent == "coderstub"
    _ = tester._ai_client
    assert shared._caller_agent == "testerstub"
    _ = qd._ai_client
    assert shared._caller_agent == "quickdebuggerstub"
    _ = coder._ai_client
    assert shared._caller_agent == "coderstub"  # flips back, not stuck
