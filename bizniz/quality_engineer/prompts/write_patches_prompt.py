"""Prompt + schema for QualityEngineer.write_patches().

QE writes best-effort source patches to address the findings it
identified. The bias firewall is RELAXED here — QE sees source files
so it can write targeted fixes.

Best-effort only. The agentic debugger handles convergence.
"""
from __future__ import annotations

from typing import Dict, List, Optional


WRITE_PATCHES_SYSTEM_PROMPT = """\
You are the QualityEngineer (patch-writing mode). You have identified
missing scenarios and just wrote tests for them. Now write source code
patches to address the underlying gaps.

BIAS FIREWALL RELAXED: you may read the source files in this prompt.
Use them to write targeted, minimal fixes.

RULES:

1. MINIMAL CHANGES. Only add or modify code that directly addresses a
   missing scenario. Do not refactor, do not rename, do not reorganize.

2. NO TEST EDITS. Never edit test files. Fix the source so the tests pass.

3. COMPLETE FILES. Emit the full file content for each patch — not diffs,
   not snippets. The existing file content is in the prompt; reproduce it
   with your changes applied.

4. BEST EFFORT. You don't need to be perfect. The agentic debugger will
   fix compile errors and test failures after you. Write the logical fix
   as you understand it.

5. AUTH CONTRACT. Never mint JWTs, never hash passwords. Use the
   existing get_current_user / require_roles dependencies for auth.
   Follow the auth contract exactly.

6. IMPORTS MUST RESOLVE. Only import from stdlib, declared deps, or
   other files visible in this prompt. Don't invent module paths.

OUTPUT: one JSON object `{patches: [{path, content, finding_ids}]}`.
Empty array is valid if no source changes are needed (the tests alone
suffice, and the debugger will handle the rest).
"""


WRITE_PATCHES_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "qe_write_patches_output",
        "schema": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "description": "Source file patches. Empty if no changes needed.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative path of the file to write.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Complete file content — not a diff.",
                            },
                            "finding_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Canonical finding IDs this patch addresses.",
                            },
                        },
                        "required": ["path", "content", "finding_ids"],
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


def build_write_patches_prompt(
    *,
    milestone_name: str,
    enriched_spec_json: str,
    missing_scenarios: List[dict],
    architecture_summary: str,
    source_files: Dict[str, str],
    auth_contract: Optional[str] = None,
) -> str:
    parts = [f"# QE Write Patches: {milestone_name}\n"]

    parts.append(
        "You identified the following missing scenarios and have already "
        "written tests for them. Now write source code patches to address "
        "the gaps. The agentic debugger will run the tests and converge "
        "any remaining failures.\n"
    )

    parts.append("\n## Architecture\n")
    parts.append(architecture_summary + "\n")

    parts.append("\n## EnrichedSpec\n```json\n")
    parts.append(enriched_spec_json.strip() + "\n```\n")

    if auth_contract:
        parts.append("\n## Auth contract\n")
        parts.append(auth_contract.strip() + "\n")

    parts.append("\n## Missing scenarios to address\n")
    for ms in missing_scenarios:
        cap = ms.get("capability_id", "?")
        scenario = ms.get("scenario", "?")
        priority = ms.get("priority", "important")
        parts.append(f"- [{priority}] `{cap}`: {scenario}")
    parts.append("")

    parts.append("\n## Current source files\n")
    if not source_files:
        parts.append("_(no source files — emit an empty patches array)_\n")
    else:
        for path, content in source_files.items():
            if len(content) > 4000:
                head = content[:2000]
                tail = content[-2000:]
                content = head + f"\n\n...[truncated {len(content)-4000} chars]...\n\n" + tail
            parts.append(f"\n### `{path}`\n```\n{content.rstrip()}\n```\n")

    parts.append(
        "\n## Your task\n"
        "Write source code patches to address the missing scenarios above. "
        "Emit `{patches: [...]}` — complete files, not diffs. "
        "Empty array if no source changes are needed.\n"
    )
    return "".join(parts)
