"""Code-example generator for AUTH_CONTRACT.md.

Two paths:

1. **Skeleton template** (CTX-3, preferred): each skeleton ships an
   ``AUTH_CONTRACT_EXAMPLES.md.template`` with verified-working
   examples using the EXACT libraries the skeleton's
   requirements.txt declares. Placeholders ({{primary_app_id}},
   etc.) are substituted from the AuthManifest. Zero LLM call;
   zero "agent invents wrong library" risk.

2. **LLM fallback** (legacy): when no template exists in the
   skeleton, a single LLM call produces the section from the
   manifest. Kept for back-compat with skeletons that haven't
   added the template yet.

The skeleton-template path drops the FROM-SCRATCH LLM generation
that was producing PyJWT examples when the skeleton only shipped
python-jose (the recipe_v4_v10 bug, 2026-05-20).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.auth_operator.manifest import AuthManifest
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.lib.llm_utils import call_with_retry


_SYSTEM_PROMPT = """\
You write a markdown ``## Code samples`` section for an
``AUTH_CONTRACT.md`` document. The contract describes a FusionAuth
identity setup; downstream agents copy your code samples into
backend, frontend, and integration-test files.

You receive:
  - The live FusionAuth URL, primary application ID, and issuer
  - The project's stack languages (e.g. python, typescript)
  - The contract's test users (email, password, roles)

For EACH language in the stack, emit one fenced code block per
operation:
  - Login (POST /api/login → JWT token)
  - Decode / validate JWT (RS256 via JWKS)
  - **Register / create a user** (combined create-user-and-register
    via ``POST /api/user/registration`` — body holds
    ``user.email``, ``user.password``, and ``registration.applicationId``
    + ``registration.roles``. CRITICAL: do NOT put a UUID in the URL
    path; ``POST /api/user/registration/{userId}`` is a DIFFERENT
    endpoint that registers an EXISTING user — passing the application
    ID there triggers a "user already exists" 400 with the same UUID
    echoed as both userId and registration target. The url
    ``POST /api/user/registration`` with no path arg is the right
    endpoint for new-user signup.)
  - **Assign / change roles for a user** (PATCH the user's
    registration: ``PATCH /api/user/registration/{userId}`` with body
    ``{"registration": {"applicationId": "...", "roles": [...]}}``).
  - **Use a logged-in user in an integration test**

For password policy: FusionAuth's default minimum password length
is 8, requires mixed case, number, and symbol. If you write a test
that creates a user, the password MUST satisfy these rules
(``Password123!`` works). A weak password returns 400 with
``fieldErrors.user.password``.

Auth API key header is ``Authorization: <api_key>`` (raw, no
``Bearer ``). The api key is read from the
``FUSIONAUTH_API_KEY`` env var inside service containers.

Use the actual values from the manifest (URL, app_id, etc.). Use
``http://auth:9011`` as the URL when running inside the docker
network — that's what containers see, not the host port. Use one
of the actual test users in test snippets.

Keep snippets short and idiomatic. Don't include explanatory prose
between blocks beyond a short header sentence per language.

