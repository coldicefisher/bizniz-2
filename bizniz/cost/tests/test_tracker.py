"""Tests for bizniz.cost.tracker."""
from bizniz.cost.tracker import CostTracker, get_tracker, set_tracker


def test_record_appends():
    t = CostTracker()
    rec = t.record(agent="coder", model="gpt-4o-mini",
                   input_tokens=1000, output_tokens=500, duration_ms=1234)
    assert rec.agent == "coder"
    assert rec.model == "gpt-4o-mini"
    assert rec.input_tokens == 1000
    assert rec.output_tokens == 500
    assert rec.duration_ms == 1234
    assert rec.cost.priced is True
    assert rec.cost.total_cost > 0
    assert len(t.records()) == 1


def test_summary_aggregates_by_model_and_agent():
    t = CostTracker()
    t.record(agent="coder", model="gpt-4o-mini",
             input_tokens=1_000_000, output_tokens=0)
    t.record(agent="tester", model="gpt-4o-mini",
             input_tokens=0, output_tokens=1_000_000)
    t.record(agent="coder", model="gpt-4o",
             input_tokens=100_000, output_tokens=0)

    s = t.summary()
    assert s.calls == 3
    assert s.input_tokens == 1_100_000
    assert s.output_tokens == 1_000_000

    # gpt-4o-mini: 1M input * $0.15 + 1M output * $0.60 = $0.75
    # gpt-4o    : 0.1M input * $2.50              = $0.25
    assert abs(s.total_cost - 1.00) < 1e-9
    assert "gpt-4o-mini" in s.by_model
    assert s.by_model["gpt-4o-mini"]["calls"] == 2
    assert s.by_agent["coder"]["calls"] == 2
    assert s.by_agent["tester"]["calls"] == 1


def test_summary_flags_unpriced_models():
    t = CostTracker()
    t.record(agent="x", model="totally-unknown-model",
             input_tokens=1000, output_tokens=2000)
    s = t.summary()
    assert s.unpriced_calls == 1
    assert "totally-unknown-model" in s.unpriced_models
    assert s.total_cost == 0.0


def test_reset_clears_records():
    t = CostTracker()
    t.record(agent="a", model="gpt-4o-mini", input_tokens=1, output_tokens=1)
    t.reset()
    assert t.records() == []
    assert t.summary().calls == 0


def test_set_context_attaches_problem_and_issue_ids():
    t = CostTracker()
    t.set_context(problem_id=42, issue_id=7)
    rec = t.record(agent="x", model="gpt-4o-mini",
                   input_tokens=10, output_tokens=10)
    assert rec.problem_id == 42
    assert rec.issue_id == 7

    # explicit args override the context
    rec2 = t.record(agent="x", model="gpt-4o-mini",
                    input_tokens=10, output_tokens=10,
                    problem_id=99, issue_id=None)
    assert rec2.problem_id == 99
    assert rec2.issue_id == 7  # falls back to context


def test_global_get_tracker_returns_same_instance():
    a = get_tracker()
    b = get_tracker()
    assert a is b


def test_set_tracker_replaces_global():
    fresh = CostTracker()
    set_tracker(fresh)
    try:
        assert get_tracker() is fresh
    finally:
        set_tracker(CostTracker())  # restore so other tests aren't affected


def test_format_summary_smoke():
    t = CostTracker()
    t.record(agent="coder", model="gpt-4o-mini",
             input_tokens=1000, output_tokens=500)
    text = t.summary().format()
    assert "calls=1" in text
    assert "input=1,000" in text
    assert "by model" in text
    assert "gpt-4o-mini" in text
