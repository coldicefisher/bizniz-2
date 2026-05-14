"""Deterministic audit battery run after the AuthAgent's tool loop.

Same battery runs in both ``configure`` and ``audit`` modes. The agent
provides the contract markdown + (in configure mode) the list of applied
changes; this module runs the verification checks against live FA and
the workspace, returning an ``AuditReport``.

Five v2.0 checks:

  1. ``audit_jwks_reachable``       — JWKS endpoint is reachable + has keys
  2. ``audit_jwt_signing``          — tenant uses RS256 (or RS384/RS512),
                                      not HS* / none; signing key bound
  3. ``audit_token_validation``     — for each contract test user: live
                                      login produces a valid JWT with
                                      expected claim shape (iss/aud/sub/roles)
  4. ``audit_credential_exposure``  — test passwords don't appear in
                                      workspace files outside ``tests/`` /
                                      ``.env*`` / ``docs/``
  5. ``audit_test_users_in_fa``     — every test user the contract names
                                      actually exists in FA + has the
                                      claimed roles

Deferred to v2.1:

  - ``audit_role_enforcement``      — requires the backend up + a route
                                      inventory; needs hit_endpoint with
                                      cross-role token swaps
  - ``audit_idempotency_replay``    — re-runs materialize with the prior
                                      spec, expects zero diffs
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from bizniz.architect.types import SystemArchitecture
from bizniz.auth_orchestrators.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth_orchestrators.types import FusionAuthError
from bizniz.auth_agent.types import AuditCheck, AuditReport
from bizniz.workspace.base_workspace import BaseWorkspace


# ── Contract parsing ─────────────────────────────────────────────────────


_TEST_USER_RE = re.compile(
    # Accepts any of these surface forms (all observed in the wild
    # across smoke runs of the AuthAgent — the AuthAgent doesn't
    # produce a stable format, so the audit is permissive):
    #   - email / password — role role_name
    #   - email / password — roles a, b
    #   - email / password (Role: role_name)
    #   - email / password (Roles: a, b)
    #   - email / password / ["RoleA", "RoleB"]
    r"^\s*[-*]\s+"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"   # email
    r"\s*/\s*(\S+?)"                                         # password (non-greedy so `/` can terminate it)
    r"\s*"
    r"(?:"
    r"[—–-]\s*roles?\s+([\w,\s_-]+?)"                       # dash form
    r"|"
    r"\(\s*roles?\s*:\s*([\w,\s_-]+?)\s*\)"                 # parens form
    r"|"
    r"/\s*\[\s*([^\]]*?)\s*\]"                              # bracket-list form
    r")"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_ISSUER_RE = re.compile(
    r"^\s*[-*]?\s*Issuer\s*\(?(?:iss\s*claim)?\)?\s*[:=]\s*[`'\"]?([^`'\"\n]+?)[`'\"]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_test_users(contract_md: str) -> List[Tuple[str, str, List[str]]]:
    """Extract (email, password, roles) tuples from the contract's
    'Test users' section. Best-effort — empty list on parse failure
    just causes downstream audits to skip with an explanation."""
    out: List[Tuple[str, str, List[str]]] = []
    for m in _TEST_USER_RE.finditer(contract_md or ""):
        email = m.group(1).strip()
        password = m.group(2).strip()
        # Exactly one of group(3..5) carries the roles list per line.
        roles_raw = (m.group(3) or m.group(4) or m.group(5) or "").strip()
        # Strip stray quotes (bracket-list form has "Role" entries).
        roles = [
            r.strip().strip('"').strip("'")
            for r in re.split(r"[,\s]+", roles_raw)
            if r.strip()
        ]
        out.append((email, password, roles))
    return out


def _parse_issuer(contract_md: str) -> Optional[str]:
    m = _ISSUER_RE.search(contract_md or "")
    return m.group(1).strip() if m else None


# ── JWT helpers ──────────────────────────────────────────────────────────


def _decode_jwt_payload(token: str) -> Optional[dict]:
    """Return the JWT's payload as a dict, or None on parse failure."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        seg = parts[1]
        seg = seg + "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return None


