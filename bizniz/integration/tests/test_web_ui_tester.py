"""Unit tests for WebUITester.

Mocks the AI client at the BaseAIAgent boundary so we don't burn
tokens. Locks in:
  - prompt construction includes problem statement + service + slimmed contracts
  - return value is the AI's text passed through code-block stripping
  - missing contracts dict defaults to empty (no crash)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.integration.web_ui_tester import WebUITester


def _make_tester(ai_response: str = "import { test } from '@playwright/test';\n") -> WebUITester:
    """Build a WebUITester with all dependencies mocked. Skips
    BaseAIAgent.__init__ so we don't need real environment/workspace."""
    inst = WebUITester.__new__(WebUITester)
    inst._client = MagicMock()
    inst._client.get_text.return_value = (ai_response, "job-id", [])
    inst._message_history = []
    inst._system_prompt_override = None
    inst._max_message_history_length = 40
    return inst


def _frontend_svc() -> ServiceDefinition:
    return ServiceDefinition(
        name="frontend", service_type="frontend", framework="react",
        language="typescript", description="React + Vite UI",
        workspace_name="frontend", port=5173,
    )


def test_generate_test_file_returns_stripped_source():
    """AI returns code; tester returns it (after strip_code_block)."""
    tester = _make_tester(ai_response="import { test } from '@playwright/test';\n")
    out = tester.generate_test_file(
        problem_statement="Users browse services",
        service=_frontend_svc(),
    )
    assert "import { test }" in out
    assert tester._client.get_text.called


def test_generate_test_file_strips_markdown_fences():
    fenced = "```typescript\nimport { test } from '@playwright/test';\n```"
    tester = _make_tester(ai_response=fenced)
    out = tester.generate_test_file(
        problem_statement="x",
        service=_frontend_svc(),
    )
    assert "```" not in out
    assert "import { test }" in out


def test_prompt_includes_problem_statement_and_service_details():
    tester = _make_tester()
    tester.generate_test_file(
        problem_statement="Users book appointments",
        service=_frontend_svc(),
    )
    # Inspect what was sent to the AI
    call_kwargs = tester._client.get_text.call_args.kwargs
    sent_messages = call_kwargs["messages"]
    user_content = next(m["content"] for m in sent_messages if m.get("role") == "user")

    assert "Users book appointments" in user_content
    assert "frontend" in user_content
    assert "react" in user_content
    assert "5173" in user_content


def test_prompt_includes_slimmed_backend_contracts():
    """When contracts are passed, only path lists go in (not full schemas)."""
    tester = _make_tester()
    full_doc = {
        "openapi": "3.0.0",
        "paths": {
            "/api/v1/services": {"get": {"summary": "list"}},
            "/api/v1/appointments": {"post": {"requestBody": {"big": "schema"}}},
        },
        "components": {"schemas": {"Heavy": {"type": "object"}}},
    }
    tester.generate_test_file(
        problem_statement="x",
        service=_frontend_svc(),
        backend_contracts={"backend": full_doc},
    )
    user_content = next(
        m["content"] for m in tester._client.get_text.call_args.kwargs["messages"]
        if m.get("role") == "user"
    )

    # Slim — paths appear, full schemas don't
    assert "/api/v1/services" in user_content
    assert "/api/v1/appointments" in user_content
    assert "Heavy" not in user_content  # component schemas filtered out
    assert "big" not in user_content  # requestBody bodies filtered


def test_no_contracts_does_not_crash():
    tester = _make_tester()
    out = tester.generate_test_file(
        problem_statement="x",
        service=_frontend_svc(),
    )
    assert out  # produced something


def test_empty_ai_response_returns_empty_string():
    """Tester is best-effort: empty AI response → empty string,
    don't throw."""
    tester = _make_tester(ai_response="")
    out = tester.generate_test_file(
        problem_statement="x",
        service=_frontend_svc(),
    )
    assert out == ""


def test_system_prompt_present():
    """Sanity: the agent has the WebUITester system prompt as its
    _process_system_prompt — distinct from HTTPApiTester."""
    tester = _make_tester()
    sp = tester._process_system_prompt
    assert "Playwright" in sp
    assert "console.error" in sp or "console errors" in sp.lower()
    assert "is not defined" in sp  # the V9-style failure mode is named explicitly
