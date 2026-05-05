"""FusionAuth debugger — agentic repair when contract validation fails.

Topologically positioned right after ``provision_fusionauth``: if the
contract validation has any failed checks, this module runs BEFORE
engineer dispatch. Auth being broken means everything downstream is
broken (integration tests can't log in, user-scoped routes can't be
written correctly), so we'd rather fix it here than chase symptoms
through engineering.

Strategy, cheapest-first:

1. **Wait + re-validate.** FusionAuth occasionally needs more time to
   finish bootstrapping; transient checks may pass on a second look.
2. **Re-materialize.** ``orchestrator.materialize(spec)`` is idempotent
   — running it again can heal partial state (e.g. a user whose role
   assignment didn't take on the first pass).
3. **Targeted typed fixes.** For each failed check, inspect the failure
   detail and apply a typed action: recreate a user, re-grant a role,
   restart FusionAuth. No LLM needed for these — the failure name
   directly maps to a fix.
4. **LLM-assisted diagnosis.** Only if steps 1-3 don't converge. Pass
   the failed checks + a small context packet to the LLM and let it
   propose one of the typed actions. Bounded iterations.

Returns the final ``ContractValidationResult``. Caller (architect)
decides what to do — typically: validation_ok → proceed to engineering;
not ok → abort milestone with a clear diagnosis.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.auth.contract import AuthContract, ContractValidationResult, ValidationCheck
from bizniz.auth.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth.kickstart import render_kickstart
from bizniz.auth.spec import AuthSpec
from bizniz.auth.types import FusionAuthError


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _restart_fusionauth(
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Bounce the FusionAuth container. Used when FA needs to re-apply
    kickstart or recover from a broken state. Returns True on success."""
    import subprocess
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_path, "restart", "fusionauth"],
            check=True, capture_output=True, timeout=60,
        )
        _log(on_status, "FA debugger: restarted fusionauth container")
        return True
    except Exception as e:
        _log(on_status, f"FA debugger: restart failed — {e}")
        return False


