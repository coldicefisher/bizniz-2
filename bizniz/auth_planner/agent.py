"""AuthPlanner — single-call structured-output agent. Maps a
milestone's problem slice + architecture to an AuthSpec.

No tool loop, no FA calls. Pure intent extraction. The
``FusionAuthOperator`` (deterministic) consumes the spec and
mutates FA.
"""
from __future__ import annotations

from typing import Callable, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.auth_orchestrators.spec import (
    AppSpec, AuthSpec, RoleSpec, UserSpec,
)
from bizniz.auth_planner.prompts.schema import AUTH_PLANNER_SCHEMA
from bizniz.auth_planner.prompts.system_prompt import (
    AUTH_PLANNER_SYSTEM_PROMPT,
)
from bizniz.auth_planner.prompts.user_prompt import build_auth_planner_prompt
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.lib.llm_utils import call_with_retry


class AuthPlannerError(Exception):
    """The AuthPlanner's LLM output failed validation."""


class AuthPlanner:
    """Single-call structured-output agent: problem statement → AuthSpec."""

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    def plan(
        self,
        *,
        problem_slice: str,
        architecture: SystemArchitecture,
    ) -> AuthSpec:
        """Emit an AuthSpec for this milestone.

        Raises ``AuthPlannerError`` on validation failure (empty roles,
        missing required fields, schema mismatch).
        """
        self._log("AuthPlanner: planning auth spec")

        user_prompt = build_auth_planner_prompt(
            problem_slice=problem_slice,
            architecture=architecture,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=AUTH_PLANNER_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=AUTH_PLANNER_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="AuthPlanner",
        )

        try:
            spec = self._build_spec(raw)
        except Exception as e:
            raise AuthPlannerError(
                f"AuthPlanner: LLM output failed AuthSpec construction: {e}; "
                f"raw: {raw!r}"
            ) from e

        self._validate_post(spec)
        self._log(
            f"AuthPlanner: emitted spec — {len(spec.roles)} role(s), "
            f"{len(spec.applications)} app(s), {len(spec.test_users)} user(s)"
        )
        return spec

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _build_spec(raw: dict) -> AuthSpec:
        """Hydrate an AuthSpec from the LLM's structured output."""
        roles = [
            RoleSpec(
                name=r["name"],
                description=r.get("description", ""),
                is_super_role=bool(r.get("is_super_role", False)),
            )
            for r in raw.get("roles") or []
        ]
        applications = [
            AppSpec(
                name=a["name"],
                role_names=list(a.get("role_names") or []),
            )
            for a in raw.get("applications") or []
        ]
        users = [
            UserSpec(
                email=u["email"],
                password=u.get("password", "password"),
                first_name=u.get("first_name", ""),
                last_name=u.get("last_name", ""),
                role_names=list(u.get("role_names") or []),
                password_change_required=False,
                verified=True,
            )
            for u in raw.get("test_users") or []
        ]
        return AuthSpec(
            enabled=bool(raw.get("enable_auth", True)),
            groups_enabled=bool(raw.get("enable_groups", False)),
            multitenant=bool(raw.get("enable_multitenant", False)),
            roles=roles,
            applications=applications,
            test_users=users,
        )

    @staticmethod
    def _validate_post(spec: AuthSpec) -> None:
        if not spec.roles:
            raise AuthPlannerError(
                "AuthPlanner: spec has zero roles — every project with "
                "FusionAuth needs at least super_admin."
            )
        if not spec.applications:
            raise AuthPlannerError(
                "AuthPlanner: spec has zero applications — need at least "
                "the 'primary' app for the project's frontend."
            )
        # Every test user's roles must exist in the spec's roles or
        # be super_admin (which the seeded admin covers automatically).
        spec_role_names = {r.name for r in spec.roles}
        bad_users: list[str] = []
        for u in spec.test_users:
            for rn in u.role_names:
                if rn not in spec_role_names and rn != "super_admin":
                    bad_users.append(f"{u.email} → {rn}")
        if bad_users:
            raise AuthPlannerError(
                f"AuthPlanner: test users reference roles not in spec: "
                f"{bad_users}"
            )

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass
