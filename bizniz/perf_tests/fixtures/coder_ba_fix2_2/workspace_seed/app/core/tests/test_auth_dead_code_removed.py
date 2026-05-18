"""Landmine sentinels for the BA-fix2-3 dead-code removal.

CodeReviewer flagged ``_decode_fusionauth_jwt`` and ``_extract_token``
(both module-level helpers in :mod:`app.core.auth`) as unused leftovers
after ``get_current_user_with_roles`` was deleted. They were never
imported anywhere in the backend, but their continued presence
duplicated the algorithm-pinning + JWKS-cache invariants that the live
``_validate_token_structure_and_alg`` / ``_verify_jwt_signature_and_claims``
helpers own — i.e. a future refactor could accidentally route through
the dead copies and silently weaken the auth contract.

These sentinels lock the removal: if a well-meaning future refactor
re-adds either helper, the assertion below trips immediately and the
re-introduction can be caught at PR time instead of in production. The
sibling BA-fix1-1 sentinels in ``test_repair_session_and_errors.py``
guard ``_sync_user_from_fusionauth`` and ``get_current_user_with_roles``
in exactly the same way — this file extends that hygiene to the two
additionally-flagged helpers and intentionally lives in its own file so
the BA-fix1-1 file stays untouched.
"""
# Settings() instantiation at app.core.config import time requires the
# FUSIONAUTH_* env vars. ``setdefault`` so any real env still wins.
import os as _os

_os.environ.setdefault(
    "FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b"
)
_os.environ.setdefault(
    "FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df"
)
_os.environ.setdefault("FUSIONAUTH_API_KEY", "test-api-key-xyz")

import app.core.auth as auth_mod


class TestDeadHelpersRemoved:
    """The two helpers MUST NOT exist on the module after the repair."""

    def test_decode_fusionauth_jwt_is_gone(self):
        assert not hasattr(auth_mod, "_decode_fusionauth_jwt")
        assert "_decode_fusionauth_jwt" not in auth_mod.__dict__

    def test_extract_token_is_gone(self):
        assert not hasattr(auth_mod, "_extract_token")
        assert "_extract_token" not in auth_mod.__dict__

    def test_live_helpers_still_present(self):
        # Defense-in-depth: the *replacement* helpers that the reviewer
        # said own the alg-pin / JWKS-cache invariants MUST still exist.
        # Without this, a refactor could "fix" the sentinels above by
        # deleting EVERY helper — which would also be wrong.
        assert hasattr(auth_mod, "_verify_jwt_signature_and_claims")
        assert hasattr(auth_mod, "_decode_unverified_header")
        assert hasattr(auth_mod, "_validate_token_shape")
