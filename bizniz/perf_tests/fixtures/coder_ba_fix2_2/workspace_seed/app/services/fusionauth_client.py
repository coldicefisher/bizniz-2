"""FusionAuth HTTP client wrapper and exception hierarchy.

The exception classes defined here are raised by every function in
this module and are the contract on which routes translate failure
modes into HTTP responses (503 for unavailable, 4xx for validation).

Password-never-logged is enforced via the module-level ``_redact``
helper: any field whose key is in ``_REDACT_KEYS`` has its value
replaced with ``'***'`` before the body is rendered in ``__str__``
or written to logs.
"""
from typing import Any, Optional, Union

import httpx

from app.core.config import settings

_REDACT_KEYS = {"password", "currentPassword", "newPassword"}


def _redact(body: Any) -> Any:
    """Recursively replace values of password-like keys with '***'.

    Walks dicts and lists. Non-container values pass through unchanged.
    Used by ``FusionAuthError.__str__`` and by internal logging so a
    plaintext password can never leak into stderr or a traceback.
    """
    if isinstance(body, dict):
        return {
            k: ("***" if k in _REDACT_KEYS else _redact(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [_redact(item) for item in body]
    return body


class FusionAuthError(Exception):
    """Base exception for all FusionAuth client failures.

    Carries the HTTP ``status_code`` (``None`` for transport-level
    errors such as connection refused / timeout), the parsed response
    ``body`` (``dict`` when JSON, ``str`` when not, ``None`` when no
    response was received), and an optional human ``message``.

    Subclasses signal the failure category so routes can map cleanly
    to HTTP status:

    * :class:`FusionAuthUnavailable` — transport failure or 5xx.
    * :class:`FusionAuthValidationError` — 4xx; FA rejected input.
    """

    def __init__(
        self,
        status_code: Optional[int],
        body: Optional[Any],
        message: str = "",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.message = message
        super().__init__(self.__str__())

    def __str__(self) -> str:
        redacted = _redact(self.body)
        parts = [f"status_code={self.status_code}"]
        if self.message:
            parts.append(f"message={self.message!r}")
        parts.append(f"body={redacted!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


class FusionAuthUnavailable(FusionAuthError):
    """FusionAuth is unreachable or returned 5xx.

    Routes should translate this to HTTP 503 with
    ``{"error": "auth_service_unavailable"}``.
    """


class FusionAuthValidationError(FusionAuthError):
    """FusionAuth rejected the request as invalid (4xx).

    Routes should inspect ``body`` to extract field-level errors
    (e.g. ``fieldErrors.user.password``) and surface the appropriate
    structured 4xx response to the client.
    """


def _safe_body(resp: "httpx.Response") -> Union[dict, str]:
    """Return ``resp.json()`` when the body is JSON, else ``resp.text``.

    FusionAuth normally returns JSON for both success and error
    envelopes, but proxies / 5xx upstreams may return HTML or empty
    bodies. Callers stash the result on the raised exception so
    downstream debuggers see *something* even when JSON parsing fails.
    """
    try:
        return resp.json()
    except Exception:
        return resp.text


async def get_jwks() -> dict:
    """Fetch the FusionAuth JSON Web Key Set.

    Issues an unauthenticated GET against
    ``{FUSIONAUTH_URL}/.well-known/jwks.json`` and returns the parsed
    JSON document. Used by the JWT-validation middleware to verify
    RS256 signatures on tokens FusionAuth issues to logged-in users.

    Raises:
        FusionAuthUnavailable: transport-level failure (DNS, connect
            timeout, read timeout, connection refused) OR a 5xx
            response from FusionAuth. Routes translate to HTTP 503.
        FusionAuthValidationError: a 4xx response from FusionAuth
            (should not happen for an unauthenticated JWKS fetch in
            normal operation; surfaced for completeness so the route
            layer can distinguish "auth misconfigured" from "auth
            down").
    """
    url = f"{settings.fusionauth_url}/.well-known/jwks.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise FusionAuthUnavailable(
            status_code=None,
            body=None,
            message=f"FA network error: {exc}",
        )
    if resp.status_code >= 500:
        raise FusionAuthUnavailable(
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    if resp.status_code >= 400:
        raise FusionAuthValidationError(
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    return resp.json()


async def register_user(
    email: str,
    password: str,
    display_name: Optional[str],
    roles: list[str],
) -> dict:
    """Create a new FusionAuth user and register them for the application.

    Issues an authenticated POST against
    ``{FUSIONAUTH_URL}/api/user/registration`` (**no userId in the path**)
    with the body shape mandated by the auth contract::

        {
          "user": {"email": ..., "password": ..., "fullName": ...?},
          "registration": {
            "applicationId": settings.fusionauth_application_id,
            "roles": [...]
          }
        }

    ``fullName`` is included only when ``display_name`` is a non-empty
    string; an empty or ``None`` display name is omitted entirely.

    The ``Authorization`` header carries the raw API key — FusionAuth
    does NOT expect ``Bearer <key>`` for admin endpoints. The key is
    read from ``settings.fusionauth_api_key``.

    Pitfall — do NOT use the path-arg form
    (``/api/user/registration/{userId}``). That endpoint registers an
    *existing* user and will return 400 with
    ``[duplicate]registration`` if the user does not already exist.
    Routes that see this error should treat it as a backend config bug
    (500 ``auth_config_error``), not a user-facing validation error.

    The plaintext ``password`` is forwarded to FusionAuth over the
    transport but is NEVER logged here. Any FA error body that echoes
    the password is redacted by :func:`_redact` before being rendered
    by :meth:`FusionAuthError.__str__`.

    Status-code mapping mirrors :func:`get_jwks` and :func:`login`:

    * transport error → :class:`FusionAuthUnavailable` (status_code=None).
    * 5xx → :class:`FusionAuthUnavailable` with status + body.
    * 4xx → :class:`FusionAuthValidationError` with status + body.
      Callers translate FA's specific codes (400
      ``[duplicate]user.email`` → 409 email_already_registered, 400
      ``fieldErrors.user.password`` → 400 weak_password) into
      user-facing errors by inspecting ``body``.
    * 2xx → parsed JSON body (typically ``{"user": {"id": "...", ...},
      "registration": {...}}``).

    Raises:
        FusionAuthUnavailable: transport-level failure or 5xx response.
        FusionAuthValidationError: any 4xx response.
    """
    url = f"{settings.fusionauth_url}/api/user/registration"
    user: dict = {"email": email, "password": password}
    if display_name is not None and display_name != "":
        user["fullName"] = display_name
    body = {
        "user": user,
        "registration": {
            "applicationId": settings.fusionauth_application_id,
            "roles": roles,
        },
    }
    headers = {
        "Authorization": settings.fusionauth_api_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        raise FusionAuthUnavailable(
            status_code=None,
            body=None,
            message=f"FA network error: {exc}",
        )
    if resp.status_code >= 500:
        raise FusionAuthUnavailable(
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    if resp.status_code >= 400:
        raise FusionAuthValidationError(
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    return resp.json()


async def login(email: str, password: str) -> dict:
    """Exchange credentials for a FusionAuth-issued JWT.

    Issues an unauthenticated POST against ``{FUSIONAUTH_URL}/api/login``
    with body ``{"loginId": email, "password": password,
    "applicationId": settings.fusionauth_application_id}`` and returns
    the parsed JSON response (typically ``{"token": "...", "user": {...}}``).

    The plaintext ``password`` is forwarded to FusionAuth over the
    transport but is NEVER logged here — neither at debug nor error
    level. If a downstream caller wants to log the request, they must
    log only ``loginId`` + ``applicationId`` and elide the password.

    Status-code mapping mirrors :func:`get_jwks` so callers can
    translate the failure category uniformly:

    * transport error (DNS, connect-refused, timeouts) →
      :class:`FusionAuthUnavailable` (status_code=None).
    * 5xx → :class:`FusionAuthUnavailable` with status + body.
    * 4xx → :class:`FusionAuthValidationError` with status + body.
      Callers translate FA's specific codes (404 invalid creds, 423
      locked, 400 weak password, 202 2FA-required, 203 change-password)
      into user-facing errors by inspecting ``status_code`` / ``body``.
    * 2xx → parsed JSON body.

    Raises:
        FusionAuthUnavailable: transport-level failure or 5xx response.
        FusionAuthValidationError: any 4xx response (including 404 for
            invalid credentials — callers map to 401 invalid_credentials).
    """
    url = f"{settings.fusionauth_url}/api/login"
    body = {
        "loginId": email,
        "password": password,
        "applicationId": settings.fusionauth_application_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
    except httpx.RequestError as exc:
        raise FusionAuthUnavailable(
            status_code=None,
            body=None,
            message=f"FA network error: {exc}",
        )
    if resp.status_code >= 500:
        raise FusionAuthUnavailable(
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    if resp.status_code >= 400:
        raise FusionAuthValidationError(
            status_code=resp.status_code,
            body=_safe_body(resp),
        )
    return resp.json()
