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
