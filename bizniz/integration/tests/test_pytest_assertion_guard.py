"""Tests for ``_assert_pytest_actually_ran_tests`` — the runner-level
guard added 2026-05-16 after the crm_v1 M5 incident where pytest
reported success with zero meaningful test execution."""
from __future__ import annotations

import pytest

from bizniz.integration.runner import _assert_pytest_actually_ran_tests


class TestActuallyRanTests:
    def test_normal_passing_run(self):
        out = (
            "============================= test session starts ==============================\n"
            "collected 5 items\n"
            "\n"
            "tests/integration/test_api.py::test_a PASSED                            [ 20%]\n"
            "tests/integration/test_api.py::test_b PASSED                            [ 40%]\n"
            "tests/integration/test_api.py::test_c PASSED                            [ 60%]\n"
            "tests/integration/test_api.py::test_d PASSED                            [ 80%]\n"
            "tests/integration/test_api.py::test_e PASSED                            [100%]\n"
            "\n"
            "============================== 5 passed in 12.34s ==============================\n"
        )
        ran, reason = _assert_pytest_actually_ran_tests(out)
        assert ran is True
        assert "collected=5" in reason
        assert "5 passed" in reason

    def test_zero_collected(self):
        # Pytest collected 0 — file existed but no test functions.
        out = (
            "============================= test session starts ==============================\n"
            "collected 0 items\n"
            "\n"
            "============================ no tests ran in 0.12s =============================\n"
        )
        ran, reason = _assert_pytest_actually_ran_tests(out)
        assert ran is False
        assert "0 tests" in reason

    def test_missing_collected_header(self):
        # Truncated output, no collection line — treat as failure.
        out = "pytest something something\n"
        ran, reason = _assert_pytest_actually_ran_tests(out)
        assert ran is False
        assert "missing" in reason.lower()

    def test_all_skipped_is_failure(self):
        # Tests existed but every single one was skipped — live stack
        # never exercised. The 2026-05-16 failure mode.
        out = (
            "============================= test session starts ==============================\n"
            "collected 3 items\n"
            "\n"
            "tests/integration/test_api.py::test_a SKIPPED                           [ 33%]\n"
            "tests/integration/test_api.py::test_b SKIPPED                           [ 66%]\n"
            "tests/integration/test_api.py::test_c SKIPPED                           [100%]\n"
            "\n"
            "============================= 3 skipped in 0.45s ==============================\n"
        )
        ran, reason = _assert_pytest_actually_ran_tests(out)
        assert ran is False
        assert "skipped" in reason.lower()

    def test_mixed_pass_and_skip_is_ok(self):
        # If at least some tests passed, the live stack was exercised.
        # Skips alongside passes are normal (env-specific skips, etc.).
        out = (
            "============================= test session starts ==============================\n"
            "collected 4 items\n"
            "\n"
            "tests/integration/test_api.py::test_a PASSED                            [ 25%]\n"
            "tests/integration/test_api.py::test_b SKIPPED                           [ 50%]\n"
            "tests/integration/test_api.py::test_c PASSED                            [ 75%]\n"
            "tests/integration/test_api.py::test_d PASSED                            [100%]\n"
            "\n"
            "======================== 3 passed, 1 skipped in 8.21s ========================\n"
        )
        ran, reason = _assert_pytest_actually_ran_tests(out)
        assert ran is True
        assert "passed" in reason

    def test_failures_are_still_real_execution(self):
        # If tests failed, the runner upstream catches via non-zero
        # exit code. We're only invoked on exit 0, but if we somehow
        # get a "X failed" summary with exit 0, still flag as ran.
        out = (
            "============================= test session starts ==============================\n"
            "collected 2 items\n"
            "\n"
            "tests/integration/test_api.py::test_a PASSED                            [ 50%]\n"
            "tests/integration/test_api.py::test_b FAILED                            [100%]\n"
            "\n"
            "========================= 1 failed, 1 passed in 5.0s ==========================\n"
        )
        ran, reason = _assert_pytest_actually_ran_tests(out)
        # Failures count as "ran" — exit-code check upstream handles
        # the pass/fail decision.
        assert ran is True

    def test_warnings_only_in_summary(self):
        # Pytest summary with warning markers — still considered ran
        # as long as there's a pass.
        out = (
            "============================= test session starts ==============================\n"
            "collected 1 item\n"
            "\n"
            "tests/integration/test_api.py::test_a PASSED                            [100%]\n"
            "\n"
            "======================= 1 passed, 2 warnings in 1.34s =========================\n"
        )
        ran, reason = _assert_pytest_actually_ran_tests(out)
        assert ran is True
