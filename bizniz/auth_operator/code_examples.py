"""Code-example generator for AUTH_CONTRACT.md.

Single LLM call. Takes the AuthManifest (which has the live FA URL,
app ID, issuer, and the actual test users) and the project's stack
languages. Emits a markdown block of language-specific code examples
the Coder can paste into integration tests + production code.

Why an LLM call (vs deterministic templating): the deterministic path
would need a per-language template per operation (login, decode JWT,
require_role, etc.) × every framework we support. The LLM can write
idiomatic snippets for any stack with one prompt. The structured
data (URL, app_id, issuer, user emails) is grounded in the manifest
so it can't be fabricated.
"""
from __future__ import annotations

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
) -> str:
    """Single LLM call. Returns the rendered ``## Code samples``
    markdown section ready to append to the contract.

    Returns an empty string on failure — the contract is still useful
    without code samples; this is additive.
    """
    if not languages:
        return ""

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
