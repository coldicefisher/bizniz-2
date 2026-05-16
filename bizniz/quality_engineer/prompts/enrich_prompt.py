"""Prompt + JSON schema for ``QualityEngineer.enrich``.

Pre-flight mode: the QE reads the milestone, architecture, and the
auth contract, and produces a production-grade specification the
Engineer will build against.

Mental model the prompt instills: "you are a senior PM/architect who
has shipped this kind of feature before. The Engineer will literally
implement what you specify here, so omitting an error case means
shipping a bug."
"""
from __future__ import annotations

import json
from typing import Iterable, Optional


ENRICH_SYSTEM_PROMPT = """\
You are the QualityEngineer (preflight mode). Your job is to enrich a
milestone into a complete, production-grade specification that the
Engineer can implement against.

You are NOT writing code. You are NOT picking technologies. You are
specifying behavior — what each capability does, what inputs it
accepts, what outputs it returns, what error cases it must handle,
what edge cases lurk, and what test scenarios prove correctness.

A senior engineer about to ship this milestone would want all of the
following before they wrote a line of code:

  - Every distinct capability (one CRUD verb, one query, one workflow
    step is one capability). Don't lump "manage pets" into a single
    item — split it into create_pet, get_pet, list_pets, update_pet,
    delete_pet.

  - For each capability:
      • required + optional inputs (with types AND constraints —
        "email: string, RFC 5322, unique", not just "email: string")
      • outputs (what the API returns; how the UI renders)
      • validation rules the implementation must enforce
      • error cases with status codes AND the trigger ("duplicate
        email → 409", not just "validation errors → 400")
      • edge cases (empty list, max-length input, concurrent writes,
        deleted parent, missing FK, race conditions, Unicode, timezone)
      • auth requirements (which roles, ownership checks)
      • test scenarios (named ideas — happy path + 2-3 negative)

  - Cross-cutting concerns spanning multiple capabilities (logging,
    error envelope, pagination defaults, idempotency keys, audit trail).

  - Anti-patterns that MUST NOT appear in the implementation
    (e.g. "never store plaintext passwords", "never log JWT bodies",
    "never trust client-supplied user_id — use the JWT subject").

GUIDELINES

1. Capability ids: snake_case, stable, semantically descriptive.
   ``create_pet``, ``list_pets_for_owner``, ``mark_appointment_no_show``.
   The Engineer threads these as ``spec_refs`` on issues; bad ids = bad
   traceability.

2. Be specific. "validate input" is useless. "name: 1-100 characters,
   trimmed, no leading/trailing whitespace" is useful.

3. Include error cases proportional to risk. CRUD on a low-stakes
   resource: 3-5 error cases is fine. Auth/payment/PII: enumerate
   exhaustively.

4. Don't invent capabilities outside the milestone's problem_slice.
   If something feels missing, list it under ``cross_cutting`` or as
   an ``anti_pattern`` rather than expanding scope.

5. The auth contract is authoritative. If it says role names are
   ``landlord`` and ``tenant``, do NOT use ``admin``/``user``.

6. Confidence: rate yourself 0-1. Score honestly — the harness
   acts on this rating:
   - >= 0.6: spec ships to the Engineer as-is.
   - 0.4 - 0.6: harness triggers ONE re-enrich pass with an
     augmented "name your ambiguities" prompt. If you genuinely
     can't improve over your first pass, return the same
     confidence — the harness detects the lack of improvement.
   - < 0.4: harness fires a soft gate (halts in --interactive, warns
     in --auto) so a human can review before the Engineer burns
     cycles on an unreliable spec.
   Under-rating wastes a re-enrich call; over-rating ships broken
   specs downstream. Honest self-assessment is load-bearing.

Output JSON ONLY, conforming to the provided schema. No prose.
"""


ENRICH_SCHEMA = {
    "name": "EnrichedSpec",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "milestone_name",
            "capabilities",
            "cross_cutting",
            "anti_patterns",
            "confidence",
        ],
        "properties": {
            "milestone_name": {"type": "string"},
            "capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "name",
                        "description",
                        "inputs",
                        "outputs",
                        "validation_rules",
                        "error_cases",
                        "edge_cases",
                        "auth_required",
                        "allowed_roles",
                        "test_scenarios",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "inputs": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/field"},
                        },
                        "outputs": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/field"},
                        },
                        "validation_rules": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "error_cases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "edge_cases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "auth_required": {"type": "boolean"},
                        "allowed_roles": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "test_scenarios": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "cross_cutting": {
                "type": "object",
                "description": "Map of concern → list of rules.",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "anti_patterns": {
                "type": "array",
                "items": {"type": "string"},
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "$defs": {
            "field": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type", "required", "constraints", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "required": {"type": "boolean"},
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": {"type": "string"},
                },
            },
        },
    },
}


