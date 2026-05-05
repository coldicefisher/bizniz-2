"""Contract-shape guard for WebUITester output.

Catches the failure mode where the AI's playwright test fills a form
with field names that don't match the backend's OpenAPI request body
schema. Example: test does ``page.fill('input[name="username"]', ...)``
to log in, but the backend's POST /auth/login expects ``email``.

The fix is structural (regenerate types from OpenAPI), but this guard
is the cheap reactive check that runs at test-generation time and
re-prompts on drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Set


# Match `name="X"` and `name='X'` inside input/select/textarea selectors
# in playwright code. Captures everything from `input[name="..."]`,
# `select[name="..."]`, `textarea[name="..."]`, and bare `name="..."`
# attribute selectors.
_NAME_ATTR_RE = re.compile(
    r"""(?:input|select|textarea)?\[\s*name\s*=\s*['"]([A-Za-z0-9_]+)['"]"""
)


# Field names that are commonly UI-only (not part of any backend
# request body) and therefore should NOT be flagged when they don't
# appear in the OpenAPI schemas.
_UI_ONLY_FIELDS: Set[str] = {
    "confirm_password", "confirmpassword", "password_confirm",
    "remember", "remember_me", "rememberme",
    "terms", "agree", "accept_terms",
    "newsletter", "marketing",
    "search", "query", "filter",
}


@dataclass
class ContractDriftReport:
    ok: bool
    drifted: List[str]   # form-field names that don't match any OpenAPI request schema
    suggestions: Dict[str, List[str]]  # for each drifted name, similarly-named fields that DO exist

    def message(self) -> str:
        if self.ok:
            return ""
        lines = [
            "Your test fills form fields with names that do NOT appear "
            "in any backend OpenAPI request body schema. The backend "
            "will reject these submissions with 422 Validation Error, "
            "and the test will fail not because of a real bug but "
            "because the field names are wrong.\n",
        ]
        for name in self.drifted[:8]:
            line = f"- 'name=\"{name}\"' is not in any request schema"
            sugg = self.suggestions.get(name) or []
            if sugg:
                line += f". Did you mean: {', '.join(sugg[:3])}?"
            lines.append(line)
        lines.append(
            "\nRe-generate the test using the EXACT field names from "
            "the OpenAPI request body schemas above. Do not invent or "
            "guess field names."
        )
        return "\n".join(lines)


def _collect_request_field_names(backend_contracts: Dict[str, dict]) -> Set[str]:
    """Pull every property name out of every requestBody schema across
    all backends. The set is used as the allowed-fields universe for
    form inputs."""
    fields: Set[str] = set()
    for doc in (backend_contracts or {}).values():
        for ops in (doc.get("paths") or {}).values():
            if not isinstance(ops, dict):
                continue
            for op in ops.values():
                if not isinstance(op, dict):
                    continue
                rb = op.get("requestBody") or {}
                schema = (
                    rb.get("content", {}).get("application/json", {}).get("schema")
                )
                _collect_from_schema(schema, fields, doc)
    return fields


def _collect_from_schema(schema, fields: Set[str], doc: dict, depth: int = 0) -> None:
    if depth > 5 or not isinstance(schema, dict):
        return
    # Resolve $ref
    if "$ref" in schema and isinstance(schema["$ref"], str):
        ref = schema["$ref"]
        if ref.startswith("#/"):
            target: object = doc
            for p in ref[2:].split("/"):
                if isinstance(target, dict) and p in target:
                    target = target[p]
                else:
                    return
            if isinstance(target, dict):
                _collect_from_schema(target, fields, doc, depth + 1)
        return
    props = schema.get("properties") or {}
    for k, v in props.items():
        fields.add(k.lower())
        _collect_from_schema(v, fields, doc, depth + 1)
    # Composition: anyOf/oneOf/allOf
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            _collect_from_schema(sub, fields, doc, depth + 1)


def _name_attrs_in_test(source: str) -> List[str]:
    """Extract `name="X"` field names that the test will actually submit.

    Skips selectors that appear inside a ``.or(...)`` fallback chain —
    those are MULTI-STRATEGY ALTERNATIVES, not the field being filled.
    Playwright's ``locator(...).or(locator(...))`` picks the first one
    that matches at runtime, so only the primary selector represents a
    real submission target. Flagging the alternatives produced false
    positives for resilient login forms (e.g. an `email`-primary
    selector with `username`/`login` fallbacks).

    A selector is considered "in a .or() chain" if it appears between
    a ``.or(`` token and the matching close-paren on the same logical
    line. We use a regex over the raw source — quick and good enough
    for the structured Playwright code these tests generate.
    """
    # Build a mask of byte positions inside .or(...) calls. Any name
    # match inside the mask is a fallback and skipped.
    or_ranges: List[tuple] = []
    i = 0
    while True:
        idx = source.find(".or(", i)
        if idx == -1:
            break
        # Find the matching close paren
        depth = 1
        j = idx + 4
        while j < len(source) and depth > 0:
            ch = source[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        or_ranges.append((idx, j))
        i = j

    def _in_or_chain(pos: int) -> bool:
        for start, end in or_ranges:
            if start <= pos < end:
                return True
        return False

    return [
        m.group(1).lower()
        for m in _NAME_ATTR_RE.finditer(source)
        if not _in_or_chain(m.start())
    ]


def _suggest_similar(name: str, candidates: Set[str]) -> List[str]:
    """Cheap suggestion: substring match either way."""
    name_l = name.lower()
    out = []
    for c in sorted(candidates):
        if name_l in c or c in name_l:
            out.append(c)
    return out


def validate_form_field_contract(
    test_source: str,
    backend_contracts: Dict[str, dict],
) -> ContractDriftReport:
    """Returns ``ok=False`` if the test fills any form field whose
    name is not present in ANY backend OpenAPI requestBody schema
    AND is not a known UI-only field."""
    if not backend_contracts:
        return ContractDriftReport(ok=True, drifted=[], suggestions={})

    allowed_fields = _collect_request_field_names(backend_contracts)
    name_attrs = _name_attrs_in_test(test_source)
    if not name_attrs:
        return ContractDriftReport(ok=True, drifted=[], suggestions={})

    drifted: List[str] = []
    suggestions: Dict[str, List[str]] = {}
    seen: Set[str] = set()
    for name in name_attrs:
        if name in seen:
            continue
        seen.add(name)
        if name in _UI_ONLY_FIELDS:
            continue
        if name in allowed_fields:
            continue
        drifted.append(name)
        sims = _suggest_similar(name, allowed_fields)
        if sims:
            suggestions[name] = sims

    return ContractDriftReport(
        ok=len(drifted) == 0,
        drifted=drifted,
        suggestions=suggestions,
    )
