"""Prompt + JSON schema for ``QualityEngineer.review``.

Post-flight mode: the QE re-enters with the EnrichedSpec it produced
preflight + the Engineer's plan + the test files the Engineer wrote.
It produces a CoverageReport: which capabilities have real test
coverage, which are missing, and what scenarios still need tests.

BIAS FIREWALL — important architectural choice. The reviewer NEVER
sees source code. It only sees:
  • the EnrichedSpec (its own preflight output)
  • the Engineer's submitted plan (issues + spec_refs)
  • the test files

This is enforced by the call site (the agent's ``review()`` method
only accepts these three inputs). The prompt repeats it so the LLM
itself doesn't try to ask for source.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, Optional


REVIEW_SYSTEM_PROMPT = """\
You are the QualityEngineer (post-flight review mode). Your job is to
verify that the Engineer's tests cover the EnrichedSpec you wrote
during preflight.

CRITICAL — BIAS FIREWALL

You are reviewing tests, not source code. You will NEVER see the
implementation. This is intentional: a reviewer who sees source code
tends to "verify" whatever the source does instead of demanding what
the spec requires. Reading only tests + spec keeps you honest.

If the tests don't cover a spec capability, that capability is
unverified — full stop. The fact that the implementation might be
correct is irrelevant; if there's no test, there's no proof.

If you find yourself wanting to inspect source, set
``bias_check_passed=false`` and explain in ``summary`` why the test
files don't give you enough to judge coverage. Don't make up coverage.

WHAT TO CHECK (per capability in the spec)

1. Is there at least one test that exercises the happy path?
   • Inputs match what the spec requires
   • Outputs are asserted (not just status code)
   • Auth requirements are satisfied (right role, right user)

2. Are the spec's error_cases covered?
   • Each named error case should have a test that triggers it
   • The expected status code from the spec should be asserted

3. Are the spec's edge_cases covered?
   • Empty inputs, max-length, Unicode, concurrent writes, etc.
   • Not every edge case demands a test — use judgment based on risk.

4. Are the spec's test_scenarios covered?
   • The Engineer should have written tests for every named scenario.
   • Missing test_scenarios are the most concrete coverage gap.

VERDICTS

Per capability, classify coverage as:
  • ``covered``  — happy path + most critical error_cases tested
  • ``partial``  — happy path tested but error/edge cases missing
  • ``missing``  — no test exercises this capability at all

OVERALL APPROVAL

``approved=true`` only if:
  • All capabilities are at least ``partial``
  • No critical error case is uncovered (auth bypass, data corruption,
    silent failure on malformed input)
  • The bias_check passed

Default to ``approved=false`` if any of those fail. The Engineer can
always add tests; they cannot un-ship a security bug.

MISSING SCENARIOS

For each gap, emit a ``MissingScenario`` with:
  • ``capability_id``: must match a spec capability id exactly
  • ``scenario``: a concrete, testable description
  • ``priority``:
      ``critical``    — security, data integrity, money
      ``important``   — common error paths, key edge cases
      ``nice-to-have`` — polish, rare edge cases

Output JSON only, conforming to the CoverageReport schema. No prose.
"""


REVIEW_SCHEMA = {
    "name": "CoverageReport",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "milestone_name",
            "approved",
            "coverage_by_capability",
            "missing_scenarios",
            "recommendations",
            "bias_check_passed",
            "summary",
            "confidence",
        ],
        "properties": {
            "milestone_name": {"type": "string"},
            "approved": {"type": "boolean"},
            "coverage_by_capability": {
                "type": "object",
                "description": "Map of capability_id → coverage verdict.",
                "additionalProperties": {
                    "type": "string",
                    "enum": ["covered", "partial", "missing"],
                },
            },
            "missing_scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["capability_id", "scenario", "priority"],
                    "properties": {
                        "capability_id": {"type": "string"},
                        "scenario": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["critical", "important", "nice-to-have"],
                        },
                    },
                },
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
            },
            "bias_check_passed": {"type": "boolean"},
            "summary": {"type": "string"},
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
    },
}


def build_review_prompt(
    *,
    milestone_name: str,
    enriched_spec_json: str,
    engineer_plan_json: str,
    test_files: Dict[str, str],
    auth_contract: Optional[str] = None,
) -> str:
    """Build the user message for a ``review`` call.

    Inputs are deliberately scoped:
      - ``enriched_spec_json``: the QE's own preflight output
      - ``engineer_plan_json``: the Engineer's submit_plan payload
      - ``test_files``: path → contents (just tests, NEVER source)
      - ``auth_contract``: optional, helps the reviewer sanity-check
        role assertions in tests

    Source files are NOT a parameter on this function. Don't add one.
    The bias firewall is in the call shape.
    """
    parts = [f"# Coverage Review: {milestone_name}\n"]

    parts.append("\n## EnrichedSpec (your preflight output)\n")
    parts.append("```json\n")
    parts.append(enriched_spec_json.strip() + "\n")
    parts.append("```\n")

    parts.append("\n## Engineer's plan (the issues they submitted)\n")
    parts.append("```json\n")
    parts.append(engineer_plan_json.strip() + "\n")
    parts.append("```\n")

    if auth_contract:
        parts.append("\n## Auth contract (for sanity-checking role assertions)\n")
        parts.append(auth_contract.strip() + "\n")

    parts.append("\n## Test files (the ONLY artifact you may read)\n")
    if not test_files:
        parts.append(
            "(no test files were submitted — coverage is necessarily missing)\n"
        )
    else:
        for path, contents in test_files.items():
            parts.append(f"\n### `{path}`\n")
            parts.append("```\n")
            parts.append(contents.rstrip() + "\n")
            parts.append("```\n")

    parts.append(
        "\n## Your task\n"
        "Produce a CoverageReport for this milestone. Output JSON only,\n"
        "conforming to the CoverageReport schema.\n"
        "\n"
        "Remember: you have NOT seen the implementation. If a test passes\n"
        "but doesn't actually verify spec behavior, that capability is\n"
        "still ``partial``, not ``covered``.\n"
    )

    return "".join(parts)
