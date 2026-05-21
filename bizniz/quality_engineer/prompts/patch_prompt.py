"""Prompt + JSON schema for ``QualityEngineer.patch``.

QE-hybrid mode: after review(), QE is given the list of missing
scenarios it just identified and asked to emit test file patches that
cover them.  The patches are validated (written + test-run) before
the CanonicalReport is frozen.  Any finding whose capability is covered
by a passing patch is marked auto-resolved and never enters the repair
queue.

BIAS FIREWALL PRESERVED — patch() still only sees test files + spec,
never source.  The LLM pattern-matches test style from the existing
tests it already read during review().

SCOPE CONSTRAINT — only emit patches for scenarios you can write
correctly from the visible test patterns + capability spec.  If a
scenario requires knowledge of internal function signatures not visible
in the existing tests, skip it (return no patch for that capability).
A missing patch is always safer than a wrong one.
"""
from __future__ import annotations

from typing import Dict, List, Optional


PATCH_SYSTEM_PROMPT = """\
You are the QualityEngineer (patch mode). You just finished reviewing a
milestone and identified missing test scenarios. Your job now is to write
the test files that cover those gaps.

BIAS FIREWALL — still in effect

You may only use:
  - The EnrichedSpec (your own preflight output)
  - The list of missing scenarios from your review
  - The existing test files (for style + API surface patterns)

You may NOT invent internal function signatures or import paths you
haven't seen in the existing tests.  If you need a symbol not visible in
the existing tests, skip that patch.

WHAT TO WRITE

For each missing scenario you can cover:
  1. Pick the right test file to add it to (prefer appending to an
     existing test file over creating a new one, unless the scenario
     belongs in a clearly separate module).
  2. Write the full file content — existing tests unchanged, new test
     functions appended.  Do not truncate or summarize existing content.
  3. Follow the exact same import style, fixture names, client setup,
     and assertion patterns as the existing tests.
  4. One test function per scenario.  Name it
     ``test_<capability_id>_<scenario_slug>``.
  5. For integration tests (HTTP endpoints): use the same httpx/pytest
     fixture pattern you see in the existing tests.  Do NOT mock the
     stack unless the existing tests do.
  6. For unit tests: only if the existing tests are unit tests for the
     same module.

SKIP CONDITIONS — do NOT emit a patch if:

  - The scenario requires internal symbols not visible in existing tests
  - The existing tests use a testing stack you don't recognize
  - You would have to mock internal implementation details
  - The capability is ``auth_required`` but you can't see how auth
    tokens are produced in the existing tests

A skipped scenario is fine — it goes to the repair queue instead.
An incorrect patch wastes a repair iteration; a skipped one doesn't.

OUTPUT FORMAT

One JSON object: ``{patches: [{path, content, capability_ids,
scenario_descriptions}]}``.  Each entry is one complete file.
Multiple scenarios for the same file → one entry.  No prose.
"""


PATCH_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "qe_patch_output",
        "schema": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "description": (
                        "Test file patches. Each entry is one complete file. "
                        "Empty array if no safe patches can be written."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative path of the test file.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Complete file content — not a diff, not a snippet.",
                            },
                            "capability_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "EnrichedSpec capability IDs this file covers.",
                            },
                            "scenario_descriptions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Human-readable list of scenarios covered.",
                            },
                        },
                        "required": ["path", "content", "capability_ids", "scenario_descriptions"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["patches"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def build_patch_prompt(
    *,
    milestone_name: str,
    enriched_spec_json: str,
    missing_scenarios: List[dict],
    test_files: Dict[str, str],
    auth_contract: Optional[str] = None,
) -> str:
    parts = [f"# QE Patch Mode: {milestone_name}\n"]

    parts.append(
        "You just reviewed this milestone and identified missing scenarios. "
        "Your job: write test file patches that cover the gaps you can safely address.\n"
    )

    parts.append("\n## EnrichedSpec (your preflight output)\n")
    parts.append("```json\n")
    parts.append(enriched_spec_json.strip() + "\n")
    parts.append("```\n")

    parts.append("\n## Missing scenarios (gaps you identified in review)\n")
    for ms in missing_scenarios:
        cap = ms.get("capability_id", "?")
        scenario = ms.get("scenario", "?")
        priority = ms.get("priority", "important")
        parts.append(f"- [{priority}] `{cap}`: {scenario}")
    parts.append("")

    if auth_contract:
        parts.append("\n## Auth contract\n")
        parts.append(auth_contract.strip() + "\n")

    parts.append("\n## Existing test files (style reference + API surface)\n")
    if not test_files:
        parts.append("(no existing test files — skip all patches; unsafe to write blind)\n")
    else:
        for path, content in test_files.items():
            # Cap each file at 3000 chars — enough to see the pattern.
            if len(content) > 3000:
                content = content[:3000] + "\n# ...[truncated]...\n"
            parts.append(f"\n### `{path}`\n")
            parts.append("```\n")
            parts.append(content.rstrip() + "\n")
            parts.append("```\n")

    parts.append(
        "\n## Your task\n"
        "Emit ``{patches: [...]}``. For each missing scenario you can safely "
        "cover (see SKIP CONDITIONS in the system prompt), write the complete "
        "test file. Empty array is a valid answer if nothing is safe to patch.\n"
    )

    return "".join(parts)