def _decode_jwt_header(token: str) -> Optional[dict]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        seg = parts[0]
        seg = seg + "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return None


# ── Individual audits ────────────────────────────────────────────────────


def audit_jwks_reachable(orch: FusionAuthOrchestrator) -> AuditCheck:
    """Hit FA's JWKS endpoint and confirm it returns at least one key."""
    try:
        jwks = orch.get_jwks()
    except FusionAuthError as e:
        return AuditCheck(
            name="jwks_reachable", passed=False,
            detail=f"FusionAuthError: {e}",
        )
    except Exception as e:
        return AuditCheck(
            name="jwks_reachable", passed=False,
            detail=f"{type(e).__name__}: {e}",
        )
    keys = (jwks or {}).get("keys") or []
    if not keys:
        return AuditCheck(
            name="jwks_reachable", passed=False,
            detail="JWKS endpoint reachable but returned no keys",
        )
    kids = [k.get("kid") for k in keys if k.get("kid")]
    return AuditCheck(
        name="jwks_reachable", passed=True,
        detail=f"{len(keys)} key(s) published; kids={kids}",
    )


def audit_jwt_signing(
    orch: FusionAuthOrchestrator,
    tenant_id: str,
    primary_app_id: str = "",
) -> AuditCheck:
    """Confirm the active signing key is RS-family.

    Checks the application's jwtConfiguration FIRST (application-level
    overrides tenant-level — see set_application_signing_key docstring
    for why we prefer this path; fresh tenants reject PATCH on a known
    FA validator quirk). Falls back to the tenant's signing key if the
    application doesn't override.
    """
    key_id = None
    source = ""
    # Try application-level first (the path AuthAgent's preflight uses).
    if primary_app_id:
        try:
            app = orch.get_application(primary_app_id)
            app_jwt_cfg = (
                (app or {}).get("application", {}).get("jwtConfiguration") or {}
            )
            if app_jwt_cfg.get("enabled") and app_jwt_cfg.get("accessTokenKeyId"):
                key_id = app_jwt_cfg["accessTokenKeyId"]
                source = f"application:{primary_app_id}"
        except Exception:
            # Fall through to tenant.
            pass

    if not key_id:
        try:
            tenant = orch.get_tenant(tenant_id)
        except Exception as e:
            return AuditCheck(
                name="jwt_signing", passed=False,
                detail=f"could not fetch tenant {tenant_id}: {type(e).__name__}: {e}",
            )
        if not tenant or not tenant.get("tenant"):
            return AuditCheck(
                name="jwt_signing", passed=False,
                detail=f"tenant {tenant_id} not found",
            )
        jwt_cfg = tenant["tenant"].get("jwtConfiguration") or {}
        key_id = jwt_cfg.get("accessTokenKeyId")
        source = f"tenant:{tenant_id}"
    if not key_id:
        return AuditCheck(
            name="jwt_signing", passed=False,
            detail=(
                f"neither application {primary_app_id} nor tenant "
                f"{tenant_id} has accessTokenKeyId — JWTs unsigned "
                f"or HS-fallback"
            ),
        )
    try:
        key = orch.get_signing_key(key_id) if hasattr(orch, "get_signing_key") else None
    except Exception:
        key = None

    if key is None:
        # Fall back: list signing keys and find the bound one
        try:
            keys = orch.list_signing_keys()
            key = next((k for k in keys if k.get("id") == key_id), None)
        except Exception as e:
            return AuditCheck(
                name="jwt_signing", passed=False,
                detail=f"could not look up signing key {key_id}: {e}",
            )

    if key is None:
        return AuditCheck(
            name="jwt_signing", passed=False,
            detail=f"tenant references signing key {key_id} but key not found",
        )

    alg = (key.get("algorithm") or "").upper()
    if not alg.startswith("RS"):
        return AuditCheck(
            name="jwt_signing", passed=False,
            detail=(
                f"signing key uses {alg!r} — must be RS256/RS384/RS512. "
                f"HS* and 'none' are unsafe for issuer-validated JWTs."
            ),
        )
    return AuditCheck(
        name="jwt_signing", passed=True,
        detail=f"alg={alg}, key_id={key_id}",
    )


