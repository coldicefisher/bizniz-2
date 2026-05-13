"""SmokePhase — cheap deterministic gate that runs after IMPLEMENT.

No LLM calls. Just curl: does the stack actually answer real user-facing
HTTP requests? Catches the v33-class bug where the milestone shipped
"14/14 green" but POST /api/v1/landlords/register returned 500 because
the Coder used ``settings.fusionauth_application_id`` (a name that
doesn't exist). Tests-pass != feature works; this is the gate that
proves the difference.

Three checks, all run from the host against the live compose stack:

  1. Service health — for every backend in the architecture, GET
     /health expects 200. Catches "container is up but app crashed
     at startup."
  2. Auth public-login — for every test user in the AUTH_CONTRACT,
     POST /api/login WITHOUT the API key expects 200 + a token.
     Catches the requireAuthentication=true bug and password-policy
     mismatches at the SPA-facing boundary.
  3. Route registration — for every path in the live OpenAPI doc,
     GET (or HEAD) the path with the landlord JWT. Any 5xx fails
     the gate. 401/403/404 on protected routes are fine. The
     intent is "do registered endpoints crash on touch?" — not
     "do they return correct data."

Result is a per-check pass/fail list. The milestone loop hard-gates
on critical failures (auth login, health) and warns on route 5xxs.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
from pydantic import BaseModel, Field

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.planner.types import Milestone


class SmokeCheck(BaseModel):
    """One smoke check's outcome."""
    name: str
    category: str  # "health" | "auth_login" | "route"
    target: str  # URL or endpoint identifier
    passed: bool
    status_code: Optional[int] = None
    detail: str = ""


class SmokePhaseResult(BaseModel):
    """Aggregate result returned to MilestoneLoop."""
    passed: bool
    checks: List[SmokeCheck] = Field(default_factory=list)
    duration_s: float = 0.0
    critical_failures: List[str] = Field(default_factory=list)

    @property
    def failed_checks(self) -> List[SmokeCheck]:
        return [c for c in self.checks if not c.passed]


