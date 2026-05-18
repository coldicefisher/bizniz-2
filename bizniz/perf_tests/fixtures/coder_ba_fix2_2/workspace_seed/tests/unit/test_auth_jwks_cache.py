"""Unit tests for the JWKS cache + refresh-on-kid-miss helper.

Covers ``_get_jwks_with_refresh`` and ``_reset_jwks_cache_for_tests``
in ``app.core.auth``:

* Cold cache: first call fetches and stores.
* Warm cache + kid present: no HTTP call.
* Warm cache + kid missing: refresh once.
* Cold cache + FA unavailable: re-raise (caller → 503).
* Warm cache + FA unavailable during refresh: swallow, return
  stale cache (signature check downstream → invalid_token).
* Reset helper sets cache back to ``None``.
* Concurrent kid-miss requests fan out to at most one FA call
  (lock serializes the refresh).

FusionAuth HTTP is mocked at the ``fusionauth_client.get_jwks``
seam so these tests do NOT require a running FA instance.
"""
import asyncio

import pytest

from app.core import auth as auth_module
from app.core.auth import (
    _get_jwks_with_refresh,
    _jwks_contains_kid,
    _reset_jwks_cache_for_tests,
)
from app.services.fusionauth_client import FusionAuthUnavailable


KID_A = "kid-a"
KID_B = "kid-b"
KID_NEW = "kid-rotated"

JWKS_WITH_A = {"keys": [{"kid": KID_A, "kty": "RSA", "alg": "RS256"}]}
JWKS_WITH_A_AND_B = {
    "keys": [
        {"kid": KID_A, "kty": "RSA", "alg": "RS256"},
        {"kid": KID_B, "kty": "RSA", "alg": "RS256"},
    ]
}
JWKS_WITH_NEW = {"keys": [{"kid": KID_NEW, "kty": "RSA", "alg": "RS256"}]}


@pytest.fixture(autouse=True)
def _reset_cache_around_each_test():
    """Each test starts and ends with a cold cache.

    Also rebinds ``_jwks_lock`` to a fresh ``asyncio.Lock`` so it
    lazily binds to the current test's event loop. pytest-asyncio
    uses function-scope event loops by default, and an ``asyncio.Lock``
    bound to a defunct loop raises ``RuntimeError: bound to a
    different event loop`` on the next acquire. Tests that exercise
    concurrent paths hit this immediately.
    """
    _reset_jwks_cache_for_tests()
    auth_module._jwks_lock = asyncio.Lock()
    yield
    _reset_jwks_cache_for_tests()


def _patch_get_jwks(monkeypatch, results):
    """Patch ``fusionauth_client.get_jwks`` to return / raise from ``results``.

    ``results`` is a list whose entries are either a dict (returned)
    or an Exception instance (raised). Each call consumes one entry;
    extra calls raise AssertionError so over-fetching is caught.
    Returns a list that records call count and timestamps.
    """
    call_log = []

    async def _fake_get_jwks():
        if not results:
            raise AssertionError(
                "fusionauth_client.get_jwks called more times than expected"
            )
        call_log.append(True)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        auth_module.fusionauth_client, "get_jwks", _fake_get_jwks
    )
    return call_log


@pytest.mark.unit
class TestJwksContainsKid:
    def test_returns_true_when_kid_present(self):
        assert _jwks_contains_kid(JWKS_WITH_A_AND_B, KID_A) is True
        assert _jwks_contains_kid(JWKS_WITH_A_AND_B, KID_B) is True

    def test_returns_false_when_kid_absent(self):
        assert _jwks_contains_kid(JWKS_WITH_A, "nope") is False

    def test_none_jwks_returns_false(self):
        assert _jwks_contains_kid(None, KID_A) is False

    def test_empty_jwks_returns_false(self):
        assert _jwks_contains_kid({}, KID_A) is False

    def test_missing_keys_array_returns_false(self):
        assert _jwks_contains_kid({"other": "thing"}, KID_A) is False

    def test_keys_is_none_returns_false(self):
        # Defensive against ``{"keys": null}`` from a misbehaving FA.
        assert _jwks_contains_kid({"keys": None}, KID_A) is False