def audit_token_validation(
    orch: FusionAuthOrchestrator,
    primary_app_id: str,
    test_users: List[Tuple[str, str, List[str]]],
    declared_issuer: Optional[str] = None,
) -> List[AuditCheck]:
    """For each test user: log in, decode JWT, verify standard claim
    shape (iss/aud/sub present, roles match contract).

    Returns one AuditCheck per user. Empty list when no test users
    were extractable from the contract (caller should add a meta
    'test_users_parseable' check explaining why)."""
    out: List[AuditCheck] = []
    for email, password, expected_roles in test_users:
        name = f"token_validation:{email}"
        try:
            token = orch.get_token(primary_app_id, email, password)
        except FusionAuthError as e:
            out.append(AuditCheck(
                name=name, passed=False,
                detail=f"login failed: {e}",
            ))
            continue
        except Exception as e:
            out.append(AuditCheck(
                name=name, passed=False,
                detail=f"login raised {type(e).__name__}: {e}",
            ))
            continue

        payload = _decode_jwt_payload(token)
        header = _decode_jwt_header(token)
        if payload is None or header is None:
            out.append(AuditCheck(
                name=name, passed=False,
                detail="token decoded as malformed JWT",
            ))
            continue

        problems: List[str] = []
        for required in ("iss", "aud", "sub"):
            if not payload.get(required):
                problems.append(f"missing claim {required!r}")
        if declared_issuer and payload.get("iss") != declared_issuer:
            problems.append(
                f"iss claim {payload.get('iss')!r} != contract's "
                f"declared issuer {declared_issuer!r}"
            )
        actual_roles = set(payload.get("roles") or [])
        for role in expected_roles:
            if role not in actual_roles:
                problems.append(
                    f"contract claims user has role {role!r} but JWT "
                    f"only carries {sorted(actual_roles)!r}"
                )
        if (header.get("alg") or "").upper().startswith("HS"):
            problems.append(
                f"token signed with {header.get('alg')} (symmetric — unsafe)"
            )

        if problems:
            out.append(AuditCheck(
                name=name, passed=False,
                detail="; ".join(problems),
            ))
        else:
            out.append(AuditCheck(
                name=name, passed=True,
                detail=f"login OK; roles={sorted(actual_roles)}; iss={payload.get('iss')!r}",
            ))
    return out


def audit_credential_exposure(
    workspace: BaseWorkspace,
    test_users: List[Tuple[str, str, List[str]]],
    *,
    allow_path_substrings: Tuple[str, ...] = (
        "/tests/", "/test/", "tests/", "test/",
        ".env", "AUTH_CONTRACT", "docs/",
        "kickstart", "fusionauth/",
    ),
) -> AuditCheck:
    """Grep the workspace for test user passwords. Each should appear
    ONLY in test files / .env files / contract docs / FA kickstart
    config. A hit anywhere else (e.g. inside ``app/api/routes/auth.py``)
    means a hardcoded credential leaked into shipped code.

    Empty test-users list: return SKIPPED (not a failure — the
    contract just didn't declare any).
    """
    if not test_users:
        return AuditCheck(
            name="credential_exposure",
            passed=True,
            detail="(skipped — no test users in contract)",
        )

    passwords = sorted({pw for _email, pw, _roles in test_users if pw})
    if not passwords:
        return AuditCheck(
            name="credential_exposure", passed=True,
            detail="(skipped — contract had no parseable passwords)",
        )

    root = Path(getattr(workspace, "root", "")) if hasattr(workspace, "root") else None
    if root is None or not root.is_dir():
        return AuditCheck(
            name="credential_exposure", passed=True,
            detail="(skipped — workspace root unavailable)",
        )

    leaks: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        # Skip allowed paths
        if any(seg in rel for seg in allow_path_substrings):
            continue
        # Skip large / binary
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for pw in passwords:
            if pw in text:
                leaks.append(f"{rel}: contains {pw!r}")
                break  # one hit per file is enough

    if leaks:
        return AuditCheck(
            name="credential_exposure", passed=False,
            detail=(
                f"test password(s) found in non-test files: "
                + "; ".join(leaks[:10])
                + (f" (and {len(leaks) - 10} more)" if len(leaks) > 10 else "")
            ),
        )
    return AuditCheck(
        name="credential_exposure", passed=True,
        detail=f"no test passwords found outside allowed paths "
        f"(scanned {len(passwords)} password(s))",
    )