class SmokePhase:
    """Deterministic post-implement gate. No LLM."""

    def __init__(
        self,
        timeout_s: float = 5.0,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._timeout_s = timeout_s
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def run(
        self,
        milestone: Milestone,
        architecture: SystemArchitecture,
        project_root: Path,
        auth_contract: Optional[str] = None,
    ) -> SmokePhaseResult:
        """Walk the stack with curl; return a structured pass/fail."""
        t0 = time.time()
        checks: List[SmokeCheck] = []
        critical_failures: List[str] = []

        self._log(
            f"SmokePhase: starting for "
            f"M{milestone.sequence_index + 1} '{milestone.name}'"
        )

        # ── 1. Health endpoints for every backend ───────────────────────
        backends = [
            s for s in architecture.services
            if (s.service_type or "").lower() == "backend"
        ]
        for backend in backends:
            url = f"http://localhost:{backend.port}/health"
            check = self._probe_health(backend.name, url)
            checks.append(check)
            if not check.passed:
                critical_failures.append(
                    f"health[{backend.name}] {check.detail}"
                )

        # ── 2. Public-flow login for every test user in the contract ───
        fa_service = self._find_fa_service(architecture)
        test_users = self._parse_test_users(auth_contract or "")
        app_id = self._parse_primary_app_id(auth_contract or "")
        tokens: Dict[str, str] = {}
        if fa_service is None or not test_users or not app_id:
            self._log(
                "SmokePhase: skipping auth checks "
                f"(fa={fa_service is not None}, users={len(test_users)}, "
                f"app_id={'yes' if app_id else 'no'})"
            )
        else:
            fa_url = f"http://localhost:{fa_service.port}"
            for email, password in test_users:
                check, token = self._probe_login(
                    fa_url, app_id, email, password,
                )
                checks.append(check)
                if not check.passed:
                    critical_failures.append(
                        f"auth_login[{email}] {check.detail}"
                    )
                elif token:
                    tokens[email] = token

        # ── 3. Route registration on every backend ─────────────────────
        # Use any token we got; if none, skip route checks (we can't
        # tell crash from "auth refused" without one).
        token = next(iter(tokens.values()), None)
        for backend in backends:
            base = f"http://localhost:{backend.port}"
            try:
                openapi = requests.get(
                    f"{base}/openapi.json", timeout=self._timeout_s,
                ).json()
            except Exception as e:
                checks.append(SmokeCheck(
                    name=f"openapi:{backend.name}",
                    category="route",
                    target=f"{base}/openapi.json",
                    passed=False,
                    detail=f"failed to fetch openapi: {type(e).__name__}: {e}",
                ))
                continue
            for path, methods in (openapi.get("paths") or {}).items():
                for method in methods:
                    if method.lower() not in (
                        "get", "post", "put", "patch", "delete",
                    ):
                        continue
                    # Don't actually mutate state in smoke — only GET.
                    if method.lower() != "get":
                        continue
                    check = self._probe_route(
                        backend.name, base, path, token,
                    )
                    checks.append(check)
                    # 5xx on a registered route IS critical — means
                    # the handler crashes on touch.
                    if not check.passed and check.status_code and check.status_code >= 500:
                        critical_failures.append(
                            f"route[{method.upper()} {path}] "
                            f"5xx {check.status_code}"
                        )

        # ── 4. Frontend probes ─────────────────────────────────────────
        # Deterministic checks the SPA can actually serve and proxy.
        # Catches the v33-era class of bugs where Coder shipped
        # correct code but skeleton/provisioner glue prevented it
        # from reaching the user. Currently three checks:
        #   - GET / on the frontend port — index HTML responds
        #   - POST /api/v1/auth/login through the frontend proxy —
        #     catches "vite proxy target points at wrong service"
        #   - GET /login on the frontend port — HTML contains an
        #     <input> tag (catches "skeleton placeholder shadowing
        #     Coder's real form")
        frontends = [
            s for s in architecture.services
            if (s.service_type or "").lower() == "frontend"
        ]
        # Use one of the test-user credentials for the proxy login probe.
        proxy_creds = test_users[0] if test_users else None
        for frontend in frontends:
            base = f"http://localhost:{frontend.port}"

            index_check = self._probe_frontend_index(frontend.name, base)
            checks.append(index_check)
            if not index_check.passed:
                critical_failures.append(
                    f"frontend_index[{frontend.name}] {index_check.detail}"
                )

            if proxy_creds and app_id:
                email, password = proxy_creds
                proxy_check = self._probe_frontend_proxy_login(
                    frontend.name, base, email, password,
                )
                checks.append(proxy_check)
                if not proxy_check.passed:
                    critical_failures.append(
                        f"frontend_proxy[{frontend.name}] {proxy_check.detail}"
                    )

            login_check = self._probe_frontend_login_route_has_form(
                frontend.name, base,
            )
            checks.append(login_check)
            # Non-critical: SPAs may legitimately render forms in JS
            # only (no <input> in initial HTML). Treat as warning.

        passed = len(critical_failures) == 0
        duration = time.time() - t0
        self._log(
            f"SmokePhase: done in {duration:.1f}s — "
            f"{sum(1 for c in checks if c.passed)}/{len(checks)} checks ok, "
            f"{len(critical_failures)} critical failure(s)"
        )
        return SmokePhaseResult(
            passed=passed,
            checks=checks,
            duration_s=duration,
            critical_failures=critical_failures,
        )

    # ── Frontend probes ────────────────────────────────────────────────

    def _probe_frontend_index(
        self, service_name: str, base: str,
    ) -> SmokeCheck:
        """GET /. Expect 200 + non-empty HTML."""
        try:
            resp = requests.get(base + "/", timeout=self._timeout_s)
        except Exception as e:
            return SmokeCheck(
                name=f"frontend_index:{service_name}",
                category="frontend",
                target=base + "/",
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
        ok = resp.status_code == 200 and bool((resp.text or "").strip())
        return SmokeCheck(
            name=f"frontend_index:{service_name}",
            category="frontend",
            target=base + "/",
            passed=ok,
            status_code=resp.status_code,
            detail=(
                "ok" if ok else
                f"status={resp.status_code}, body={len(resp.text or '')} bytes"
            ),
        )

    def _probe_frontend_proxy_login(
        self, service_name: str, base: str, email: str, password: str,
    ) -> SmokeCheck:
        """POST /api/v1/auth/login through the frontend's dev-server
        proxy. Mirrors the path the browser takes when the SPA's API
        client calls backend routes. 502 here means the proxy target
        is wrong (skeleton hardcoded the wrong service name).

        We don't fail on a 4xx — the backend may legitimately reject
        these credentials. We fail on 5xx, network errors, or "no
        backend reachable through proxy."
        """
        url = base + "/api/v1/auth/login"
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json={"email": email, "password": password},
                timeout=self._timeout_s,
            )
        except Exception as e:
            return SmokeCheck(
                name=f"frontend_proxy:{service_name}",
                category="frontend",
                target=url,
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
        # 502 = bad gateway = vite proxy can't reach upstream.
        # Other 5xx = backend crashed or wiring broken.
        if resp.status_code >= 500:
            return SmokeCheck(
                name=f"frontend_proxy:{service_name}",
                category="frontend",
                target=url,
                passed=False,
                status_code=resp.status_code,
                detail=(
                    f"5xx through proxy ({resp.status_code}) — "
                    f"check vite.config.ts proxy target matches the "
                    f"actual backend service name"
                ),
            )
        return SmokeCheck(
            name=f"frontend_proxy:{service_name}",
            category="frontend",
            target=url,
            passed=True,
            status_code=resp.status_code,
            detail=(
                f"proxy reaches backend (got {resp.status_code} — "
                f"4xx fine, means backend responded)"
            ),
        )

    def _probe_frontend_login_route_has_form(
        self, service_name: str, base: str,
    ) -> SmokeCheck:
        """GET /login. Look for any <input> tag in the response body.
        Non-critical: many SPAs render forms via JS-only, so the
        initial HTML may not contain inputs. A warning when we don't
        find any — the user can investigate.

        Catches the "skeleton placeholder still rendering instead of
        Coder's real form" class — the placeholder is usually a
        styled <div> with no inputs.
        """
        url = base + "/login"
        try:
            resp = requests.get(url, timeout=self._timeout_s)
        except Exception as e:
            return SmokeCheck(
                name=f"frontend_login_form:{service_name}",
                category="frontend",
                target=url,
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
        body = (resp.text or "").lower()
        # SPAs: the body is usually the vite-served index.html for ANY
        # route (client-side router handles /login). So the absence of
        # <input> in raw HTML isn't a hard fail — just a signal.
        has_input = "<input" in body
        return SmokeCheck(
            name=f"frontend_login_form:{service_name}",
            category="frontend",
            target=url,
            passed=resp.status_code < 500,
            status_code=resp.status_code,
            detail=(
                "html served (form rendering is JS-driven for SPAs)"
                if has_input else
                "html served; no <input> in initial HTML "
                "(expected for SPAs — JS will render). "
                "If your /login shows a placeholder, check that "
                "skeleton App.tsx mounts auto-discovered routes "
                "BEFORE hardcoded placeholders."
            ),
        )

    # ── Probes ──────────────────────────────────────────────────────────

    def _probe_health(self, service_name: str, url: str) -> SmokeCheck:
        try:
            resp = requests.get(url, timeout=self._timeout_s)
        except Exception as e:
            return SmokeCheck(
                name=f"health:{service_name}",
                category="health",
                target=url,
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
        ok = resp.status_code == 200
        return SmokeCheck(
            name=f"health:{service_name}",
            category="health",
            target=url,
            passed=ok,
            status_code=resp.status_code,
            detail="ok" if ok else f"unexpected status {resp.status_code}",
        )

    def _probe_login(
        self, fa_url: str, app_id: str, email: str, password: str,
    ) -> tuple:
        url = f"{fa_url}/api/login"
        try:
            resp = requests.post(
                url,
                # CRITICAL: no Authorization header. We're testing
                # the same path the SPA frontend uses.
                headers={"Content-Type": "application/json"},
                json={
                    "applicationId": app_id,
                    "loginId": email,
                    "password": password,
                },
                timeout=self._timeout_s,
            )
        except Exception as e:
            check = SmokeCheck(
                name=f"auth_login:{email}",
                category="auth_login",
                target=url,
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
            return check, None
        if resp.status_code != 200:
            return SmokeCheck(
                name=f"auth_login:{email}",
                category="auth_login",
                target=url,
                passed=False,
                status_code=resp.status_code,
                detail=(
                    f"public login returned {resp.status_code} "
                    f"(was the API key bypass enabled? "
                    f"requireAuthentication should be false)"
                ),
            ), None
        try:
            token = resp.json().get("token") or ""
        except Exception:
            token = ""
        if not token:
            return SmokeCheck(
                name=f"auth_login:{email}",
                category="auth_login",
                target=url,
                passed=False,
                status_code=resp.status_code,
                detail="200 OK but no token in response",
            ), None
        return SmokeCheck(
            name=f"auth_login:{email}",
            category="auth_login",
            target=url,
            passed=True,
            status_code=200,
            detail="public-flow login succeeded",
        ), token

    def _probe_route(
        self,
        service_name: str,
        base: str,
        path: str,
        token: Optional[str],
    ) -> SmokeCheck:
        url = f"{base}{path}"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.get(url, headers=headers, timeout=self._timeout_s)
        except Exception as e:
            return SmokeCheck(
                name=f"route:{service_name}{path}",
                category="route",
                target=url,
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
        # 2xx / 3xx → pass. 4xx → pass (route works, just rejected
        # for some valid reason like auth/validation/not-found). 5xx
        # → fail (handler crashed).
        ok = resp.status_code < 500
        return SmokeCheck(
            name=f"route:{service_name}{path}",
            category="route",
            target=url,
            passed=ok,
            status_code=resp.status_code,
            detail="ok" if ok else f"server error {resp.status_code}",
        )

    # ── Contract parsing ────────────────────────────────────────────────

    @staticmethod
    def _find_fa_service(arch: SystemArchitecture) -> Optional[ServiceDefinition]:
        for s in arch.services:
            if (s.service_type or "").lower() == "auth":
                return s
        return None

    @staticmethod
    def _parse_test_users(contract_md: str) -> List[tuple]:
        """Extract ``- email / password — roles ...`` lines from the
        AUTH_CONTRACT.md ``## Test users`` section."""
        users: List[tuple] = []
        in_section = False
        for line in contract_md.splitlines():
            if line.strip().startswith("## "):
                in_section = line.strip().lower().startswith("## test users")
                continue
            if not in_section:
                continue
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            # Format: ``- email / password — roles ROLE ✓``
            body = stripped[2:].split(" — ", 1)[0]
            if "/" not in body:
                continue
            email, _, pw = body.partition("/")
            users.append((email.strip(), pw.strip()))
        return users

    @staticmethod
    def _parse_primary_app_id(contract_md: str) -> Optional[str]:
        """Pull the primary application ID from the contract."""
        for line in contract_md.splitlines():
            s = line.strip()
            if s.startswith("- Primary application ID:"):
                # Format: ``- Primary application ID: `<uuid>` ``
                tick = s.find("`")
                if tick >= 0:
                    end = s.find("`", tick + 1)
                    if end > tick:
                        return s[tick + 1:end]
        return None
