"""Hallucination guard for integration test generation.

The HTTP and WebUI testers can confabulate domain content from
training-data priors — we've seen `grooming.tsx` and
`appointments.tsx` show up in property-management projects.

This module post-validates generated test source against the actual
problem statement. The check is a heuristic: extract domain-suspicious
words from the test source's string literals + identifier names, and
flag any that don't appear in the problem statement (or a generic
test-vocabulary stoplist).

The heuristic is intentionally simple — it only needs to catch
egregious cases (a pet groomer in a property manager). It does not
need to validate every word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Set


# Generic vocabulary that's fine to appear in any test, regardless of
# the problem statement domain. Keep this conservative — anything
# missing here just produces false positives, but anything we add
# wrongly makes the guard less effective.
_GENERIC_VOCAB: Set[str] = {
    # Test/HTTP/auth scaffolding
    "test", "tests", "client", "clients", "fixture", "fixtures", "module",
    "scope", "yield", "assert", "headers", "header", "json", "post", "get",
    "put", "patch", "delete", "request", "requests", "response", "responses",
    "status", "code", "body", "payload", "field", "fields", "valid", "invalid",
    "missing", "expected", "actual", "fail", "pass", "fails", "passes",
    "with", "without", "denied", "allowed", "access",
    # Auth scaffolding
    "auth", "login", "logout", "user", "users", "username", "email", "password",
    "token", "tokens", "access_token", "refresh", "refresh_token", "bearer",
    "register", "registration", "session", "credential", "credentials",
    "role", "roles", "admin", "authenticated", "unauthenticated", "authorize",
    "authorization", "forbidden", "unauthorized", "401", "403", "404", "200",
    "201", "422", "boundary", "happy", "path", "wrong", "correct", "valid",
    # HTTP/UI scaffolding
    "url", "endpoint", "endpoints", "route", "routes", "page", "pages", "form",
    "forms", "input", "inputs", "submit", "click", "fill", "navigate", "redirect",
    "redirects", "modal", "button", "field", "label", "list", "lists", "view",
    "views", "render", "display", "show", "hide", "visible", "browser", "page",
    "context", "fixture", "playwright", "expect", "locator", "console", "error",
    "errors", "log", "logs", "type", "text",
    # Pytest/httpx/zustand-ish scaffolding
    "pytest", "httpx", "fixture", "scope", "module", "session", "function",
    "parametrize", "mark", "skip", "client", "base_url", "timeout", "yield",
    # Common adjectives/verbs/connectives
    "create", "creates", "created", "read", "reads", "update", "updates",
    "updated", "delete", "deletes", "deleted", "list", "lists", "search",
    "searches", "find", "finds", "view", "views", "edit", "edits",
    "happy", "sad", "boundary", "round", "trip", "round_trip", "see", "sees",
    "name", "names", "value", "values", "item", "items", "data", "type",
    "types", "id", "ids", "uuid", "test_user", "test_users", "fake", "real",
    # Programming/structural words
    "self", "this", "if", "else", "for", "while", "return", "import", "from",
    "def", "class", "async", "await", "const", "let", "var", "function",
    "true", "false", "null", "none", "undefined",
    # Generic English filler that shows up in test docstrings/asserts
    "example", "failed", "fails", "passes", "passed", "assuming", "doesn",
    "isn", "wasn", "hasn", "haven", "didn", "won", "should", "shouldn",
    "would", "could", "might", "lacks", "lack", "missing", "present",
    "absent", "exists", "exist", "first", "last", "next", "previous",
    "before", "after", "during", "while", "until", "minimum", "maximum",
    "typical", "common", "normal", "abnormal", "edge", "case", "cases",
    "scenario", "scenarios", "given", "when", "then", "verify", "verifies",
    "ensure", "ensures", "check", "checks", "checking",
    # Common backend concepts that aren't training-prior contamination
    "health", "healthcheck", "version", "metadata", "config", "configuration",
    "environment", "env", "dev", "development", "production", "staging",
    "database", "table", "record", "records", "schema", "migration",
    # More generic English filler that shows up in test docstrings
    "contract", "domain", "failure", "localhost", "malformed", "problem",
    "profile", "provided", "requires", "simulation", "statement", "there",
    "these", "those", "unique", "using", "this", "that", "than", "then",
    "their", "they", "them", "such", "some", "many", "much", "most",
    "least", "more", "less", "very", "just", "also", "only", "other",
    "another", "same", "different", "actually", "really", "still",
    "always", "never", "often", "sometimes", "usually", "rarely",
    "above", "below", "here", "there", "now", "later", "earlier",
    # Common test-data placeholder values
    "pass123", "password123", "test123", "example", "examples", "sample",
    "samples", "demo", "dummy", "placeholder", "lorem", "ipsum",
    # i18n/UI words
    "english", "language", "default", "primary", "secondary",
    # Modal/negation forms missed above
    "cannot", "shall", "ought", "either", "neither",
    # Stdlib/test util common identifiers
    "uuid4", "uuid", "datetime", "timedelta", "freezegun", "monkeypatch",
    "tmp_path", "tmpdir", "mock", "magicmock",
    # Playwright + Jest test-framework API names (these aren't domain
    # nouns; they're test scaffolding that appears in every spec)
    "pageerror", "beforeall", "beforeeach", "afterall", "aftereach",
    "playwright", "chromium", "firefox", "webkit", "browser", "viewport",
    "screenshot", "navigate", "navigation", "tobetruthy", "tobefalsy",
    "tobedefined", "containtext", "havecount", "tohavetext", "tohaveurl",
    "domcontentloaded", "networkidle", "waitfor", "waitforurl",
    "waitforselector", "waitforload", "waitfortimeout", "waitforfunction",
    "describe", "context", "spec", "specs", "expect", "expected",
    "mocha", "jest", "vitest", "chai",
    # Generic web/HTTP terms that aren't domain-specific
    "homepage", "landingpage", "dashboard", "navbar", "topbar", "sidebar",
    "footer", "header", "logout", "signin", "signout", "signup",
}


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
_STRING_LITERAL_RE = re.compile(r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")


@dataclass
class HallucinationReport:
    ok: bool
    suspicious: List[str]   # tokens found in test source not justifiable from problem statement
    sample_contexts: List[str]  # short snippets showing how the suspicious token was used

    def message(self) -> str:
        """Render a corrective message suitable for re-prompting the AI."""
        if self.ok:
            return ""
        bullet_list = "\n".join(f"- '{tok}'" for tok in self.suspicious[:10])
        return (
            "The generated tests reference domain concepts that do NOT appear "
            "in the problem statement. This is the hallucination failure mode "
            "described in the system prompt. Specifically, these tokens were "
            "found in your test source but are not in the problem statement:\n\n"
            f"{bullet_list}\n\n"
            "Re-generate the test file using ONLY domain language from the "
            "problem statement above. If the problem statement does not "
            "mention a noun or verb, do not write a test about it."
        )


def _tokenize(text: str) -> Set[str]:
    """Pull tokens from text. Split on underscores and camelCase so
    ``landlord_headers`` and ``landlordHeaders`` both check their parts
    piece by piece — that way the compound is allowed when each piece
    is allowed, but a confabulated compound like ``groomingAppointment``
    still gets flagged on both halves.
    """
    out: Set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        # Split on underscores AND camelCase boundaries, then lowercase.
        parts = re.split(r"_|(?<=[a-z])(?=[A-Z])", raw)
        for part in parts:
            p = part.lower()
            if len(p) >= 4:
                out.add(p)
    return out


def _significant_tokens_from_test_source(source: str) -> Set[str]:
    """Pull tokens from the high-signal positions in a test file:
    string literals (assertions, URLs, payloads), test names, and
    docstrings. Skip body code that's mostly fixture plumbing."""
    significant_chunks: List[str] = []

    # 1. All string literal contents
    for m in _STRING_LITERAL_RE.finditer(source):
        # strip the quotes
        significant_chunks.append(m.group(0)[1:-1])

    # 2. test_<name> / test('<name>', ...) — pytest and playwright naming
    for m in re.finditer(r"def\s+(test_[A-Za-z0-9_]+)", source):
        significant_chunks.append(m.group(1))
    for m in re.finditer(r"test\(['\"]([^'\"]+)['\"]", source):
        significant_chunks.append(m.group(1))

    # 3. Inline /* */ and // comments and Python docstrings — drop these,
    #    too noisy and not user-facing test content. Skip.

    blob = "\n".join(significant_chunks)
    return _tokenize(blob)


