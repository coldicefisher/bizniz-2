"""User-prompt builder + JSON schema for ``CodeReviewer.review``."""
from __future__ import annotations

from typing import Dict, Iterable, Optional


CODE_REVIEW_SCHEMA = {
    "name": "CodeReviewReport",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "milestone_name",
            "approved",
            "flagged_symbols",
            "anti_pattern_violations",
            "ungated_auth",
            "missing_error_handling",
            "recommendations",
            "summary",
            "confidence",
        ],
        "properties": {
            "milestone_name": {"type": "string"},
            "approved": {"type": "boolean"},
            "flagged_symbols": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file", "line", "symbol", "kind", "reason", "severity"],
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer", "minimum": 0},
                        "symbol": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "import", "function_call", "attribute",
                                "class", "type", "field",
                            ],
                        },
                        "reason": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning"],
                        },
                    },
                },
            },
            "anti_pattern_violations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file", "line", "anti_pattern", "evidence", "severity"],
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer", "minimum": 0},
                        "anti_pattern": {"type": "string"},
                        "evidence": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning"],
                        },
                    },
                },
            },
            "ungated_auth": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file", "capability_id", "evidence", "severity"],
                    "properties": {
                        "file": {"type": "string"},
                        "capability_id": {"type": "string"},
                        "evidence": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning"],
                        },
                    },
                },
            },
            "missing_error_handling": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file", "capability_id", "error_case", "severity"],
                    "properties": {
                        "file": {"type": "string"},
                        "capability_id": {"type": "string"},
                        "error_case": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning"],
                        },
                    },
                },
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
            },
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
    changed_files: Dict[str, str],
    existing_symbols: Optional[str] = None,
    auth_contract: Optional[str] = None,
    prior_specs: Optional[Iterable[str]] = None,
) -> str:
    """Build the user message for a CodeReviewer call.

    ``changed_files``: path → contents of every file the Engineer
    wrote/modified for this milestone. Only these are reviewed.

    ``existing_symbols``: pre-rendered text snapshot of symbols that
    EXIST in the codebase the Engineer's code can rely on (skeleton
    contracts, library imports, auto-mount conventions, etc.). Helps
    the reviewer distinguish "real" from "fabricated." Optional but
    strongly recommended — without it the reviewer falls back to
    training-data priors which is the same failure mode as the
    Engineer's hallucinations.

    ``prior_specs``: prior milestone EnrichedSpec JSON strings. For
    naming-consistency checks (does this milestone's code reference
    capability ids that match prior specs?).
    """
    parts = [f"# Code Review: {milestone_name}\n"]

    parts.append("\n## EnrichedSpec (the build-against contract)\n")
    parts.append("```json\n")
    parts.append(enriched_spec_json.strip() + "\n")
    parts.append("```\n")

    if auth_contract:
        parts.append("\n## Auth contract (authoritative role names + JWT shape)\n")
        parts.append("```markdown\n")
        parts.append(auth_contract.strip() + "\n")
        parts.append("```\n")

    if existing_symbols:
        parts.append(
            "\n## Existing symbols (REAL — anything not here that's used "
            "in the changed files is suspect)\n"
        )
        parts.append("```\n")
        parts.append(existing_symbols.strip() + "\n")
        parts.append("```\n")

    prior_list = list(prior_specs or [])
    if prior_list:
        parts.append(
            "\n## Prior milestone EnrichedSpecs "
            "(for naming consistency)\n"
        )
        for i, s in enumerate(prior_list, 1):
            parts.append(f"\n### Prior {i}\n```json\n")
            parts.append(s.strip() + "\n")
            parts.append("```\n")

    parts.append("\n## Changed files (the ONLY code under review)\n")
    if not changed_files:
        parts.append(
            "(no files were submitted — nothing to review; "
            "return approved=true with confidence=0)\n"
        )
    else:
        for path, contents in changed_files.items():
            parts.append(f"\n### `{path}`\n```\n")
            # Add line numbers so the reviewer can cite line:N
            numbered = "\n".join(
                f"{i:4d}  {line}"
                for i, line in enumerate(contents.splitlines(), 1)
            )
            parts.append(numbered + "\n```\n")

    parts.append(
        "\n## Your task\n"
        "Produce a CodeReviewReport. Output JSON only. Critical-severity\n"
        "findings block approval; warnings do not. When in doubt about\n"
        "framework magic vs hallucination, mark `warning` and explain.\n"
    )

    return "".join(parts)