def build_enrich_prompt(
    *,
    milestone_name: str,
    problem_slice: str,
    use_cases: Iterable[str],
    success_criteria: Iterable[str],
    architecture_summary: str,
    auth_contract: Optional[str] = None,
    prior_contracts: Optional[Iterable[str]] = None,
) -> str:
    """Build the user message for an ``enrich`` call.

    ``architecture_summary`` is a compact text summary of the services
    + their interactions. The QE doesn't need full code; it needs to
    know what stack to scope its spec to.

    ``prior_contracts`` is a list of EnrichedSpec JSON strings from
    earlier milestones. Helps the QE avoid contradicting upstream
    decisions and maintain naming consistency across milestones.
    """
    parts = [
        f"# Milestone: {milestone_name}\n",
        "## Problem slice (in scope)\n",
        problem_slice.strip() + "\n",
    ]

    use_cases_list = list(use_cases or [])
    if use_cases_list:
        parts.append("\n## Use cases\n")
        for uc in use_cases_list:
            parts.append(f"- {uc}\n")

    success_criteria_list = list(success_criteria or [])
    if success_criteria_list:
        parts.append("\n## Success criteria\n")
        for sc in success_criteria_list:
            parts.append(f"- {sc}\n")

    parts.append("\n## Architecture (for scoping only)\n")
    parts.append(architecture_summary.strip() + "\n")

    if auth_contract:
        parts.append("\n## Auth contract (AUTHORITATIVE — use exact role names)\n")
        parts.append(auth_contract.strip() + "\n")

    prior_list = list(prior_contracts or [])
    if prior_list:
        parts.append("\n## Prior milestone EnrichedSpecs (for consistency)\n")
        for i, c in enumerate(prior_list, 1):
            parts.append(f"\n### Prior spec {i}\n")
            parts.append("```json\n")
            parts.append(c.strip() + "\n")
            parts.append("```\n")

    parts.append(
        "\n## Your task\n"
        "Produce an EnrichedSpec for this milestone. Output JSON only,\n"
        "conforming to the EnrichedSpec schema.\n"
    )

    return "".join(parts)


def build_reenrich_prompt(
    *,
    milestone_name: str,
    problem_slice: str,
    use_cases: Iterable[str],
    success_criteria: Iterable[str],
    architecture_summary: str,
    auth_contract: Optional[str] = None,
    prior_contracts: Optional[Iterable[str]] = None,
    prior_low_confidence_spec_json: str = "",
    prior_confidence: float = 0.0,
) -> str:
    """Build a user message for a SECOND enrich pass when the first
    came back with confidence in the re-enrich band (default 0.4-0.6).

    The model sees its own prior low-confidence output + an explicit
    instruction to name what made it uncertain and either resolve the
    ambiguities or surface them as TODOs in the spec's notes. Two
    outcomes are valuable: (a) the second pass actually has clearer
    grounding and produces a stronger spec, OR (b) the TODOs land in
    the spec so the Engineer sees them and surfaces them in
    follow-up work rather than silently coding around uncertainty.
    """
    base = build_enrich_prompt(
        milestone_name=milestone_name,
        problem_slice=problem_slice,
        use_cases=use_cases,
        success_criteria=success_criteria,
        architecture_summary=architecture_summary,
        auth_contract=auth_contract,
        prior_contracts=prior_contracts,
    )
    addendum = (
        "\n## RE-ENRICH PASS (confidence was low)\n"
        f"Your prior enrichment of this milestone scored "
        f"confidence={prior_confidence:.2f}, below the 0.6 threshold\n"
        "that triggers this re-pass. The prior EnrichedSpec is below.\n"
        "\n"
        "Do BOTH of the following:\n"
        "\n"
        "1. **Name the ambiguities** explicitly. What about this\n"
        "   milestone made you uncertain? List them as bullet points.\n"
        "   Common causes: unclear scope boundaries, conflicting use\n"
        "   cases, missing architectural decisions, ambiguous data\n"
        "   shapes, role/permission unknowns.\n"
        "\n"
        "2. **Resolve or document each one.** For each ambiguity:\n"
        "   - If you can resolve it by re-reading the architecture\n"
        "     summary / prior specs / auth contract above, do so and\n"
        "     write a sharper capability/scenario in the new spec.\n"
        "   - If you cannot resolve from available context, add a TODO\n"
        "     to the spec's ``notes`` field describing the unknown so\n"
        "     the Engineer can surface it (e.g.\n"
        "     ``TODO: clarify whether sales role can edit other users'\n"
        "     deals or only their own``).\n"
        "\n"
        "Then return a fresh EnrichedSpec (same schema as before). If\n"
        "you genuinely cannot improve over the prior pass, return a\n"
        "spec with the same confidence — the harness will detect the\n"
        "lack of improvement and decide whether to halt for human\n"
        "review.\n"
        "\n"
        "PRIOR LOW-CONFIDENCE SPEC:\n"
        "```json\n"
        f"{prior_low_confidence_spec_json.strip()}\n"
        "```\n"
    )
    return base + addendum


def render_enriched_spec(spec) -> str:
    """Render an EnrichedSpec back to JSON for use as a 'prior contract'
    on subsequent milestones."""
    if hasattr(spec, "model_dump_json"):
        return spec.model_dump_json(indent=2)
    return json.dumps(spec, indent=2)