def _wait_until_ready(
    orchestrator: FusionAuthOrchestrator,
    deadline_s: float = 30.0,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Block until FusionAuth's status endpoint reports healthy."""
    if orchestrator.wait_until_ready(deadline_s=deadline_s, poll_s=2.0):
        _log(on_status, "FA debugger: fusionauth confirmed ready")
        return True
    _log(on_status, f"FA debugger: fusionauth not ready after {deadline_s}s")
    return False


def _apply_typed_fixes(
    failed_checks: List[ValidationCheck],
    auth_spec: AuthSpec,
    auth_contract: AuthContract,
    orchestrator: FusionAuthOrchestrator,
    application_id: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> int:
    """For each failed check, apply a deterministic typed fix.

    Returns the number of fix actions actually attempted (some
    failures don't have a typed fix and get skipped).
    """
    actions = 0
    for check in failed_checks:
        name = check.name
        detail = check.detail or ""

        # role_exists:<role>@<app>  → re-create the role
        if name.startswith("role_exists:"):
            role_name = name.split(":", 1)[1]
            role_def = next(
                (r for r in auth_spec.roles if r.name == role_name), None,
            )
            if role_def is not None:
                _log(on_status, f"FA debugger: re-creating role '{role_name}'")
                try:
                    orchestrator.ensure_role(
                        app_id=application_id,
                        name=role_def.name,
                        description=role_def.description,
                        is_default=role_def.is_default,
                        is_super_role=role_def.is_super_role,
                    )
                    actions += 1
                except FusionAuthError as e:
                    _log(on_status, f"FA debugger: ensure_role failed — {e}")

        # user_exists:<email>  → re-create the user with full role list
        # user_login:<email>   → also re-create (likely a missing-password issue)
        # user_roles:<email>   → re-assign the user's roles
        elif name.startswith(("user_exists:", "user_login:", "user_roles:")):
            email = name.split(":", 1)[1]
            user_def = next(
                (u for u in auth_spec.test_users if u.email == email), None,
            )
            if user_def is None and email == auth_spec.seeded_admin.email:
                # Synth a UserSpec for the seeded admin
                from bizniz.auth.spec import UserSpec
                user_def = UserSpec(
                    email=auth_spec.seeded_admin.email,
                    password=auth_spec.seeded_admin.password,
                    first_name=auth_spec.seeded_admin.first_name,
                    last_name=auth_spec.seeded_admin.last_name,
                    role_names=list(auth_spec.seeded_admin.role_names),
                )
            if user_def is not None:
                _log(on_status, f"FA debugger: re-creating user '{email}'")
                # Compute roles for THIS application
                granted = set(user_def.role_names)
                for gn in user_def.group_names:
                    grp = next(
                        (g for g in auth_spec.groups if g.name == gn), None,
                    )
                    if grp:
                        granted.update(grp.role_names)
                try:
                    orchestrator.ensure_user(
                        app_id=application_id,
                        email=user_def.email,
                        password=user_def.password,
                        first_name=user_def.first_name,
                        last_name=user_def.last_name,
                        roles=sorted(granted),
                        verified=user_def.verified,
                    )
                    actions += 1
                except FusionAuthError as e:
                    _log(on_status, f"FA debugger: ensure_user failed — {e}")

        # application_exists  → re-create the app
        elif name == "application_exists":
            app_name = auth_contract.application_name
            _log(on_status, f"FA debugger: re-creating application '{app_name}'")
            try:
                orchestrator.ensure_application(
                    app_id=application_id, name=app_name,
                )
                actions += 1
            except FusionAuthError as e:
                _log(on_status, f"FA debugger: ensure_application failed — {e}")

    return actions


def repair_fusionauth_state(
    *,
    auth_spec: AuthSpec,
    auth_contract: AuthContract,
    validation_result: ContractValidationResult,
    orchestrator: FusionAuthOrchestrator,
    application_id: str,
    project_root: Path,
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    max_iterations: int = 3,
) -> ContractValidationResult:
    """Try to bring FusionAuth state into compliance with the spec.

    Returns the final ``ContractValidationResult``. On success, the
    caller should re-write AUTH_CONTRACT.md (the original was held back
    because the contract was invalid). On failure, the caller should
    abort the milestone — auth being broken means downstream work
    will produce broken artifacts.
    """
    _log(on_status,
         f"FA debugger: starting repair for {len(validation_result.failed_checks)} "
         f"failed check(s)")

    last_result = validation_result

    for iteration in range(1, max_iterations + 1):
        _log(on_status, f"FA debugger: iteration {iteration}/{max_iterations}")

        # Step 1 (cheapest first iteration): wait + re-validate.
        # FusionAuth may have been mid-bootstrap on the first pass.
        if iteration == 1:
            _wait_until_ready(orchestrator, deadline_s=15.0, on_status=on_status)

        # Step 2: re-materialize the spec (idempotent). Heals
        # partially-applied state from the original run.
        try:
            report = orchestrator.materialize(
                auth_spec, primary_app_id=application_id,
            )
            applied = sum(1 for a in report.actions if a.applied)
            failed = sum(1 for a in report.actions if a.error)
            _log(on_status,
                 f"FA debugger: re-materialize — {applied} applied, "
                 f"{failed} failed")
        except Exception as e:
            _log(on_status, f"FA debugger: materialize failed — {e}")

        # Step 3: typed fixes for each remaining failed check.
        if last_result.failed_checks:
            fix_count = _apply_typed_fixes(
                failed_checks=list(last_result.failed_checks),
                auth_spec=auth_spec,
                auth_contract=auth_contract,
                orchestrator=orchestrator,
                application_id=application_id,
                on_status=on_status,
            )
            _log(on_status,
                 f"FA debugger: applied {fix_count} typed fix action(s)")

        # Step 4 (last iteration only): if still failing, restart FA.
        # Kickstart re-application only happens on a fresh DB volume,
        # not on container restart, so this is mostly a "kick FA in
        # case something is wedged" hammer. We don't overwrite the
        # provisioner's kickstart on disk — provisioner owns that
        # artifact (it carries the api_key + application_id .env
        # depends on).
        if iteration == max_iterations and last_result.failed_checks:
            _log(on_status, "FA debugger: last-resort restart of fusionauth")
            _restart_fusionauth(compose_path, on_status=on_status)
            _wait_until_ready(orchestrator, deadline_s=60.0, on_status=on_status)
            try:
                orchestrator.materialize(
                    auth_spec, primary_app_id=application_id,
                )
            except Exception as e:
                _log(on_status, f"FA debugger: post-restart materialize failed — {e}")

        # Re-validate
        last_result = auth_contract.validate(orchestrator)
        if last_result.ok:
            _log(on_status,
                 f"FA debugger: contract NOW VALID after iteration {iteration} "
                 f"({len(last_result.checks)} checks pass)")
            return last_result

        _log(on_status,
             f"FA debugger: still failing after iteration {iteration} — "
             f"{len(last_result.failed_checks)} check(s) red")
        for check in last_result.failed_checks:
            _log(on_status, f"FA debugger:   • {check.name}: {check.detail}")

        # Brief pause before next iteration (FA propagation lag)
        time.sleep(1.0)

    _log(on_status,
         f"FA debugger: GAVE UP after {max_iterations} iterations — "
         f"{len(last_result.failed_checks)} check(s) still red")
    return last_result