@pytest.mark.unit
class TestColdCache:
    """Cold cache = ``_jwks_cache is None`` at call time."""

    @pytest.mark.asyncio
    async def test_first_call_fetches_and_stores(self, monkeypatch):
        calls = _patch_get_jwks(monkeypatch, [JWKS_WITH_A])
        assert auth_module._jwks_cache is None

        result = await _get_jwks_with_refresh(KID_A)

        assert result == JWKS_WITH_A
        assert auth_module._jwks_cache == JWKS_WITH_A
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cold_cache_fa_unavailable_reraises(self, monkeypatch):
        calls = _patch_get_jwks(
            monkeypatch,
            [FusionAuthUnavailable(status_code=None, body=None, message="boom")],
        )
        assert auth_module._jwks_cache is None

        with pytest.raises(FusionAuthUnavailable):
            await _get_jwks_with_refresh(KID_A)

        # Cache should remain cold so the next request retries.
        assert auth_module._jwks_cache is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cold_cache_propagates_5xx_unavailable(self, monkeypatch):
        calls = _patch_get_jwks(
            monkeypatch,
            [FusionAuthUnavailable(status_code=503, body={"error": "down"})],
        )

        with pytest.raises(FusionAuthUnavailable) as excinfo:
            await _get_jwks_with_refresh(KID_A)
        assert excinfo.value.status_code == 503
        assert len(calls) == 1


@pytest.mark.unit
class TestWarmCacheKidPresent:
    @pytest.mark.asyncio
    async def test_no_http_call_when_kid_present(self, monkeypatch):
        # Pre-warm by manually seeding the cache.
        auth_module._jwks_cache = JWKS_WITH_A_AND_B
        calls = _patch_get_jwks(monkeypatch, [])  # Any call → AssertionError.

        result = await _get_jwks_with_refresh(KID_A)
        assert result == JWKS_WITH_A_AND_B
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_warm_kid_present_returns_cached_object(self, monkeypatch):
        auth_module._jwks_cache = JWKS_WITH_A_AND_B
        _patch_get_jwks(monkeypatch, [])

        result = await _get_jwks_with_refresh(KID_B)
        # The exact cached object is returned — no copy.
        assert result is auth_module._jwks_cache


@pytest.mark.unit
class TestWarmCacheKidMiss:
    """Warm cache but the requested kid isn't in it → refresh once."""

    @pytest.mark.asyncio
    async def test_refresh_succeeds_returns_new_keys(self, monkeypatch):
        auth_module._jwks_cache = JWKS_WITH_A  # warm
        calls = _patch_get_jwks(monkeypatch, [JWKS_WITH_NEW])

        result = await _get_jwks_with_refresh(KID_NEW)

        assert result == JWKS_WITH_NEW
        assert auth_module._jwks_cache == JWKS_WITH_NEW
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_unavailable_returns_stale_cache(self, monkeypatch):
        stale = dict(JWKS_WITH_A)
        auth_module._jwks_cache = stale  # warm
        calls = _patch_get_jwks(
            monkeypatch,
            [FusionAuthUnavailable(status_code=None, body=None, message="net")],
        )

        result = await _get_jwks_with_refresh(KID_NEW)

        # Returned the stale cache — caller's signature check will
        # then fail with invalid_token for the missing kid.
        assert result == stale
        assert auth_module._jwks_cache == stale
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_unavailable_5xx_returns_stale_cache(self, monkeypatch):
        stale = dict(JWKS_WITH_A)
        auth_module._jwks_cache = stale
        _patch_get_jwks(
            monkeypatch,
            [FusionAuthUnavailable(status_code=502, body="bad gateway")],
        )

        result = await _get_jwks_with_refresh(KID_NEW)
        assert result == stale

    @pytest.mark.asyncio
    async def test_refresh_does_not_retry_on_subsequent_call(self, monkeypatch):
        """Two kid-miss calls when FA is down → only ONE refresh attempt per call.

        Each call to ``_get_jwks_with_refresh`` is allowed to issue
        its own single refresh; the "refresh ONCE" rule is per-call,
        not lifetime. (A debounce belongs to the caller if it wants
        one.) This test pins that contract.
        """
        stale = dict(JWKS_WITH_A)
        auth_module._jwks_cache = stale
        unavailable = FusionAuthUnavailable(
            status_code=None, body=None, message="net"
        )
        calls = _patch_get_jwks(monkeypatch, [unavailable, unavailable])

        r1 = await _get_jwks_with_refresh(KID_NEW)
        r2 = await _get_jwks_with_refresh(KID_NEW)

        assert r1 == stale
        assert r2 == stale
        assert len(calls) == 2


