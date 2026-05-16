"""Prompts + JSON schema for the Decomposer agent."""
from __future__ import annotations

from typing import Iterable, Optional


DECOMPOSE_SYSTEM_PROMPT = """\
You are a software-engineering decomposer. Given ONE coarse issue
from the ServicePlanner, break it into an ORDERED list of granular
units of work. The downstream Coder will implement one unit at a
time, run a per-unit test after each, and only advance when the
test passes.

Your job is judgment about **sequencing and granularity**. You are
NOT writing code. You are deciding "what's the minimal next step
that produces a testable signal."

## What a unit of work is

  - ONE new exported symbol (function, class, component, route
    handler, ORM model) OR
  - ONE new behavior added to an existing symbol (new method,
    new prop, new field) OR
  - Pure boilerplate that supports a sibling unit (imports, types,
    constants — set ``kind=bundled_boilerplate`` and
    ``expected_test_kind=no_test_needed`` for these).

Bound each unit to ONE target file. If a unit must touch two
files, split it.

## What a unit is NOT

  - A "feature" that requires 4 files to land coherently. That's an
    issue; you're breaking it INTO units.
  - "Wire X into Y" without specifying which file. Be concrete.
  - Pure documentation or comments. Bundle those with the symbol.

## Sequencing rules

  - Order units in DEPENDENCY ORDER. If unit B uses a symbol unit A
    creates, A comes first.
  - Use ``depends_on`` to make dependencies explicit. List prior
    unit ids OR existing workspace-symbol paths
    (``app/models/user.py::User``).
  - First unit should compile + test successfully against the
    EXISTING workspace. Each subsequent unit then layers cleanly.
  - When the issue creates a vertical slice (model → repo → route →
    component), the order is: model → schema/types → repo → route
    → frontend types → frontend component → wire-up.

## Sizing rules

  - Aim for 30-180 seconds of Coder work per unit. If a unit would
    require >5 method bodies or >50 lines of new code, split.
  - Aim for 3-12 units per issue. Fewer means you're hiding work
    in one giant unit; more means you're micro-fragmenting.
  - If the issue is genuinely tiny (one function, no scaffolding),
    one unit IS acceptable. Don't manufacture units.

## Confidence

Self-rate 0-1 on how well-bounded the decomposition is:
  - 1.0: clear vertical slice, every unit has a single concern,
    dependencies are obvious from the description.
  - 0.6-0.9: minor ambiguity (e.g. you guessed file paths from the
    target_files list).
  - <0.6: the issue itself is ambiguous; surface in ``notes``.

Output a SINGLE JSON object matching the provided schema. No
markdown, no prose around it.
"""


def build_decompose_prompt(
    *,
    issue_id: str,
    issue_title: str,
    issue_description: str,
    issue_target_files: Iterable[str],
    issue_success_criteria: Iterable[str],
    service_name: str,
    service_framework: str,
    architecture_summary: str,
    existing_files_hint: Optional[str] = None,
) -> str:
    """Render the user message for one decomposition call.

    ``existing_files_hint`` is an optional compact view of the
    workspace state at the time of decomposition (so the model can
    distinguish "create file X" from "extend file X that already
    exists").
    """
    target_files = list(issue_target_files or [])
    target_block = "\n".join(f"  - {p}" for p in target_files) or "  (none specified)"
    success_block = "\n".join(f"  - {sc}" for sc in (issue_success_criteria or [])) or "  (none specified)"

    parts = [
        f"# Issue to decompose: {issue_id}\n",
        f"## Title\n{issue_title}\n",
        "## Description\n",
        issue_description.strip() + "\n",
        "\n## Target files (from ServicePlanner)\n",
        target_block + "\n",
        "\n## Success criteria\n",
        success_block + "\n",
        f"\n## Service context\n",
        f"  - name: {service_name}\n",
        f"  - framework: {service_framework}\n",
        "\n## Architecture summary\n",
        architecture_summary.strip() + "\n",
    ]
    if existing_files_hint:
        parts.append("\n## Workspace state (compact)\n")
        parts.append(existing_files_hint.strip() + "\n")

    parts.append(
        "\n## Your task\n"
        f"Decompose issue ``{issue_id}`` into an ordered list of "
        "``UnitOfWork``. Output the single JSON object described in "
        "the system prompt. ``issue_id`` field MUST echo "
        f"``{issue_id}``.\n"
    )
    return "".join(parts)


# JSON schema for response_format=JSON_SCHEMA. Mirrors
# bizniz/decomposer/types.py — keep in sync if the pydantic model
# changes.
DECOMPOSE_SCHEMA = {
    "name": "DecompositionResult",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["issue_id", "ordered_units", "confidence"],
        "properties": {
            "issue_id": {"type": "string"},
            "ordered_units": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id", "summary", "description",
                        "target_file", "kind", "depends_on",
                        "expected_test_kind",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "summary": {"type": "string"},
                        "description": {"type": "string"},
                        "target_file": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "new_symbol",
                                "new_behavior",
                                "bundled_boilerplate",
                            ],
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "expected_test_kind": {
                            "type": "string",
                            "enum": ["unit_test", "no_test_needed"],
                        },
                        "notes": {"type": ["string", "null"]},
                    },
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "notes": {"type": ["string", "null"]},
        },
    },
}