def audit_test_users_in_fa(
    orch: FusionAuthOrchestrator,
    primary_app_id: str,
    test_users: List[Tuple[str, str, List[str]]],
) -> AuditCheck:
    """Every test user the contract names should be a real registered
    user in FA on the primary application. Pure existence check —
    role correctness is handled by audit_token_validation."""
    if not test_users:
        return AuditCheck(
            name="test_users_in_fa", passed=True,
            detail="(skipped — no test users in contract)",
        )
    missing: List[str] = []
    for email, _pw, _roles in test_users:
        try:
            user = orch.get_user_by_email(email)
        except Exception as e:
            return AuditCheck(
                name="test_users_in_fa", passed=False,
                detail=f"lookup raised {type(e).__name__}: {e}",
            )
        if not user:
            missing.append(email)
    if missing:
        return AuditCheck(
            name="test_users_in_fa", passed=False,
            detail=f"contract names users not present in FA: {', '.join(missing)}",
        )
    return AuditCheck(
        name="test_users_in_fa", passed=True,
        detail=f"all {len(test_users)} contract user(s) present in FA",
    )


# ── Orchestration ────────────────────────────────────────────────────────


def run_audit_battery(
    *,
    orch: FusionAuthOrchestrator,
    workspace: BaseWorkspace,
    architecture: SystemArchitecture,
    primary_app_id: str,
    tenant_id: str,
    contract_markdown: str,
) -> AuditReport:
    """Run the full v2.0 audit battery. Each check is best-effort and
    returns its own ``AuditCheck`` with passed/failed/skipped status.

    Failures are surfaced in ``AuditReport.failed`` for caller-side
    formatting; the report's ``passed`` is True only when every check
    passed."""
    checks: List[AuditCheck] = []

    # 1. JWKS reachable
    checks.append(audit_jwks_reachable(orch))

    # 2. JWT signing safe
    checks.append(audit_jwt_signing(orch, tenant_id, primary_app_id=primary_app_id))

    # 3. Test-user inventory + token validation
    test_users = _parse_test_users(contract_markdown)
    if not test_users:
        checks.append(AuditCheck(
            name="test_users_parseable", passed=False,
            detail=(
                "could not parse any test users from the contract — "
                "format-shape audits (token validation, credential "
                "exposure, FA presence) will be skipped. Expected lines "
                "like '- user@example.com / password — role rolename'."
            ),
        ))
    else:
        checks.append(AuditCheck(
            name="test_users_parseable", passed=True,
            detail=f"parsed {len(test_users)} test user(s)",
        ))
        declared_issuer = _parse_issuer(contract_markdown)
        # 4. Per-user token validation
        checks.extend(audit_token_validation(
            orch, primary_app_id, test_users,
            declared_issuer=declared_issuer,
        ))
        # 5. Test users present in FA
        checks.append(audit_test_users_in_fa(orch, primary_app_id, test_users))

    # 6. Credential exposure (independent of test_users count — runs anyway)
    checks.append(audit_credential_exposure(workspace, test_users))

    return AuditReport(checks=checks)
