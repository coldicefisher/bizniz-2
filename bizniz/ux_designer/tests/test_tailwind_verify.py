"""Tests for ProUXDesigner Tailwind-serving verifier retry logic
(Ticket 1b — back-off to absorb Vite warm-up races).

These tests stub ``_probe_tailwind_once`` so we exercise the retry
wrapper directly without making real HTTP calls.
"""
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.pro_ux_designer import ProUXDesigner


def _designer():
    with patch(
        "bizniz.ux_designer.claude_ux_designer.shutil.which",
        return_value="/usr/bin/claude",
    ):
        return ProUXDesigner(vision_client=MagicMock(), on_status=None)


def _service():
    return ServiceDefinition(
        name="frontend", service_type="frontend",
        framework="react", language="typescript",
        description="x", workspace_name="frontend",
        port=5173, depends_on=[], requirements=[], skeleton="react",
    )


class TestRetryWrapper:
    def test_returns_immediately_when_first_probe_ok(self):
        d = _designer()
        d._probe_tailwind_once = MagicMock(return_value={
            "ok": True, "detail": "found", "css_urls": [],
        })
        with patch("bizniz.ux_designer.pro_ux_designer.time.sleep") as sleep:
            out = d._verify_tailwind_serving(_service(), "compose.yml")
        assert out["ok"] is True
        assert out["attempts"] == 1
        assert d._probe_tailwind_once.call_count == 1
        # First attempt has backoff_seconds[0]=0.0 → no sleep call.
        assert sleep.call_count == 0

    def test_retries_until_success(self):
        d = _designer()
        # Fail twice, succeed on third — the Vite warm-up race.
        results = [
            {"ok": False, "detail": "no markers (warm up)", "css_urls": []},
            {"ok": False, "detail": "no markers (warm up)", "css_urls": []},
            {"ok": True, "detail": "found", "css_urls": []},
        ]
        d._probe_tailwind_once = MagicMock(side_effect=results)
        with patch("bizniz.ux_designer.pro_ux_designer.time.sleep") as sleep:
            out = d._verify_tailwind_serving(_service(), "compose.yml")
        assert out["ok"] is True
        assert out["attempts"] == 3
        assert d._probe_tailwind_once.call_count == 3
        # Attempt 2 sleeps 3s; attempt 3 sleeps 7s. Attempt 1 sleeps 0.
        # time.sleep is only called when wait > 0.
        slept = [c.args[0] for c in sleep.call_args_list]
        assert slept == [3.0, 7.0]

    def test_returns_last_failure_after_all_attempts(self):
        d = _designer()
        fail = {"ok": False, "detail": "no markers", "css_urls": []}
        d._probe_tailwind_once = MagicMock(return_value=fail)
        with patch("bizniz.ux_designer.pro_ux_designer.time.sleep"):
            out = d._verify_tailwind_serving(_service(), "compose.yml")
        assert out["ok"] is False
        assert out["attempts"] == 3
        assert d._probe_tailwind_once.call_count == 3

    def test_max_attempts_one_is_single_shot(self):
        d = _designer()
        fail = {"ok": False, "detail": "no markers", "css_urls": []}
        d._probe_tailwind_once = MagicMock(return_value=fail)
        with patch("bizniz.ux_designer.pro_ux_designer.time.sleep") as sleep:
            out = d._verify_tailwind_serving(
                _service(), "compose.yml", max_attempts=1,
            )
        assert out["attempts"] == 1
        assert d._probe_tailwind_once.call_count == 1
        assert sleep.call_count == 0  # backoff[0]=0 → no sleep

    def test_custom_backoff_schedule(self):
        d = _designer()
        d._probe_tailwind_once = MagicMock(side_effect=[
            {"ok": False, "detail": "warm-up", "css_urls": []},
            {"ok": True, "detail": "found", "css_urls": []},
        ])
        with patch("bizniz.ux_designer.pro_ux_designer.time.sleep") as sleep:
            out = d._verify_tailwind_serving(
                _service(), "compose.yml",
                max_attempts=2,
                backoff_seconds=(0.0, 1.5),
            )
        assert out["ok"] is True
        slept = [c.args[0] for c in sleep.call_args_list]
        assert slept == [1.5]