@pytest.mark.unit
class TestResetHelper:
    def test_reset_sets_cache_to_none(self):
        auth_module._jwks_cache = JWKS_WITH_A
        assert auth_module._jwks_cache is not None
        _reset_jwks_cache_for_tests()
        assert auth_module._jwks_cache is None

    def test_reset_idempotent_on_already_cold_cache(self):
        auth_module._jwks_cache = None
        _reset_jwks_cache_for_tests()
        assert auth_module._jwks_cache is None


@pytest.mark.unit
class TestConcurrency:
    """N concurrent kid-misses → at most one FA call (lock serializes)."""

    @pytest.mark.asyncio
    async def test_concurrent_cold_calls_dedupe(self, monkeypatch):
        slow_done = asyncio.Event()
        calls = []

        async def _slow_get_jwks():
            calls.append(True)
            # Let other coroutines queue on the lock before we return.
            await asyncio.sleep(0.05)
            slow_done.set()
            return JWKS_WITH_A

        monkeypatch.setattr(
            auth_module.fusionauth_client, "get_jwks", _slow_get_jwks
        )

        # Fan out 5 concurrent cold-cache requests.
        results = await asyncio.gather(
            *[_get_jwks_with_refresh(KID_A) for _ in range(5)]
        )

        assert slow_done.is_set()
        assert all(r == JWKS_WITH_A for r in results)
        # Only one HTTP call despite five concurrent waiters.
        assert len(calls) == 1
        assert auth_module._jwks_cache == JWKS_WITH_A

    @pytest.mark.asyncio
    async def test_concurrent_kid_miss_refreshes_once(self, monkeypatch):
        auth_module._jwks_cache = JWKS_WITH_A
        calls = []

        async def _slow_get_jwks():
            calls.append(True)
            await asyncio.sleep(0.05)
            return JWKS_WITH_NEW

        monkeypatch.setattr(
            auth_module.fusionauth_client, "get_jwks", _slow_get_jwks
        )

        # 5 concurrent kid-misses → only ONE refresh, others see the
        # post-lock re-check satisfied.
        results = await asyncio.gather(
            *[_get_jwks_with_refresh(KID_NEW) for _ in range(5)]
        )

        assert all(r == JWKS_WITH_NEW for r in results)
        assert len(calls) == 1
        assert auth_module._jwks_cache == JWKS_WITH_NEW


@pytest.mark.unit
class TestExportedSymbols:
    """The module-level symbols this issue ships must exist and have
    the right types — downstream issues (BE-006-U4/U5) import them
    directly from ``app.core.auth``.
    """

    def test_jwks_cache_attribute_exists(self):
        assert hasattr(auth_module, "_jwks_cache")

    def test_jwks_lock_is_asyncio_lock(self):
        assert hasattr(auth_module, "_jwks_lock")
        assert isinstance(auth_module._jwks_lock, asyncio.Lock)

    def test_get_jwks_with_refresh_is_coroutine_function(self):
        import inspect
        assert inspect.iscoroutinefunction(
            auth_module._get_jwks_with_refresh
        )

    def test_reset_helper_is_sync_callable(self):
        import inspect
        assert callable(auth_module._reset_jwks_cache_for_tests)
        assert not inspect.iscoroutinefunction(
            auth_module._reset_jwks_cache_for_tests
        )