Return JSON with one field: ``markdown`` (the full ``## Code
samples`` section as a single string).
"""

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "auth_code_examples",
        "schema": {
            "type": "object",
            "properties": {
                "markdown": {"type": "string"},
            },
            "required": ["markdown"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def generate_code_examples(
    *,
    client: BaseAIClient,
    manifest: AuthManifest,
    languages: List[str],
    on_status: Optional[Callable[[str], None]] = None,
    max_retries: int = 2,
    skeleton_paths: Optional[List[Path]] = None,
) -> str:
    """Returns the rendered ``## Code samples`` markdown section.

    Order of attempts:
    1. **Skeleton template** (CTX-3): if any provided skeleton path
       contains ``AUTH_CONTRACT_EXAMPLES.md.template``, load it,
       substitute placeholders from the manifest, return.
    2. **LLM fallback**: single LLM call produces the section from
       the manifest. Used when no template is found.

    Returns an empty string on failure — the contract is still useful
    without code samples; this is additive.
    """
    if not languages:
        return ""

    # CTX-3 (2026-05-20): try the skeleton-shipped template first.
    template_md = _try_skeleton_template(
        manifest=manifest,
        skeleton_paths=skeleton_paths or [],
        on_status=on_status,
    )
    if template_md:
        return template_md
    # Fall through to LLM generation.

    # Build the user prompt as compact JSON-ish data (no Pydantic
    # serialization needed; the LLM reads it as text).
    users_block = "\n".join(
        f"  - {u.email} / {u.password} — roles: {', '.join(u.roles) or '(none)'}"
        for u in manifest.users
    ) or "  (no test users)"

    user_prompt = (
        "## Manifest\n\n"
        f"- FusionAuth URL (in-network): http://auth:9011\n"
        f"- FusionAuth URL (host): {manifest.fa_url}\n"
        f"- Primary application ID: {manifest.primary_app_id}\n"
        f"- Tenant ID: {manifest.tenant_id}\n"
        f"- Issuer: {manifest.issuer or '(not set)'}\n"
        f"- Signing algorithm: {manifest.signing_key.algorithm}\n"
        f"\n## Stack languages\n\n"
        f"{', '.join(sorted(set(languages)))}\n"
        f"\n## Test users\n\n"
        f"{users_block}\n"
        f"\n## Your job\n\n"
        f"Emit the ``## Code samples`` markdown section. One subsection "
        f"per language. Code blocks for: login, decode/validate JWT, "
        f"REGISTER a new user (POST /api/user/registration with no "
        f"path arg), CHANGE a user's roles, integration-test usage. "
        f"Reference the live URLs/IDs above. Use http://auth:9011 "
        f"inside containers."
    )

    if on_status:
        on_status(
            f"AuthOperator: generating code samples for "
            f"{', '.join(languages)}"
        )

    try:
        raw = call_with_retry(
            client=client,
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=__import__(
                "bizniz.clients.chatgpt.types.response_format",
                fromlist=["ResponseFormat"],
            ).ResponseFormat.JSON_SCHEMA,
            schema=_SCHEMA,
            max_attempts=max_retries,
            on_status=on_status,
            label="AuthOperator.code_examples",
        )
    except Exception as e:
        if on_status:
            on_status(
                f"AuthOperator: code-sample generation failed "
                f"({type(e).__name__}: {str(e)[:120]}); contract will "
                f"ship without samples"
            )
        return ""

    md = (raw or {}).get("markdown") or ""
    return md.strip()



# ── CTX-3 (2026-05-20): skeleton-template path ────────────────────


_TEMPLATE_FILENAME = "AUTH_CONTRACT_EXAMPLES.md.template"


def _try_skeleton_template(
    *,
    manifest: AuthManifest,
    skeleton_paths: List[Path],
    on_status: Optional[Callable[[str], None]] = None,
) -> str:
    """Look for AUTH_CONTRACT_EXAMPLES.md.template in any provided
    skeleton path; load + substitute + return. Empty string if none
    found or substitution fails.

    Substitution: ``{{placeholder}}`` → value, applied via simple
    ``str.replace`` (no LLM, no Jinja). The skeleton author writes
    canonical values; pipeline substitutes runtime ones.
    """
    template_text = ""
    template_source = ""
    for sk_path in skeleton_paths:
        candidate = Path(sk_path) / _TEMPLATE_FILENAME
        if candidate.exists() and candidate.is_file():
            try:
                template_text = candidate.read_text(encoding="utf-8")
                template_source = str(candidate)
                break
            except Exception:
                continue
    if not template_text:
        if on_status:
            on_status(
                "AuthOperator: no AUTH_CONTRACT_EXAMPLES.md.template "
                "in skeletons — falling back to LLM generation"
            )
        return ""

    substitutions = _build_substitutions(manifest)
    rendered = template_text
    for placeholder, value in substitutions.items():
        rendered = rendered.replace("{{" + placeholder + "}}", value)

    if on_status:
        on_status(
            f"AuthOperator: rendered code samples from "
            f"{template_source} (deterministic, "
            f"{len(substitutions)} substitutions)"
        )
    return rendered.strip()


def _build_substitutions(manifest: AuthManifest) -> dict:
    """Map of placeholder → substitution value, from the manifest."""
    # Pick a representative test user (first one, by convention).
    test_email = ""
    test_password = ""
    if manifest.users:
        test_email = manifest.users[0].email or ""
        test_password = manifest.users[0].password or ""
    return {
        "primary_app_id": manifest.primary_app_id or "",
        "tenant_id": manifest.tenant_id or "",
        "issuer": manifest.issuer or "",
        "fa_url_host": manifest.fa_url or "",
        "fa_url_container": "http://auth:9011",
        "signing_algorithm": (
            manifest.signing_key.algorithm if manifest.signing_key else "RS256"
        ),
        "test_user_email": test_email,
        "test_user_password": test_password,
    }