def validate_test_grounding(
    problem_statement: str,
    test_source: str,
    *,
    extra_allowed: Set[str] = frozenset(),
    max_suspicious: int = 2,
) -> HallucinationReport:
    """Check that test source doesn't reference domain concepts absent
    from the problem statement.

    Returns ok=True when no significant unfamiliar tokens are found.
    Returns ok=False when ``max_suspicious`` or more unfamiliar tokens
    show up — that's a strong signal the AI hallucinated a domain.

    ``extra_allowed`` lets callers add project-specific allowlist
    items (e.g. service names from the architecture).
    """
    problem_tokens = _tokenize(problem_statement)
    test_tokens = _significant_tokens_from_test_source(test_source)

    allowed = problem_tokens | _GENERIC_VOCAB | set(extra_allowed)

    # Flag tokens that:
    # - appear in the test
    # - don't appear in the problem statement
    # - aren't generic test vocabulary
    # - are at least 5 chars (avoid noise from short tokens)
    suspicious = sorted(
        {t for t in test_tokens if t not in allowed and len(t) >= 5}
    )

    # Sample contexts so the corrective re-prompt has something concrete
    contexts: List[str] = []
    for tok in suspicious[:5]:
        # Find the first line containing this token
        for line in test_source.splitlines():
            if tok in line.lower():
                contexts.append(line.strip()[:200])
                break

    return HallucinationReport(
        ok=len(suspicious) <= max_suspicious,
        suspicious=suspicious,
        sample_contexts=contexts,
    )
