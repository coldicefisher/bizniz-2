"""FusionAuthOperator — deterministic apply of an AuthSpec to live FA.

Stages (run in order, each idempotent):
  0. Wait until FA is fully ready (api_key authenticates).
  1. Generate RS256 signing key + bind to app + tenant.
  2. Materialize spec via the orchestrator (apps, roles, users).
  3. Re-ensure each test user (catches role-name mismatches that
     materialize silently skip-userd).
  4. Smoke-login each test user; record login_verified per user.
  5. Read live FA state into an AuthManifest.

Failures inside any stage do NOT raise. They're recorded in the
manifest (login_verified=False, signing_key.algorithm=HS256, etc.)
and surface via the deterministic audit + the pipeline gate. The
contract markdown reflects manifest reality — never claims a user
that didn't get created.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from bizniz.auth_operator.manifest import (
    ApplicationManifest, AuthManifest, RoleManifest, SigningKeyInfo,
    UserManifest,
)
from bizniz.auth_orchestrators.fusionauth_orchestrator import (
    FusionAuthOrchestrator,
)
from bizniz.auth_orchestrators.kickstart import _deterministic_uuid
from bizniz.auth_orchestrators.spec import AuthSpec
from bizniz.auth_orchestrators.types import FusionAuthError


class FusionAuthOperatorError(Exception):
    """Operator-level structural error (cannot reach FA at all,
    cannot derive primary_app_id). Audit-eligible failures are
    recorded in the manifest, not raised."""


class FusionAuthOperator:
    """Deterministic FusionAuth applier. No LLM. Owns all FA quirks."""

    def __init__(
        self,
        orchestrator: FusionAuthOrchestrator,
        on_status: Optional[Callable[[str], None]] = None,
        readiness_deadline_s: float = 600.0,
        readiness_poll_s: float = 5.0,
    ):
        self._orch = orchestrator
        self._on_status = on_status
        self._readiness_deadline_s = readiness_deadline_s
        self._readiness_poll_s = readiness_poll_s

    def apply(
        self,
        *,
        spec: AuthSpec,
        primary_app_id: str,
        tenant_id: str,
    ) -> AuthManifest:
        """Run the full apply pipeline. Returns the post-apply manifest."""
        self._log("FusionAuthOperator: apply starting")

        # Stage 0 — readiness. Don't talk to FA until both /api/status
        # is up AND the api_key authenticates.
        ready = self._orch.wait_until_fully_ready(
            deadline_s=self._readiness_deadline_s,
            poll_s=self._readiness_poll_s,
            on_status=self._on_status,
        )
        if not ready:
            self._log(
                "FusionAuthOperator: FA not fully ready before deadline; "
                "proceeding anyway (manifest will reflect reality)"
            )

        # Stage 1 — signing key. Best-effort with retry.
        signing_key = self._ensure_rs256(
            primary_app_id=primary_app_id, tenant_id=tenant_id,
        )

        # Stage 2 — apps + roles + users via materialize.
        try:
            self._orch.materialize(spec, primary_app_id=primary_app_id)
        except Exception as e:
            self._log(
                f"FusionAuthOperator: materialize raised "
                f"{type(e).__name__}: {str(e)[:200]} — continuing to "
                f"per-user re-ensure"
            )

        # Stage 3 — re-ensure each user (catches materialize silent
        # skips on role-name case mismatch + similar quirks).
        users = self._ensure_users(spec, primary_app_id)

        # Stage 4 — smoke-login each user. Populates login_verified.
        for user in users:
            user.login_verified = self._smoke_login(
                user.email, user.password, primary_app_id,
            )

        # Stage 5 — read live state into the manifest.
        manifest = AuthManifest(
            fa_url=self._orch.base_url,
            primary_app_id=primary_app_id,
            tenant_id=tenant_id,
            issuer=self._read_issuer(tenant_id),
            signing_key=signing_key,
            applications=self._read_applications(spec, primary_app_id),
            roles=self._read_roles(primary_app_id),
            users=users,
        )

        self._log(
            f"FusionAuthOperator: apply done — "
            f"{len(manifest.users)} user(s) "
            f"({sum(1 for u in manifest.users if u.login_verified)} "
            f"login-verified), signing_key={manifest.signing_key.algorithm}"
        )
        return manifest

    # ── Stage 1 ────────────────────────────────────────────────────────

    def _ensure_rs256(
        self, *, primary_app_id: str, tenant_id: str,
    ) -> SigningKeyInfo:
        """Generate RS256 key + bind to app + tenant. Returns the
        active signing key info read back from FA after binding."""
        key_id = _deterministic_uuid("signing-key", primary_app_id)
        bound_app = False
        bound_tenant = False
        for attempt in range(3):
            try:
                self._orch.generate_signing_key(
                    key_id=key_id,
                    name=f"bizniz-app-{primary_app_id}-rs256",
                    algorithm="RS256",
                    length=2048,
                )
                # Tenant binding may fail on fresh tenants (FA
                # validator quirk) — log and continue.
                try:
                    self._orch.set_tenant_signing_key(
                        tenant_id=tenant_id, key_id=key_id,
                    )
                    bound_tenant = True
                except Exception as e:
                    self._log(
                        f"FusionAuthOperator: tenant binding skipped "
                        f"({type(e).__name__}: {str(e)[:120]})"
                    )
                self._orch.set_application_signing_key(
                    app_id=primary_app_id, key_id=key_id,
                )
                bound_app = True
                break
            except Exception as e:
                msg = str(e).lower()
                if attempt < 2 and (
                    "connection" in msg or "unreachable" in msg
                    or "timeout" in msg or "reset" in msg
                ):
                    self._log(
                        f"FusionAuthOperator: signing-key attempt "
                        f"{attempt + 1} hit transient {type(e).__name__}; "
                        f"retrying"
                    )
                    time.sleep(2 + attempt * 3)
                    continue
                self._log(
                    f"FusionAuthOperator: signing-key apply gave up "
                    f"({type(e).__name__}: {str(e)[:120]})"
                )
                break

        # Read back what's actually active. Application-level overrides
        # tenant — check app first, fall back to tenant, fall back to
        # "no key" (audit will fail jwt_signing on that).
        active_key_id, active_alg = self._read_active_signing_key(
            primary_app_id=primary_app_id, tenant_id=tenant_id,
        )
        return SigningKeyInfo(
            key_id=active_key_id or "",
            algorithm=active_alg or "UNKNOWN",
            bound_to_app=bound_app,
            bound_to_tenant=bound_tenant,
        )

    def _read_active_signing_key(
        self, *, primary_app_id: str, tenant_id: str,
    ) -> tuple[str, str]:
        """Resolve the algorithm of the key actually used to sign tokens
        for ``primary_app_id``. App config wins over tenant default."""
        # App-level
        try:
            app = self._orch.get_application(primary_app_id) or {}
            cfg = (app.get("application", {}).get("jwtConfiguration") or {})
            if cfg.get("enabled") and cfg.get("accessTokenKeyId"):
                key_id = cfg["accessTokenKeyId"]
                alg = self._lookup_key_algorithm(key_id)
                return key_id, alg
        except Exception:
            pass
        # Tenant-level
        try:
            tenant = self._orch.get_tenant(tenant_id) or {}
            cfg = (tenant.get("tenant", {}).get("jwtConfiguration") or {})
            if cfg.get("accessTokenKeyId"):
                key_id = cfg["accessTokenKeyId"]
                alg = self._lookup_key_algorithm(key_id)
                return key_id, alg
        except Exception:
            pass
        return "", ""

    def _lookup_key_algorithm(self, key_id: str) -> str:
        try:
            key = self._orch.get_signing_key(key_id)
            if key:
                inner = key.get("key") or key  # FA returns either shape
                return (inner.get("algorithm") or "").upper()
        except Exception:
            pass
        return ""

    # ── Stage 3 ────────────────────────────────────────────────────────

    def _ensure_users(
        self, spec: AuthSpec, primary_app_id: str,
    ) -> List[UserManifest]:
        """Re-ensure every spec test user. materialize() can silently
        skip-user when role names mismatch on the application; we
        explicitly re-ensure each one with the spec's roles so the
        manifest reflects what the contract will claim.
        """
        out: List[UserManifest] = []
        for user_spec in spec.test_users:
            # Make sure each role exists on the app first. If
            # materialize already created them this is a no-op.
            for role_name in user_spec.role_names:
                try:
                    self._orch.ensure_role(
                        app_id=primary_app_id, name=role_name,
                    )
                except Exception as e:
                    self._log(
                        f"FusionAuthOperator: ensure_role({role_name}) "
                        f"raised {type(e).__name__}: {str(e)[:120]}"
                    )

            user_id = ""
            registered = False
            try:
                user_id = self._orch.ensure_user(
                    app_id=primary_app_id,
                    email=user_spec.email,
                    password=user_spec.password,
                    first_name=user_spec.first_name,
                    last_name=user_spec.last_name,
                    roles=list(user_spec.role_names),
                    verified=user_spec.verified,
                    password_change_required=user_spec.password_change_required,
                )
                registered = True
            except Exception as e:
                self._log(
                    f"FusionAuthOperator: ensure_user({user_spec.email}) "
                    f"raised {type(e).__name__}: {str(e)[:200]}"
                )

            out.append(UserManifest(
                email=user_spec.email,
                user_id=user_id or "",
                password=user_spec.password,
                first_name=user_spec.first_name,
                last_name=user_spec.last_name,
                roles=list(user_spec.role_names),
                registered=registered,
                login_verified=False,  # populated in stage 4
            ))
        return out

    # ── Stage 4 ────────────────────────────────────────────────────────

    def _smoke_login(
        self, email: str, password: str, primary_app_id: str,
    ) -> bool:
        try:
            token = self._orch.get_token(primary_app_id, email, password)
            return bool(token)
        except Exception:
            return False

    # ── Stage 5 ────────────────────────────────────────────────────────

    def _read_issuer(self, tenant_id: str) -> str:
        try:
            tenant = self._orch.get_tenant(tenant_id) or {}
            return (tenant.get("tenant", {}).get("issuer")) or ""
        except Exception:
            return ""

    def _read_applications(
        self, spec: AuthSpec, primary_app_id: str,
    ) -> List[ApplicationManifest]:
        out: List[ApplicationManifest] = []
        # We collapse the spec.applications onto the primary app
        # (matches materialize's behavior).
        for app_spec in spec.applications:
            try:
                app = self._orch.get_application(primary_app_id) or {}
                role_objs = (app.get("application", {}).get("roles") or [])
                role_names = sorted(
                    r.get("name") for r in role_objs if r.get("name")
                )
            except Exception:
                role_names = list(app_spec.role_names)
            out.append(ApplicationManifest(
                name=app_spec.name,
                application_id=primary_app_id,
                role_names=role_names,
            ))
        return out

    def _read_roles(self, primary_app_id: str) -> List[RoleManifest]:
        try:
            app = self._orch.get_application(primary_app_id) or {}
            role_objs = (app.get("application", {}).get("roles") or [])
        except Exception:
            return []
        return [
            RoleManifest(
                name=r.get("name", ""),
                description=r.get("description", ""),
                is_super_role=bool(r.get("isSuperRole", False)),
            )
            for r in role_objs if r.get("name")
        ]

    # ── Logging ────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass
