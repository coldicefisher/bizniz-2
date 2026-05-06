"""Tests for driver.gates."""
import pytest

from bizniz.driver.gates import GateAction, GatePolicy, GateViolation


class TestGatePolicy:
    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            GatePolicy(mode="bogus")

    def test_hard_always_halts_strict(self):
        gp = GatePolicy(mode="strict")
        with pytest.raises(GateViolation) as exc:
            gp.hard("g", "boom")
        assert exc.value.gate_name == "g"
        assert exc.value.hard is True

    def test_hard_always_halts_auto(self):
        gp = GatePolicy(mode="auto")
        with pytest.raises(GateViolation):
            gp.hard("g", "boom")

    def test_soft_warns_in_strict(self):
        gp = GatePolicy(mode="strict")
        action = gp.soft("g", "concern")
        assert action == GateAction.WARN

    def test_soft_warns_in_auto(self):
        gp = GatePolicy(mode="auto")
        action = gp.soft("g", "concern")
        assert action == GateAction.WARN

    def test_soft_halts_in_interactive(self):
        gp = GatePolicy(mode="interactive")
        with pytest.raises(GateViolation) as exc:
            gp.soft("g", "concern")
        assert exc.value.hard is False

    def test_status_callback_fires_on_warn(self):
        seen = []
        gp = GatePolicy(mode="strict", on_status=lambda m: seen.append(m))
        gp.soft("g", "concern")
        assert any("WARN" in m for m in seen)
        assert any("g" in m for m in seen)

    def test_status_callback_fires_on_halt(self):
        seen = []
        gp = GatePolicy(mode="strict", on_status=lambda m: seen.append(m))
        with pytest.raises(GateViolation):
            gp.hard("g", "boom")
        assert any("FAIL" in m for m in seen)

    def test_conditional_routes_to_hard(self):
        gp = GatePolicy(mode="strict")
        with pytest.raises(GateViolation):
            gp.conditional("g", hard=True, reason="x")

    def test_conditional_routes_to_soft(self):
        gp = GatePolicy(mode="strict")
        action = gp.conditional("g", hard=False, reason="x")
        assert action == GateAction.WARN
