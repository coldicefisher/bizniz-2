import pytest
from bizniz.orchestrator.model_progression import ModelProgression, DEFAULT_PROGRESSION


def test_default_progression():
    mp = ModelProgression()
    assert mp.current_model == "gpt-4o-mini"
    assert not mp.is_at_max


def test_escalate():
    mp = ModelProgression(["gpt-4o-mini", "gpt-4o", "gpt-5"])
    assert mp.current_model == "gpt-4o-mini"

    result = mp.escalate()
    assert result == "gpt-4o"
    assert mp.current_model == "gpt-4o"

    result = mp.escalate()
    assert result == "gpt-5"
    assert mp.current_model == "gpt-5"
    assert mp.is_at_max


def test_escalate_at_max_returns_none():
    mp = ModelProgression(["gpt-4o-mini"])
    assert mp.is_at_max
    assert mp.escalate() is None


def test_reset():
    mp = ModelProgression(["gpt-4o-mini", "gpt-4o"])
    mp.escalate()
    assert mp.current_model == "gpt-4o"
    mp.reset()
    assert mp.current_model == "gpt-4o-mini"


def test_empty_progression_raises():
    with pytest.raises(ValueError):
        ModelProgression([])
