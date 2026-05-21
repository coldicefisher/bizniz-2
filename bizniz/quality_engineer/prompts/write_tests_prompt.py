"""Prompt + schema for QualityEngineer.write_tests().

QE writes one-shot test files for every missing scenario it identified
in review(). Tests are the ground truth the debugger will use to converge
the code — NOT the canonical findings, NOT LLM judgment.

SCOPE HIERARCHY (always highest applicable):
  E2E → Integration → Unit

DOCKER RULE: always target the live containerized stack.
"""
from __future__ import annotations

from typing import Dict, List, Optional


WRITE_TESTS_SYSTEM_PROMPT = """\
You are the QualityEngineer (test-writing mode). You just reviewed a
milestone and identified missing scenarios. Your job is to write the
test files that prove those scenarios work.

These tests are the GROUND TRUTH. The agentic debugger will run them
and fix the source code until they pass. Write tests that are correct
by spec — do not soften assertions to match broken code.

═══════════════════════════════════════════════════════════════
SCOPE HIERARCHY — MANDATORY, ALWAYS HIGHEST APPLICABLE SCOPE
═══════════════════════════════════════════════════════════════

TIER 1 — E2E (Playwright .spec.cjs)
  WHEN: any scenario involving UI interaction, form submission,
        navigation, or a full user journey spanning frontend + backend.
  WHERE: frontend/tests/e2e/<feature>.spec.cjs
  HOW:   Playwright against the live compose stack.
         Use the frontend service URL (or PLAYWRIGHT_BASE_URL env var).
         CommonJS only (.spec.cjs with require()) — the frontend sets
         "type":"module" which breaks Node's ESM loader for .cjs files.

TIER 2 — Integration
  WHEN: REST API behaviour, DB writes, auth flows — no UI rendering.
  Backend (Python/FastAPI):
    pytest in backend/tests/integration/test_<feature>.py
    httpx.AsyncClient against backend:8000 (Docker internal) or
    BACKEND_URL env var for host-side runs.
    Mark async tests with @pytest.mark.asyncio.
  Frontend (React/TS):
    jest in frontend/src/**/*.integration.test.tsx
    Test against live API or MSW interceptors.

TIER 3 — Unit
  WHEN: isolated logic that is unreachable by a higher-scope test
        (a pure function, a validator, a data transformer).
  DO NOT write unit tests for scenarios that integration or E2E cover.
  Backend: pytest in backend/tests/unit/test_<feature>.py
  Frontend: jest in frontend/src/**/*.test.tsx

═══════════════════════════════════════════════════════════════
DOCKER RULE
═══════════════════════════════════════════════════════════════
Every test that can run against the live stack MUST do so.
Service hostnames from inside the Docker network:
  backend → backend:<port>
  auth    → auth:9011
  db      → postgres:5432  (or db:<port>)
For host-side runs support BACKEND_URL / FUSIONAUTH_URL / DATABASE_URL
env var overrides so the same test file works both ways.
Never mock a service that is running in Docker.

═══════════════════════════════════════════════════════════════
PLATFORM RULE
═══════════════════════════════════════════════════════════════
Backend scenarios   → pytest (Python)
Frontend scenarios  → jest or Playwright
Full-stack journeys → Playwright ONLY (.spec.cjs)
Never mix frameworks in one file.

═══════════════════════════════════════════════════════════════
QUALITY RULES
═══════════════════════════════════════════════════════════════
- Each test function covers exactly ONE named scenario.
- Name: test_<capability_id>_<scenario_slug> (snake_case).
- Assertions must be specific: check response body fields, DB state,
  status codes — not just "response is not None".
- Auth-required scenarios must send a valid JWT (or show exactly how
  to obtain one from FusionAuth in the existing test pattern).
- Tests must be runnable as-is: no TODOs, no placeholder assertions.
- Best-effort is acceptable — the debugger will fix failures.
  Write the correct assertion even if you think the code might not
  pass it yet.

OUTPUT: one JSON object matching the schema. No prose outside JSON.
"""


WRITE_TESTS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "qe_write_tests_output",
        "schema": {
            "type": "object",
            "properties": {
                "tests": {
                    "type": "array",
                    "description": "Test files to write. Each is one complete file.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative path (e.g. backend/tests/integration/test_auth.py).",
                            },
                            "content": {
                                "type": "string",
                                "description": "Complete file content — written verbatim to disk.",
                            },
                            "scope": {
                                "type": "string",
                                "enum": ["e2e", "integration", "unit"],
                                "description": "Scope tier per the hierarchy.",
                            },
                            "service": {
                                "type": "string",
                                "description": "Which service this test lives in (e.g. 'backend', 'frontend').",
                            },
                            "finding_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Canonical finding IDs this file covers.",
                            },
                        },
                        "required": ["path", "content", "scope", "service", "finding_ids"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tests"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def build_write_tests_prompt(
    *,
    milestone_name: str,
    enriched_spec_json: str,
    missing_scenarios: List[dict],
    architecture_summary: str,
    compose_path: str,
    test_files: Dict[str, str],
    auth_contract: Optional[str] = None,
) -> str:
    parts = [f"# QE Write Tests: {milestone_name}\n"]

    parts.append(
        "You identified the following missing scenarios in your review. "
        "Write test files that prove these scenarios work. "
        "The agentic debugger will fix the source until these tests pass.\n"
    )

    parts.append("\n## Architecture\n")
    parts.append(architecture_summary + "\n")
    parts.append(f"\n**Compose file:** `{compose_path}`\n")

    parts.append("\n## EnrichedSpec\n```json\n")
    parts.append(enriched_spec_json.strip() + "\n```\n")

    parts.append("\n## Missing scenarios to cover\n")
    for ms in missing_scenarios:
        cap = ms.get("capability_id", "?")
        scenario = ms.get("scenario", "?")
        priority = ms.get("priority", "important")
        parts.append(f"- [{priority}] `{cap}`: {scenario}")
    parts.append("")

    if auth_contract:
        parts.append("\n## Auth contract\n")
        parts.append(auth_contract.strip() + "\n")

    parts.append("\n## Existing test files (style + API surface reference)\n")
    if not test_files:
        parts.append("_(no existing test files — infer patterns from the spec and auth contract)_\n")
    else:
        for path, content in list(test_files.items())[:20]:
            if len(content) > 3000:
                content = content[:3000] + "\n# ...[truncated]...\n"
            parts.append(f"\n### `{path}`\n```\n{content.rstrip()}\n```\n")

    parts.append(
        "\n## Your task\n"
        "Write test files for ALL missing scenarios above. "
        "Follow the scope hierarchy (E2E → Integration → Unit). "
        "Always target the Docker stack. "
        "Emit `{tests: [...]}` — no prose outside the JSON.\n"
    )
    return "".join(parts)
